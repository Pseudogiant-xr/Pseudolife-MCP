"""Every registered client session leaves a durable ``client_sessions`` row.

A session root is DELETED when it ends holding no band entry (prune-on-empty
at SessionEnd or shim exit; the idle reaper defers and later sweeps the same
roots), and until schema v43 nothing else recorded that the session had
existed. A session that only searched, set facts or logged outcomes left
retrieval_events / outcome_signals rows whose ids named nothing: on the live
bank on 2026-09-25 a 24 h window had 43 surviving client sessions beside 94
pruned sessions with searches and 67 with outcomes (``evals/
capture_metrics.py``), and the memory-policy online A/B could log its arm
but not keep it. The registration row is written where both registration
paths meet (``episode_start_session``: the SessionStart hook and the stdio
shim's ``POST /api/episode/start``) and is never pruned.
"""
from __future__ import annotations

import json
import re
import time

import pytest

from pseudolife_memory.utils.config import MemoryPolicyConfig
from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.session_hook import (
    hook_session_end, hook_session_start, memory_policy_variant)
from tests.asgi_helpers import call, stub_mcp
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401  (fixtures)

HOOK = "0f1e2d3c-1111-4222-8333-444455556666"
SHIM = "9a8b7c6d5e4f30211203f4e5d6c7b8a9"
_COLS = ("registered_via", "principal", "started_at", "ended_at",
         "end_reason", "policy_variant", "episode_ids", "start_times")


def _row(conn, key):
    r = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM client_sessions WHERE session_key = %s",
        (key,)).fetchone()
    return None if r is None else dict(zip(_COLS, r))


def _handle(text):
    return re.search(r'episode="([0-9a-f]+)"', text).group(1)


def test_hook_registration_writes_a_session_record(pg_service, pg_conn):
    before = time.time()
    text = hook_session_start(pg_service, session_id=HOOK, source="startup")
    row = _row(pg_conn, HOOK)
    assert row is not None
    assert row["registered_via"] == "hook"
    assert before - 1 <= row["started_at"] <= time.time() + 1
    assert row["ended_at"] is None and row["end_reason"] is None
    # The default policy (no A/B arms configured) is what was served.
    assert row["policy_variant"] == "compact"
    # The root the briefing advertised is named, so an outcome stamped with
    # it stays attributable after the root itself is gone.
    assert len(row["episode_ids"]) == 1
    assert row["episode_ids"][0].startswith(_handle(text))
    # In-process call: no request, so no principal is claimed.
    assert row["principal"] is None


def test_record_survives_prune_on_empty_end(pg_service, pg_conn):
    hook_session_start(pg_service, session_id=HOOK, source="startup")
    root_id = _row(pg_conn, HOOK)["episode_ids"][0]
    pg_service.search("a read-only session")
    assert hook_session_end(pg_service, session_id=HOOK) == {"ok": True}
    # The empty root is pruned, exactly as before ...
    assert pg_conn.execute("SELECT 1 FROM episodes WHERE id = %s",
                           (root_id,)).fetchone() is None
    # ... and the session is still on record, ended.
    row = _row(pg_conn, HOOK)
    assert row["ended_at"] is not None and row["end_reason"] == "end"
    assert row["episode_ids"] == [root_id]


def test_refire_keeps_first_start_and_reopens(pg_service, pg_conn):
    hook_session_start(pg_service, session_id=HOOK, source="startup")
    first = _row(pg_conn, HOOK)
    hook_session_end(pg_service, session_id=HOOK)          # pruned empty
    # A resumed transcript re-registers the same session id; its old root
    # is gone, so it gets a new one.
    hook_session_start(pg_service, session_id=HOOK, source="resume")
    row = _row(pg_conn, HOOK)
    assert row["started_at"] == first["started_at"]
    assert row["ended_at"] is None and row["end_reason"] is None
    assert len(row["episode_ids"]) == 2
    assert row["episode_ids"][0] == first["episode_ids"][0]
    # Every registration's time is kept: a resumed client starts a new shim
    # near the LATER one, and pairing the two needs it.
    assert first["start_times"] == [first["started_at"]]
    assert len(row["start_times"]) == 2
    assert row["start_times"][0] == first["started_at"] <= row["start_times"][1]
    # A compact re-fire on the still-open root adds no episode.
    hook_session_start(pg_service, session_id=HOOK, source="compact")
    assert _row(pg_conn, HOOK)["episode_ids"] == row["episode_ids"]


