"""Session identity contract (spec 2026-07-18): tier resolution units."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.writer_context import (
    reset_writer_context, resolve_writer, resolve_writer_detailed,
    set_writer_context,
)


def test_detailed_override_maps_to_header_slot():
    tok = set_writer_context("w1", "sessA")
    try:
        assert resolve_writer_detailed("dflt") == ("w1", "sessA", None)
        assert resolve_writer("dflt") == ("w1", "sessA")
    finally:
        reset_writer_context(tok)


def test_detailed_no_context_returns_default_and_nones():
    assert resolve_writer_detailed("dflt") == ("dflt", None, None)
    assert resolve_writer("dflt") == ("dflt", None)


# ── Service-side tier resolution + persistent active-session pointer
# (PG-backed) ─────────────────────────────────────────────────────────

from tests.pg_fixtures import (  # noqa: F401  (fixtures)
    pg_conn, pg_service, pg_url,
)


def test_resolver_prefers_header_then_pointer_then_transport(pg_service):
    svc = pg_service
    svc.set_active_session("hookSess")
    tok = set_writer_context("w", "headerSess")
    try:
        assert svc._resolve_writer() == ("w", "headerSess")
    finally:
        reset_writer_context(tok)
    # no header: pointer wins (transport can't be simulated without the MCP
    # request context — its tier is covered by resolve_writer_detailed units)
    assert svc._resolve_writer()[1] == "hookSess"
    svc.set_active_session(None)
    assert svc._resolve_writer()[1] is None


def test_pointer_persists_and_clear_only_if_owner(pg_service):
    svc = pg_service
    svc.set_active_session("s1")
    assert svc._storage.get_meta("active_session_pointer")["session_id"] == "s1"
    assert svc.clear_active_session("someone-else") is False
    assert svc._resolve_writer()[1] == "s1"
    assert svc.clear_active_session("s1") is True
    assert svc._resolve_writer()[1] is None
    assert svc._storage.get_meta("active_session_pointer") is None


# ── Pointer TTL (finding 4, 2026-07-19): a crashed client that never fires
# SessionEnd must not attract other clients' tier-3 writes forever ───────────


def test_pointer_expires_past_ttl(pg_service, monkeypatch):
    """A pointer older than the TTL is ignored — tier 3 falls through instead
    of attributing to a dead session."""
    svc = pg_service
    monkeypatch.setenv("PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS", "3600")
    svc.set_active_session("liveSess")
    assert svc._resolve_writer()[1] == "liveSess"          # fresh -> resolves
    # age the pointer past the TTL (refresh is on-set only, so this is stale)
    svc._active_session = ("liveSess", svc._active_session[1] - 7200)
    assert svc._resolve_writer()[1] is None                # expired -> ignored


def test_pointer_ttl_zero_disables(pg_service, monkeypatch):
    """TTL=0 disables expiry (matching the resume-window convention) — an
    ancient pointer still resolves."""
    svc = pg_service
    monkeypatch.setenv("PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS", "0")
    svc.set_active_session("s")
    svc._active_session = ("s", 0.0)                        # ancient timestamp
    assert svc._resolve_writer()[1] == "s"


def test_pointer_legacy_shape_without_ts_is_stale(pg_service, monkeypatch):
    """A legacy pointer hydrates to ts=0.0 (no timestamp field); under a
    positive TTL that reads as infinitely old and is ignored until the next
    SessionStart re-registers it — fail-safe, not a crash."""
    svc = pg_service
    monkeypatch.setenv("PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS", "21600")
    svc._active_session = ("legacySess", 0.0)
    assert svc._resolve_writer()[1] is None


def test_pointer_legacy_shape_hydration_from_meta(pg_service, tmp_path):
    """End-to-end hydration of a pre-TTL persisted pointer (2026-07-19 review
    coverage note): a meta row without a ``ts`` field — written by a daemon
    older than the TTL change — must hydrate through ``_ensure_init`` as
    ts=0.0, not crash, and read as stale under a positive TTL."""
    from pseudolife_memory.service import MemoryService

    pg_service._storage.set_meta(
        "active_session_pointer", {"session_id": "preTtlSess"})  # no "ts"
    svc2 = MemoryService(data_dir=tmp_path / "second")
    try:
        svc2._ensure_init()  # env still points at the test PG (fixture-set)
        assert svc2._active_session == ("preTtlSess", 0.0)
        assert svc2._resolve_writer()[1] is None  # default TTL 6h -> stale
    finally:
        svc2._storage.close()  # don't leak a backend onto the shared bench PG


# ── Ownership guards on both episode-close paths (Task 3) ────────────────────
# The observed bug: `episode_end_session(None)` used to force-close ANY open
# root, and `episode_end()`'s no-identity fallback popped whatever episode
# happened to be globally "current" — either way, one workstream could pop
# another's session episode out from under it.


def test_end_session_never_pops_foreign_root(pg_service):
    svc = pg_service
    svc.episode_start_session("victim-key", "victim session")
    svc.store("victim entry", source="t")          # non-empty -> survives close-prune
    tok = set_writer_context("w", None)            # resolver yields no identity
    try:
        res = svc.episode_end_session(None)
        assert res == {"closed": None, "reason": "no owned open session"}
    finally:
        reset_writer_context(tok)
    tok = set_writer_context("w", "attacker-key")  # identity that owns nothing
    try:
        res = svc.episode_end_session(None)
        assert res == {"closed": None, "reason": "no owned open session"}
    finally:
        reset_writer_context(tok)
    tok = set_writer_context("w", "victim-key")    # the owner can close it
    try:
        res = svc.episode_end_session(None)
        assert res.get("id")
    finally:
        reset_writer_context(tok)


def test_episode_end_fallthrough_guarded(pg_service):
    svc = pg_service
    svc.episode_start_session("victim-key", "victim session")
    svc.store("victim entry", source="t")
    tok = set_writer_context("w", "attacker-key")
    try:
        res = svc.episode_end()               # no open sub-episode for attacker
        # attacker has a resolved identity but owns nothing open, so
        # open_leaf_for("attacker-key") is None before the ownership-mismatch
        # branch is even reached -> plain no-op, not the mismatch dict.
        assert res == {}
    finally:
        reset_writer_context(tok)
    with svc._lock:
        open_roots = [e for e in svc._cms.episodes.episodes.values()
                      if e.parent_id is None and e.ended_at is None
                      and e.session_key == "victim-key"]
    assert len(open_roots) == 1


# ── Task 4: `episode` handle on write tools (identity tier 2) ────────────────


def test_store_with_valid_handle_attributes_and_keys(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyA", "session A")
    res = svc.store("handled entry", source="t", episode=ep["id"][:12])
    assert "episode_warning" not in res
    found = [e for band in svc._cms.bands for e in band.entries
             if e.text == "handled entry"]
    assert found and found[0].episode_id == ep["id"]


def test_store_with_bad_handle_warns_and_degrades(pg_service):
    svc = pg_service
    res = svc.store("degraded entry", source="t", episode="nope-not-real")
    assert res["episode_warning"] == "unknown or closed episode handle"
    assert res.get("stored") is not None      # the write itself succeeded


def test_outcome_with_handle_lands_on_episode(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyB", "session B")
    svc.record_outcome(task="t", outcome="success", episode=ep["id"][:12])
    sigs = [s for s in svc._storage.pending_signals(limit=100)
            if s.get("episode_id") == ep["id"]]
    assert len(sigs) == 1


def test_short_prefix_rejected(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyC", "session C")
    res = svc.store("short prefix", source="t", episode=ep["id"][:4])
    assert res["episode_warning"] == "unknown or closed episode handle"


def test_fact_set_with_valid_handle_stamps_session_key(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyD", "session D")
    res = svc.cortex_write("widget", "color", "blue", episode=ep["id"][:10])
    assert "episode_warning" not in res
    assert res.get("session_id") == "keyD"


def test_episode_end_no_identity_never_pops_foreign_root(pg_service):
    """The real fallthrough: no resolved identity at all, while another
    session's root is the globally "current" episode. `Episodes.open_episode`
    would hand back that root regardless of ownership if the guard weren't
    there."""
    svc = pg_service
    svc.episode_start_session("victim-key", "victim session")
    svc.store("victim entry", source="t")
    tok = set_writer_context("w", None)
    try:
        res = svc.episode_end()
        assert res == {"closed": None, "reason": "no owned open session"}
    finally:
        reset_writer_context(tok)
    with svc._lock:
        open_roots = [e for e in svc._cms.episodes.episodes.values()
                      if e.parent_id is None and e.ended_at is None
                      and e.session_key == "victim-key"]
    assert len(open_roots) == 1


def test_handle_with_header_present_header_wins_identity(pg_service):
    """Header session wins identity resolution, but handle target still gets
    the attribution. This is the design doc's 'one disagreement case that
    matters': when both are present, the handle's episode receives the write
    while the header session becomes the resolved identity."""
    svc = pg_service
    ep = svc.episode_start_session("handleKey", "handle session")
    tok = set_writer_context("w", "headerKey")
    try:
        res = svc.store("both present", source="t", episode=ep["id"][:12])
        assert "episode_warning" not in res
        found = [e for band in svc._cms.bands for e in band.entries
                 if e.text == "both present"]
        # attribution targets the handle's episode...
        assert found and found[0].episode_id == ep["id"]
        # ...while identity resolution still yields the header session
        assert svc._resolve_writer() == ("w", "headerKey")
    finally:
        reset_writer_context(tok)


def test_outcome_with_header_and_handle(pg_service):
    """Pin: record_outcome attributes to the handle's episode unconditionally,
    same as store() — a header session must not steal the signal's
    attribution (spec 2026-07-18, "Precedence rationale")."""
    svc = pg_service
    ep = svc.episode_start_session("handleKey2", "handle session 2")
    tok = set_writer_context("w", "headerKey2")
    try:
        svc.record_outcome(task="t", outcome="success", episode=ep["id"][:12])
    finally:
        reset_writer_context(tok)
    sigs = [s for s in svc._storage.pending_signals(limit=100)
            if s.get("episode_id") == ep["id"]]
    assert len(sigs) == 1


# ── Task 5: hook endpoints — register on start, close on end (identity
# tier 3, spec 2026-07-18) ────────────────────────────────────────────────


def test_hook_start_registers_and_advertises(pg_service):
    from pseudolife_memory.web.session_hook import hook_session_start
    text = hook_session_start(pg_service, session_id="claudeSess1",
                              source="startup")
    assert "Session episode:" in text
    # Spec 2026-08-10 (tier-2 promotion): the handle is advertised as
    # always-pass, not conditional on concurrency.
    assert "every memory write" in text
    assert "when running concurrent sessions" not in text
    assert pg_service._resolve_writer()[1] == "claudeSess1"
    # idempotent on resume
    text2 = hook_session_start(pg_service, session_id="claudeSess1",
                               source="resume")
    assert text.splitlines()[0] == text2.splitlines()[0]


def test_hook_end_closes_and_clears_only_owner(pg_service):
    from pseudolife_memory.web.session_hook import (
        hook_session_end, hook_session_start)
    hook_session_start(pg_service, session_id="claudeSess2", source="startup")
    pg_service.store("an entry", source="t")
    assert hook_session_end(pg_service, session_id="other") == {"ok": True}
    assert pg_service._resolve_writer()[1] == "claudeSess2"   # not cleared
    assert hook_session_end(pg_service, session_id="claudeSess2") == {"ok": True}
    assert pg_service._resolve_writer()[1] is None


# ── Task 6 (security fix): hook mutation paths honor the bearer gate ───────
# Verified finding: branch 4 (session-start) only used `_authorized(scope)`
# to gate the briefing CONTENT, and branch 4b (session-end) never checked it
# at all — with PSEUDOLIFE_MCP_TOKEN configured, an unauthenticated LAN
# client could still hijack the active-session pointer and force-close
# sessions. ASGI-level coverage (there was none in either direction).

import json

from pseudolife_memory.web.api import build_console_app

from tests.asgi_helpers import call, stub_mcp


def test_hook_endpoints_unauthorized_with_token_do_not_mutate(pg_service):
    """Token configured, no bearer header: session-start must serve the
    instructions only (no registration, no advertisement, no pointer write)
    and session-end must be rejected outright — pre-fix, both silently
    mutated state regardless of the token."""
    svc = pg_service
    app = build_console_app(stub_mcp, "secret", lambda: {"status": "ok"}, svc)

    st, body = call(app, "GET", "/api/hook/session-start",
                    query="session_id=evil")
    assert st == 200
    text = body.decode("utf-8")
    assert "memory_search" in text            # instructions still serve
    assert "Session episode:" not in text     # no advertisement
    assert svc._resolve_writer()[1] is None   # no pointer hijack
    with svc._lock:
        assert not any(e.session_key == "evil"
                       for e in svc._cms.episodes.episodes.values())

    st2, _ = call(app, "POST", "/api/hook/session-end",
                  body=json.dumps({"session_id": "evil"}).encode())
    assert st2 == 401
    assert svc._resolve_writer()[1] is None


def test_hook_endpoints_authorized_with_token_mutate_normally(pg_service):
    """With the correct bearer, both hook endpoints behave exactly as they
    do with no token configured: session-start registers + advertises,
    session-end closes and clears the pointer it owns."""
    svc = pg_service
    app = build_console_app(stub_mcp, "secret", lambda: {"status": "ok"}, svc)
    auth = [(b"authorization", b"Bearer secret")]

    st, body = call(app, "GET", "/api/hook/session-start",
                    query="session_id=goodSess", headers=auth)
    assert st == 200
    assert "Session episode:" in body.decode("utf-8")
    assert svc._resolve_writer()[1] == "goodSess"

    st2, body2 = call(app, "POST", "/api/hook/session-end", headers=auth,
                      body=json.dumps({"session_id": "goodSess"}).encode())
    assert st2 == 200
    assert json.loads(body2) == {"ok": True}
    assert svc._resolve_writer()[1] is None


# ── handle-path resume of a reaped episode (2026-08-10 incident) ─────────────
# The idle reaper closed a session's root mid-session; the briefing handle the
# session was told to always-pass then warned instead of attributing. The
# session-key path already resumes a reaped root within the resume window
# (_resume_closed_session_locked); the explicit-handle path must match.
import time as _time


def test_store_with_reaped_handle_resumes_within_window(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyR", "session R")
    svc.store("seed keyR", source="t", episode=ep["id"][:12])
    svc.episode_end_session("keyR")   # simulate the idle reaper; close survives (non-empty)
    res = svc.store("resumed via handle", source="t", episode=ep["id"][:12])
    assert "episode_warning" not in res
    found = [e for band in svc._cms.bands for e in band.entries
             if e.text == "resumed via handle"]
    assert found and found[0].episode_id == ep["id"]
    root = svc._cms.episodes.episodes[ep["id"]]
    assert root.ended_at is None             # the episode is open again


def test_reaped_handle_does_not_hijack_current_pointer(pg_service):
    """A handle write may come from a DIFFERENT session (the concurrency
    use-case) — resuming the target root must not redirect the global
    current-episode pointer the way a session-key resume deliberately does."""
    svc = pg_service
    ep = svc.episode_start_session("keyS", "session S")
    svc.store("seed keyS", source="t", episode=ep["id"][:12])
    svc.episode_end_session("keyS")   # close survives: episode is non-empty
    other = svc.episode_start_session("keyT", "session T")
    svc.store("cross-session attribution", source="t", episode=ep["id"][:12])
    assert svc._cms.episodes.current_id == other["id"]


def test_reaped_handle_resumes_past_session_key_window(pg_service):
    """A presented handle is an explicit identity claim — only that session's
    briefing could have carried it — so it resumes under its own, much longer
    window (``PSEUDOLIFE_HANDLE_RESUME_SECONDS``, default 30 d), not the 6 h
    session-key window. A deferred task returning days later must still
    attribute (2026-09-02 incident)."""
    svc = pg_service
    ep = svc.episode_start_session("keyU", "session U")
    svc.store("seed keyU", source="t", episode=ep["id"][:12])
    svc.episode_end_session("keyU")   # close survives: episode is non-empty
    root = svc._cms.episodes.episodes[ep["id"]]
    root.ended_at = _time.time() - 30_000    # beyond 6 h, inside the handle window
    res = svc.store("back after a long break", source="t", episode=ep["id"][:12])
    assert "episode_warning" not in res
    assert root.ended_at is None             # resumed


def test_reaped_handle_past_handle_window_warns(pg_service, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_HANDLE_RESUME_SECONDS", "3600")
    svc = pg_service
    ep = svc.episode_start_session("keyU2", "session U2")
    svc.store("seed keyU2", source="t", episode=ep["id"][:12])
    svc.episode_end_session("keyU2")   # close survives: episode is non-empty
    root = svc._cms.episodes.episodes[ep["id"]]
    root.ended_at = _time.time() - 7_200     # beyond the shrunk handle window
    res = svc.store("too old", source="t", episode=ep["id"][:12])
    assert res["episode_warning"] == "unknown or closed episode handle"


def test_reaped_handle_resume_disabled_warns(pg_service, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_HANDLE_RESUME_SECONDS", "0")
    svc = pg_service
    ep = svc.episode_start_session("keyV", "session V")
    svc.store("seed keyV", source="t", episode=ep["id"][:12])
    svc.episode_end_session("keyV")   # close survives: episode is non-empty
    res = svc.store("resume off", source="t", episode=ep["id"][:12])
    assert res["episode_warning"] == "unknown or closed episode handle"


# ── tombstone recreation (empty roots swept during a long break) ─────────────
# A session that only reads (searches, fact_gets) before a multi-day break has
# an EMPTY root: the reaper closes it, and the sweep deletes it once past the
# session resume window. The always-pass briefing handle must survive even
# that — a tombstone lets the resolver recreate the episode under its
# original id (2026-09-02 incident).


def test_reap_keeps_empty_root_within_retention(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyE", "session E")     # open, 0 entries
    out = svc.reap_idle_sessions(idle_seconds=0, now=_time.time() + 10_000)
    assert out["reaped"] == 1
    root = svc._cms.episodes.episodes[ep["id"]]             # closed, kept
    assert root.ended_at is not None


def test_swept_empty_root_handle_recreates_from_tombstone(pg_service):
    svc = pg_service
    ep = svc.episode_start_session("keyT2", "session T2")   # open, 0 entries
    svc.reap_idle_sessions(idle_seconds=0, now=_time.time() + 10_000)
    out = svc.reap_idle_sessions(idle_seconds=0, now=_time.time() + 30_000)
    assert out["swept"] == 1
    assert ep["id"] not in svc._cms.episodes.episodes       # row deleted
    res = svc.store("deferred work resumed", source="t", episode=ep["id"][:12])
    assert "episode_warning" not in res
    root = svc._cms.episodes.episodes[ep["id"]]             # recreated, same id
    assert root.ended_at is None and root.session_key == "keyT2"
    found = [e for band in svc._cms.bands for e in band.entries
             if e.text == "deferred work resumed"]
    assert found and found[0].episode_id == ep["id"]


def test_tombstone_survives_daemon_restart(pg_service, tmp_path):
    """The tombstone map must reach storage meta — without write-through a
    daemon restart during the break silently loses the handle anyway (the
    same failure shape as the PR #134 storage finding)."""
    svc = pg_service
    ep = svc.episode_start_session("keyP", "session P")     # open, 0 entries
    svc.reap_idle_sessions(idle_seconds=0, now=_time.time() + 30_000)
    assert ep["id"] not in svc._cms.episodes.episodes       # close+sweep, one pass
    from pseudolife_memory.service import MemoryService
    svc2 = MemoryService(data_dir=tmp_path / "restart")
    svc2._ensure_init()
    res = svc2.store("write after restart", source="t", episode=ep["id"][:12])
    assert "episode_warning" not in res
    root = svc2._cms.episodes.episodes[ep["id"]]
    assert root.ended_at is None and root.session_key == "keyP"


def test_tombstone_recreation_preserves_agent_title(pg_service):
    """An agent-set session title must ride the tombstone — recreating the
    episode with a generic stamp would discard the one human-meaningful
    label the session deliberately left (review finding, 2026-09-02)."""
    svc = pg_service
    ep = svc.episode_start_session("keyTT", "session TT")   # open, 0 entries
    svc.set_session_title("PseudoLife - deferred benchmark",
                          episode=ep["id"][:12])
    svc.reap_idle_sessions(idle_seconds=0, now=_time.time() + 10_000)
    out = svc.reap_idle_sessions(idle_seconds=0, now=_time.time() + 30_000)
    assert out["swept"] == 1
    res = svc.store("back after the break", source="t", episode=ep["id"][:12])
    assert "episode_warning" not in res
    root = svc._cms.episodes.episodes[ep["id"]]
    assert root.title == "PseudoLife - deferred benchmark"


def test_tombstone_recreation_respects_handle_window(pg_service, monkeypatch):
    svc = pg_service
    ep = svc.episode_start_session("keyTW", "session TW")   # open, 0 entries
    svc.reap_idle_sessions(idle_seconds=0, now=_time.time() + 30_000)
    assert ep["id"] not in svc._cms.episodes.episodes       # close+sweep, one pass
    monkeypatch.setenv("PSEUDOLIFE_HANDLE_RESUME_SECONDS", "3600")
    t = svc._episode_tombstones[ep["id"]]                   # age the tombstone
    svc._episode_tombstones[ep["id"]] = (
        (t[0], _time.time() - 7_200) + tuple(t[2:]))
    res = svc.store("too late", source="t", episode=ep["id"][:12])
    assert res["episode_warning"] == "unknown or closed episode handle"


def test_tombstone_expiry_and_cap(pg_service):
    """Window expiry and the 200 cap are only reachable through a sweep —
    exercise them directly so a regression there is visible."""
    svc = pg_service
    now = _time.time()
    svc._episode_tombstones = {
        f"{i:032x}": ("k", now - i, "") for i in range(250)}
    svc._episode_tombstones["f" * 32] = ("k", now - 3_000_000, "")  # > 30 d
    svc._expire_tombstones_locked(now)
    assert "f" * 32 not in svc._episode_tombstones          # expired
    assert len(svc._episode_tombstones) == 200              # capped, newest kept
    assert f"{0:032x}" in svc._episode_tombstones
    assert f"{249:032x}" not in svc._episode_tombstones


def test_deferred_empty_close_fires_no_dream(pg_service, monkeypatch):
    """An empty root closed-but-kept by the reaper must not fire a dream —
    empties never did (they were pruned), and deferral must not change that."""
    svc = pg_service
    calls = []
    monkeypatch.setattr(svc, "_fire_and_forget_dream",
                        lambda: calls.append(1))
    svc.episode_start_session("keyD", "session D")          # open, 0 entries
    svc.reap_idle_sessions(idle_seconds=0, now=_time.time() + 10_000)
    assert calls == []


def test_handle_touch_survives_the_next_reaper_sweep(pg_service):
    """Post-review hardening (finding 1): the reaper proxies activity by
    band-entry timestamps, but record_outcome never writes a band entry —
    without the touch map, a handle-resumed episode is re-reaped on the
    very next sweep, firing a dream per resume cycle."""
    svc = pg_service
    ep = svc.episode_start_session("keyW", "session W")
    svc.store("seed keyW", source="t", episode=ep["id"][:12])
    for band in svc._cms.bands:                # age the only activity proxy
        for e in band.entries:
            if e.episode_id == ep["id"]:
                e.timestamp = _time.time() - 20_000
    svc._episode_touches.clear()               # the seed store touched too
    svc.reap_idle_sessions(7_200)
    assert svc._cms.episodes.episodes[ep["id"]].ended_at is not None
    svc.record_outcome(task="t", outcome="success", episode=ep["id"][:12])
    root = svc._cms.episodes.episodes[ep["id"]]
    assert root.ended_at is None               # resumed
    svc.reap_idle_sessions(7_200)
    assert root.ended_at is None, "touched episode must survive the sweep"
    # The touch map is exactly what keeps it alive: clear it and the same
    # sweep re-reaps (the pre-hardening behavior).
    svc._episode_touches.clear()
    svc.reap_idle_sessions(7_200)
    assert root.ended_at is not None


def test_handle_resume_reaches_storage(pg_service):
    """Post-review hardening (finding 2): the reopen must hit the episodes
    table, or a daemon restart hydrates the root closed again and the
    2026-08-10 symptom returns silently."""
    svc = pg_service
    ep = svc.episode_start_session("keyY", "session Y")
    svc.store("seed keyY", source="t", episode=ep["id"][:12])
    svc.episode_end_session("keyY")
    svc.store("resumed keyY", source="t", episode=ep["id"][:12])
    row = next(r for r in svc._storage.load_episodes() if r["id"] == ep["id"])
    assert row["ended_at"] is None


def test_hook_resume_touch_survives_the_next_reaper_sweep(pg_service):
    """Review follow-up (2026-08-11): _resume_closed_session_locked (the
    session-key path shared by episode_start_session and the store path)
    cleared ended_at without writing a touch — the same blind spot the
    handle path fixed above. A SessionStart-resumed episode whose session
    then makes only non-band-entry writes (outcomes, cortex writes) was
    re-reaped on the very next sweep."""
    svc = pg_service
    ep = svc.episode_start_session("keyX", "session X")
    svc.store("seed keyX", source="t", episode=ep["id"][:12])
    for band in svc._cms.bands:                # age the only activity proxy
        for e in band.entries:
            if e.episode_id == ep["id"]:
                e.timestamp = _time.time() - 20_000
    svc._episode_touches.clear()               # the seed store touched too
    svc.reap_idle_sessions(7_200)
    assert svc._cms.episodes.episodes[ep["id"]].ended_at is not None
    resumed = svc.episode_start_session("keyX", "session X")   # hook re-fire
    assert resumed["id"] == ep["id"]
    root = svc._cms.episodes.episodes[ep["id"]]
    assert root.ended_at is None               # resumed, not forked
    # Outcome-only return: no episode handle (attributes via the current
    # pointer the resume just moved), so neither a band entry nor a
    # handle-path touch is written — the resume itself must protect.
    svc.record_outcome(task="t", outcome="success")
    svc.reap_idle_sessions(7_200)
    assert root.ended_at is None, "resumed episode must survive the sweep"
    # The touch map is exactly what keeps it alive: clear it and the same
    # sweep re-reaps (the pre-hardening behavior).
    svc._episode_touches.clear()
    svc.reap_idle_sessions(7_200)
    assert root.ended_at is not None


def test_keyless_root_never_resumes_via_handle(pg_service):
    """Post-review hardening (finding 4): the reaper skips keyless roots, so
    resuming one via handle would leave it open forever with no auto-close
    and no end-of-session dream — refuse, warn-and-degrade instead."""
    svc = pg_service
    ep = svc.episode_start_session("keyZ", "session Z")
    svc.store("seed keyZ", source="t", episode=ep["id"][:12])
    root = svc._cms.episodes.episodes[ep["id"]]
    root.session_key = None
    root.ended_at = _time.time()
    res = svc.store("keyless resume", source="t", episode=ep["id"][:12])
    assert res["episode_warning"] == "unknown or closed episode handle"
    assert root.ended_at is not None


# ── `episode` handle on the episode lifecycle tools (spec 2026-08-25) ────────
# Two concurrent Claude Code sessions share one transport connection; only
# the hook-minted handle distinguishes them. The lifecycle tools must accept
# it as an explicit anchor so a per-call assertion beats the global pointer.


def _two_hook_roots(svc):
    a = svc.episode_start_session("hook-key-A", "session A")
    b = svc.episode_start_session("hook-key-B", "session B")
    svc.set_active_session("hook-key-B")   # pointer last written by B's hook
    return a, b


def test_episode_start_with_handle_nests_under_handle_root(pg_service):
    svc = pg_service
    a, b = _two_hook_roots(svc)
    sub = svc.episode_start("A subtask", episode=a["id"][:12])
    assert sub["parent_id"] == a["id"]
    assert sub["session_key"] == "hook-key-A"
    svc.set_active_session(None)


def test_episode_start_without_handle_keeps_pointer_behavior(pg_service):
    svc = pg_service
    a, b = _two_hook_roots(svc)
    sub = svc.episode_start("B subtask")
    assert sub["parent_id"] == b["id"]
    svc.set_active_session(None)


def test_episode_end_with_handle_pops_only_within_subtree(pg_service):
    svc = pg_service
    a, b = _two_hook_roots(svc)
    sub = svc.episode_start("A subtask", episode=a["id"][:12])
    # A handle owning no open sub-leaf is a no-op — never touches A's leaf.
    assert svc.episode_end(episode=b["id"][:12]) == {}
    closed = svc.episode_end(episode=a["id"][:12])
    assert closed.get("id") == sub["id"]
    # The handle never closes the session root itself — that belongs to the
    # hook lifecycle (SessionEnd / idle reaper).
    assert svc.episode_end(episode=a["id"][:12]) == {}
    with svc._lock:
        assert svc._cms.episodes.episodes[a["id"]].ended_at is None
    svc.set_active_session(None)


def test_episode_start_with_bad_handle_warns_and_degrades(pg_service):
    svc = pg_service
    a, b = _two_hook_roots(svc)
    sub = svc.episode_start("subtask", episode="ffffffffffff")
    assert sub["episode_warning"] == "unknown or closed episode handle"
    assert sub["parent_id"] == b["id"]     # degraded to pointer behavior
    svc.set_active_session(None)


def test_episode_end_with_handle_does_not_resurrect_closed_root(pg_service):
    # The resume side effect belongs to WRITE paths (a store must not be
    # lost); a lifecycle end on a closed session must refuse without
    # reopening the root (review finding, 2026-08-25).
    svc = pg_service
    a = svc.episode_start_session("hook-key-R", "session R")
    svc.store("keep me", source="t")           # survives prune-on-empty
    svc.episode_end_session("hook-key-R", run_dream=False)
    res = svc.episode_end(episode=a["id"][:12])
    assert res == {"closed": None,
                   "reason": "unknown or closed episode handle"}
    with svc._lock:
        assert svc._cms.episodes.episodes[a["id"]].ended_at is not None


def test_episode_start_with_handle_stays_inside_handle_subtree(pg_service):
    # Two open roots can share one session_key (a handle-resume while a
    # newer root holds the key): the nest must anchor within the HANDLE's
    # subtree, never the foreign root that open_leaf_for would pick.
    svc = pg_service
    a = svc.episode_start_session("hook-key-S", "session S old")
    svc.store("anchor", source="t", episode=a["id"][:12])
    # Construct the shared-key state directly: A closed long ago (outside
    # the resume window, so start_session mints a NEW root), then reopened
    # the way a handle-resume does — two open roots, one key.
    import time as _t
    with svc._lock:
        svc._cms.episodes.episodes[a["id"]].ended_at = _t.time() - 10 * 86400
    b = svc.episode_start_session("hook-key-S", "session S new")
    assert b["id"] != a["id"]
    with svc._lock:
        svc._cms.episodes.episodes[a["id"]].ended_at = None
    sub = svc.episode_start("A subtask", episode=a["id"][:12])
    with svc._lock:
        em = svc._cms.episodes
        assert em._descends_from(em.get(sub["id"]), a["id"])
        assert not em._descends_from(em.get(sub["id"]), b["id"])


def test_episode_lifecycle_empty_handle_degrades_like_none(pg_service):
    # Claude clients fill optional string params with "" — treat it as
    # "no handle", not as an unknown handle (warn/refuse would be wrong).
    svc = pg_service
    a, b = _two_hook_roots(svc)
    sub = svc.episode_start("task", episode="")
    assert "episode_warning" not in sub
    assert sub["parent_id"] == b["id"]
    closed = svc.episode_end(episode="")
    assert closed.get("id") == sub["id"]
    svc.set_active_session(None)


def test_transport_session_fallback_retired(monkeypatch):
    from pseudolife_memory import writer_context as wc
    monkeypatch.setattr(wc, "_http_request_headers",
                        lambda: {"mcp-session-id": "conn-1"})
    monkeypatch.delenv("PSEUDOLIFE_LEGACY_TRANSPORT_SESSION", raising=False)
    assert wc.resolve_writer_detailed("d") == ("d", None, None)
    monkeypatch.setenv("PSEUDOLIFE_LEGACY_TRANSPORT_SESSION", "1")
    assert wc.resolve_writer_detailed("d") == ("d", None, "conn-1")