def test_api_registration_is_recorded_without_a_variant(pg_service, pg_conn):
    """The stdio shim registers through ``POST /api/episode/start``: its row
    is what lets a search carrying the shim's key be attributed after the
    shim's root is pruned. No policy text is served on that path."""
    ep = pg_service.episode_start_session(SHIM, "PseudoLife-MCP - 2026-09-25 10:00")
    row = _row(pg_conn, SHIM)
    assert row["registered_via"] == "api"
    assert row["policy_variant"] is None
    assert row["episode_ids"] == [ep["id"]]


def test_idle_close_is_stamped_idle_then_an_explicit_end_wins(pg_service, pg_conn):
    pg_service.episode_start_session(SHIM, "t")
    pg_service.reap_idle_sessions(idle_seconds=0, now=time.time() + 10)
    row = _row(pg_conn, SHIM)
    assert row["end_reason"] == "idle" and row["ended_at"] is not None
    # The reaped session comes back (key resume) and then ends for real.
    pg_service.episode_start_session(SHIM, "t")
    assert _row(pg_conn, SHIM)["ended_at"] is None
    pg_service.episode_end_session(SHIM)
    assert _row(pg_conn, SHIM)["end_reason"] == "end"


def test_ending_an_unregistered_session_writes_nothing(pg_service, pg_conn):
    """A root the daemon opened lazily (first store carrying a session key,
    no registration) is not a registered session; closing it must not
    invent a row."""
    with pg_service._lock:
        pg_service._ensure_session_episode("lazy-key-1")
    pg_service.episode_end_session("lazy-key-1")
    assert _row(pg_conn, "lazy-key-1") is None


def test_principal_is_recorded_from_the_bearer(pg_service, pg_conn, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tok-a:codex")
    app = build_console_app(stub_mcp, None, lambda: {"status": "ok"},
                            pg_service, token_map={"tok-a": "codex"})
    auth = [(b"authorization", b"Bearer tok-a")]
    st, _ = call(app, "GET", "/api/hook/session-start", headers=auth,
                 query=f"session_id={HOOK}&source=startup")
    assert st == 200
    st, _ = call(app, "POST", "/api/episode/start",
                 headers=auth + [(b"content-type", b"application/json")],
                 body=json.dumps({"session_key": SHIM, "title": "x"}).encode())
    assert st == 200
    assert _row(pg_conn, HOOK)["principal"] == "codex"
    assert _row(pg_conn, SHIM)["principal"] == "codex"


def test_ab_arm_is_recorded_per_session(pg_service, pg_conn):
    pg_service.config.memory_policy = MemoryPolicyConfig(
        ab_arms=["none", "compact"])
    keys = [f"{n:08x}-1111-4222-8333-444455556666" for n in range(8)]
    for key in keys:
        hook_session_start(pg_service, session_id=key, source="startup")
    served = {key: _row(pg_conn, key)["policy_variant"] for key in keys}
    assert served == {key: memory_policy_variant(pg_service, key) for key in keys}
    assert set(served.values()) == {"none", "compact"}


def test_registration_failure_never_breaks_session_start(pg_service, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("storage down")
    monkeypatch.setattr(pg_service._storage, "register_client_session", boom)
    monkeypatch.setattr(pg_service._storage, "end_client_session", boom)
    text = hook_session_start(pg_service, session_id=HOOK, source="startup")
    assert "Session episode:" in text
    assert hook_session_end(pg_service, session_id=HOOK) == {"ok": True}
    with pg_service._lock:
        assert not any(e.session_key == HOOK
                       for e in pg_service._cms.episodes.episodes.values())


@pytest.mark.parametrize("key", [None, ""])
def test_keyless_start_registers_nothing(pg_service, pg_conn, key):
    pg_service.episode_start_session(key, "session")
    assert pg_conn.execute("SELECT count(*) FROM client_sessions").fetchone()[0] == 0


# ── the record follows every lifecycle path ─────────────────────────────────


def test_refire_under_an_open_sub_episode_names_the_root(pg_service, pg_conn):
    text = hook_session_start(pg_service, session_id=HOOK, source="startup")
    root_id = _row(pg_conn, HOOK)["episode_ids"][0]
    sub = pg_service.episode_start("a nested task", episode=_handle(text))
    assert sub["parent_id"] == root_id
    # The re-fire finds the open LEAF (the sub-episode); the record must
    # still name the session's root.
    hook_session_start(pg_service, session_id=HOOK, source="compact")
    assert _row(pg_conn, HOOK)["episode_ids"] == [root_id]


def test_a_lazy_root_for_a_registered_key_is_added_at_close(pg_service, pg_conn):
    first = pg_service.episode_start_session(SHIM, "t")
    pg_service.episode_end_session(SHIM)                  # pruned empty
    with pg_service._lock:                                # a later store's lazy open
        lazy = pg_service._ensure_session_episode(SHIM)
    assert lazy and lazy != first["id"]
    pg_service.episode_end_session(SHIM)
    assert _row(pg_conn, SHIM)["episode_ids"] == [first["id"], lazy]


def test_session_end_after_an_idle_close_is_stamped_end(pg_service, pg_conn):
    """The reaper closed the session during a break and nothing reopened
    it; the client's own SessionEnd is still the session's real end."""
    pg_service.episode_start_session(SHIM, "t")
    pg_service.reap_idle_sessions(idle_seconds=0, now=time.time() + 10)
    idle = _row(pg_conn, SHIM)
    assert idle["end_reason"] == "idle"
    pg_service.episode_end_session(SHIM)                  # nothing open to close
    row = _row(pg_conn, SHIM)
    assert row["end_reason"] == "end" and row["ended_at"] >= idle["ended_at"]


def test_store_resume_after_an_idle_close_reopens_the_record(pg_service, pg_conn):
    from pseudolife_memory.writer_context import (
        reset_writer_context, set_writer_context)
    pg_service.episode_start_session(SHIM, "t")
    pg_service.reap_idle_sessions(idle_seconds=0, now=time.time() + 10)
    assert _row(pg_conn, SHIM)["ended_at"] is not None
    token = set_writer_context("w", SHIM)                 # the shim's own key
    try:
        pg_service.store("back from the break", source="t")
    finally:
        reset_writer_context(token)
    row = _row(pg_conn, SHIM)
    assert row["ended_at"] is None and row["end_reason"] is None


def test_handle_resume_after_an_idle_close_reopens_the_record(pg_service, pg_conn):
    text = hook_session_start(pg_service, session_id=HOOK, source="startup")
    pg_service.reap_idle_sessions(idle_seconds=0, now=time.time() + 10)
    assert _row(pg_conn, HOOK)["end_reason"] == "idle"
    res = pg_service.store("resumed by handle", source="t", episode=_handle(text))
    assert "episode_warning" not in res
    row = _row(pg_conn, HOOK)
    assert row["ended_at"] is None and row["end_reason"] is None


def test_tombstone_recreation_reopens_the_record(pg_service, pg_conn):
    text = hook_session_start(pg_service, session_id=HOOK, source="startup")
    root_id = _row(pg_conn, HOOK)["episode_ids"][0]
    pg_service.reap_idle_sessions(idle_seconds=0, now=time.time() + 10_000)
    out = pg_service.reap_idle_sessions(idle_seconds=0, now=time.time() + 30_000)
    assert out["swept"] == 1
    assert _row(pg_conn, HOOK)["ended_at"] is not None
    pg_service.store("after a long break", source="t", episode=_handle(text))
    row = _row(pg_conn, HOOK)
    assert row["ended_at"] is None and row["episode_ids"] == [root_id]
