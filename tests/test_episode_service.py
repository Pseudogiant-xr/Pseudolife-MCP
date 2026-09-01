"""Episode service layer: session lifecycle, pruning, the idle reaper, and
the consolidation primitives (rename / merge / resume-on-return / auto-title).

The consolidation half covers the 2026-07-02 episode rework
(docs/superpowers/specs/2026-07-02-episode-consolidation-design.md):
rename-by-id, merge, resume-on-return after an idle-reap, auto-titling
generic sessions at close, the early-sub-episode nesting fix, and the
untitled-session store hint.
"""
from dataclasses import asdict

from pseudolife_memory.memory.episodes import Episode
from pseudolife_memory.storage.postgres import PostgresStorage
from pseudolife_memory.writer_context import (
    reset_writer_context, set_writer_context)
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


def _episodes_by_id(service):
    return {e["id"]: e
            for e in service.episode_list(limit=1000, include_open=True)["episodes"]}


def _titles(service):
    return [e["title"]
            for e in service.episode_list(include_open=True)["episodes"]]


def _session_root(service, session_key):
    roots = [e for e in service.episode_list(limit=1000,
                                             include_open=True)["episodes"]
             if e["session_key"] == session_key and e["parent_id"] is None]
    assert roots, f"no root episode for {session_key}"
    return roots[0]


def _store_in_session(service, session_key, text, source="claude"):
    tok = set_writer_context("w", session_key)
    try:
        return service.store(text, source=source)
    finally:
        reset_writer_context(tok)


def test_episode_session_key_round_trips(pg_conn, pg_url):
    storage = PostgresStorage(pg_url)
    ep = Episode(id="e1", title="S", started_at=1.0, session_key="sess-xyz")
    storage.upsert_episode(asdict(ep))           # episode_row(ep) == asdict(ep)
    rows = {r["id"]: r for r in storage.load_episodes()}
    assert rows["e1"]["session_key"] == "sess-xyz"


def test_session_start_is_idempotent_per_key(pristine_service):
    service = pristine_service
    a = service.episode_start_session("sess-1", "Session A")
    b = service.episode_start_session("sess-1", "Session A")   # re-fire
    assert a["id"] == b["id"]                                   # no second episode


def test_session_end_matches_key_only(pristine_service):
    service = pristine_service
    service.episode_start_session("sess-1", "Session A")
    # Give the session an entry so prune-on-empty-close doesn't delete it; this
    # test is about key matching, not the empty-prune path.
    service.store("a durable fact so sess-1 survives close")
    assert service.episode_end_session("other", run_dream=False) == {}   # no-op
    closed = service.episode_end_session("sess-1", run_dream=False)
    assert closed and closed["ended_at"] is not None
    assert service.episode_end_session("sess-1", run_dream=False) == {}   # nothing open


# ── Prune-on-empty-close + no-clobber (v2) ────────────────────────────────────


def test_empty_session_is_pruned_on_close(pristine_service):
    service = pristine_service
    service.episode_start_session("S1", "empty-proj")
    service.episode_end_session("S1", run_dream=False)   # nothing stored
    titles = _titles(service)
    assert "empty-proj" not in titles                    # empty husk deleted


def test_nonempty_session_survives_close(pristine_service):
    service = pristine_service
    service.episode_start_session("S2", "real-proj")
    service.store("durable work in S2")                  # stamps the open leaf
    service.episode_end_session("S2", run_dream=False)
    titles = _titles(service)
    assert "real-proj" in titles


def test_two_sessions_start_without_clobber(pristine_service):
    service = pristine_service
    a = service.episode_start_session("A", "proj-a")
    b = service.episode_start_session("B", "proj-b")
    assert a["ended_at"] is None and b["ended_at"] is None


def test_prune_empty_deletes_only_entryless_closed(pristine_service):
    service = pristine_service
    service.episode_start_session("KEEP", "has-entry")
    service.store("durable")                       # stamps the open leaf (KEEP)
    service.episode_end_session("KEEP", run_dream=False)   # survives (has entry)
    # A closed, entry-less husk (closed directly so prune-on-close doesn't run):
    service.episode_start_session("DROP", "empty")
    service._cms.episodes.end_session("DROP")
    out = service.episode_prune_empty(include_open=False)
    titles = _titles(service)
    assert "has-entry" in titles
    assert "empty" not in titles
    assert out["deleted"] >= 1


def test_prune_empty_keeps_open_session_by_default(pristine_service):
    service = pristine_service
    service.episode_start_session("OPEN", "live-open")    # open, 0 entries
    service.episode_prune_empty(include_open=False)
    titles = _titles(service)
    assert "live-open" in titles                          # not deleted while open


def test_episode_rest_start_and_end(pristine_service):
    from pseudolife_memory.web.routes import ConsoleRoutes
    service = pristine_service
    routes = ConsoleRoutes(service)
    started = routes.dispatch("POST", "/api/episode/start", {},
                              {"session_key": "s1", "title": "Sess"})
    assert started["session_key"] == "s1"
    service.store("a durable fact so the session is not pruned on close")
    ended = routes.dispatch("POST", "/api/episode/end", {},
                            {"session_key": "s1", "run_dream": False})
    assert ended["ended_at"] is not None


def test_agent_episode_nests_under_session(pristine_service):
    service = pristine_service
    service.episode_start_session("s1", "Session")
    sub = service.episode_start("Big task")           # agent sub-episode
    assert sub["parent_id"] is not None
    # storing now stamps the sub-episode (the leaf)
    service.store("did a thing")  # returns entry/ack; episode stamped internally
    closed = service.episode_end_session("s1", run_dream=False)
    assert closed and closed["parent_id"] is None      # the root was closed


def test_search_episode_filter_includes_child_episodes(pristine_service):
    service = pristine_service
    root = service.episode_start_session("s1", "Session")
    sub = service.episode_start("Sub")                       # nests under root
    service.store("alpha beta gamma", source="pseudolife")   # stamped to sub
    hits = service.search("alpha beta gamma", episodes=[root["id"]])
    texts = [e["text"] for e in hits.get("entries", [])]
    assert any("alpha beta gamma" in t for t in texts)


# ── Idle reaper (direct-http has no session-end signal) ───────────────────────


def test_reap_idle_closes_nonempty_session_keeps_it(pristine_service):
    service = pristine_service
    tok = set_writer_context("w", "SESS-IDLE")
    try:
        service.store("did real work in this session")  # lazy-opens + stamps
    finally:
        reset_writer_context(tok)
    # Not idle yet -> nothing reaped.
    assert service.reap_idle_sessions(idle_seconds=3600)["reaped"] == 0
    # Force idle (now far in the future) -> closed but NOT pruned (has an entry).
    out = service.reap_idle_sessions(idle_seconds=0, now=9e12)
    assert out["reaped"] == 1
    mine = [e for e in service.episode_list(include_open=True)["episodes"]
            if e["session_key"] == "SESS-IDLE"]
    assert len(mine) == 1 and mine[0]["ended_at"] is not None


def test_reap_far_idle_sweeps_empty_session(pristine_service):
    service = pristine_service
    service.episode_start_session("SESS-EMPTY", "manual-empty")  # open, 0 entries
    out = service.reap_idle_sessions(idle_seconds=0, now=9e12)
    assert out["reaped"] == 1
    titles = _titles(service)
    # now=9e12 is far past the retention window, so the same pass sweeps it
    assert "manual-empty" not in titles


def test_reap_defers_empty_prune_within_retention(pristine_service):
    """An empty root closed by the reaper is KEPT while inside the session
    resume window — deleting it immediately orphaned the session's briefing
    handle (2026-09-02 incident); a later pass sweeps it once stale."""
    import time as _t
    service = pristine_service
    service.episode_start_session("SESS-DEFER", "empty-deferred")
    out = service.reap_idle_sessions(idle_seconds=0, now=_t.time() + 10_000)
    assert out["reaped"] == 1
    assert "empty-deferred" in _titles(service)   # closed but not yet deleted


def test_reap_sweeps_stale_empty_root_and_reports(pristine_service):
    import time as _t
    service = pristine_service
    service.episode_start_session("SESS-SWEEP", "empty-swept")
    service.reap_idle_sessions(idle_seconds=0, now=_t.time() + 10_000)
    out = service.reap_idle_sessions(idle_seconds=0, now=_t.time() + 30_000)
    assert out["swept"] == 1
    assert "empty-swept" not in _titles(service)


def test_sweep_ignores_roots_that_lost_entries_after_close(pristine_service):
    """A root that was NON-empty at close must never be swept, even when its
    entries later vanish — the flat preset's capacity eviction is a true
    drop, and forget deletes rows, so "zero live band entries" does not mean
    "captured nothing". The sweep targets only roots the reaper closed
    empty, never history whose memories aged out (review finding,
    2026-09-02)."""
    import time as _t
    service = pristine_service
    _store_in_session(service, "SESS-HIST", "durable work, later evicted")
    root = _session_root(service, "SESS-HIST")
    service.reap_idle_sessions(idle_seconds=0, now=_t.time() + 10_000)
    for band in service._cms.bands:      # simulate eviction / forget
        band.entries[:] = [e for e in band.entries
                           if e.episode_id != root["id"]]
    out = service.reap_idle_sessions(idle_seconds=0, now=_t.time() + 30_000)
    assert out["swept"] == 0
    assert root["id"] in service._cms.episodes.episodes   # history survives


def test_reap_ignores_already_closed(pristine_service):
    service = pristine_service
    service.episode_start_session("SESS-DONE", "done")
    service._cms.episodes.end_session("SESS-DONE")  # already closed
    assert service.reap_idle_sessions(idle_seconds=0, now=9e12)["reaped"] == 0


# ── episode_rename ────────────────────────────────────────────────────────────


def test_rename_updates_episode_and_entry_stamps(pristine_service):
    service = pristine_service
    service.episode_start_session("R1", "old-name")
    service.store("work stamped under the old title")
    service.episode_end_session("R1", run_dream=False)
    ep = _session_root(service, "R1")
    out = service.episode_rename(ep["id"], "new-name")
    assert out["ok"] and out["title"] == "new-name"
    assert _episodes_by_id(service)[ep["id"]]["title"] == "new-name"
    entries = service.recent(n=10)["entries"]
    stamped = [e for e in entries if e["episode_id"] == ep["id"]]
    assert stamped and all(e["episode_title"] == "new-name" for e in stamped)


def test_rename_unknown_id_fails(pristine_service):
    out = pristine_service.episode_rename("nope", "anything")
    assert out["ok"] is False


# ── episode_merge ─────────────────────────────────────────────────────────────


def test_merge_into_new_episode_repoints_entries_and_deletes_sources(
        pristine_service):
    service = pristine_service
    service.episode_start_session("M1", "frag one")
    service.store("first fragment work")
    service.episode_end_session("M1", run_dream=False)
    service.episode_start_session("M2", "frag two")
    service.store("second fragment work")
    service.episode_end_session("M2", run_dream=False)
    a, b = _session_root(service, "M1"), _session_root(service, "M2")

    out = service.episode_merge([a["id"], b["id"]], title="Project - day")
    assert out["ok"] and out["entries_moved"] == 2
    eps = _episodes_by_id(service)
    assert a["id"] not in eps and b["id"] not in eps
    target = eps[out["id"]]
    assert target["title"] == "Project - day"
    assert target["entry_count"] == 2
    # span covers both sources; the rollup is closed
    assert target["started_at"] <= a["started_at"]
    assert target["ended_at"] is not None
    entries = service.recent(n=10)["entries"]
    moved = [e for e in entries if e["episode_id"] == out["id"]]
    assert len(moved) == 2
    assert all(e["episode_title"] == "Project - day" for e in moved)


def test_merge_into_existing_target_widens_span(pristine_service):
    service = pristine_service
    service.episode_start_session("T", "target")
    service.store("target work")
    service.episode_end_session("T", run_dream=False)
    service.episode_start_session("S", "source")
    service.store("source work")
    service.episode_end_session("S", run_dream=False)
    target, source = _session_root(service, "T"), _session_root(service, "S")

    out = service.episode_merge([source["id"]], into=target["id"])
    assert out["ok"] and out["id"] == target["id"]
    eps = _episodes_by_id(service)
    assert source["id"] not in eps
    assert eps[target["id"]]["entry_count"] == 2
    assert eps[target["id"]]["ended_at"] >= source["ended_at"]


def test_merge_skips_open_sources(pristine_service):
    service = pristine_service
    service.episode_start_session("OPEN", "still running")
    service.store("live session work")
    out = pristine_service.episode_merge(
        [_session_root(service, "OPEN")["id"]], title="rollup")
    assert out["ok"] is False
    assert _session_root(service, "OPEN")["id"] in out["skipped_open"]


def test_merge_reparents_children(pristine_service):
    service = pristine_service
    service.episode_start_session("P", "parent session")
    child = service.episode_start("named sub-task")
    service.store("sub-task work")           # stamps the child leaf
    service.episode_end_session("P", run_dream=False)
    root = _session_root(service, "P")

    out = service.episode_merge([root["id"]], title="rollup")
    assert out["ok"]
    eps = _episodes_by_id(service)
    assert eps[child["id"]]["parent_id"] == out["id"]


def test_merge_requires_title_for_new_target(pristine_service):
    service = pristine_service
    service.episode_start_session("X", "x")
    service.store("x work")
    service.episode_end_session("X", run_dream=False)
    out = service.episode_merge([_session_root(service, "X")["id"]])
    assert out["ok"] is False


# ── resume-on-return (reaper-husk fix) ───────────────────────────────────────


def test_store_resumes_recently_closed_session_episode(pristine_service):
    service = pristine_service
    _store_in_session(service, "SESS-R", "work before the idle gap")
    first = _session_root(service, "SESS-R")
    service.reap_idle_sessions(idle_seconds=0, now=9e12)   # reaper closes it
    _store_in_session(service, "SESS-R", "work after coming back")
    roots = [e for e in _episodes_by_id(service).values()
             if e["session_key"] == "SESS-R" and e["parent_id"] is None]
    assert len(roots) == 1                       # no husk: same episode resumed
    assert roots[0]["id"] == first["id"]
    assert roots[0]["ended_at"] is None          # reopened


def test_store_opens_fresh_episode_outside_resume_window(
        pristine_service, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_SESSION_RESUME_SECONDS", "0")
    service = pristine_service
    _store_in_session(service, "SESS-W", "work before the long gap")
    first = _session_root(service, "SESS-W")
    service.reap_idle_sessions(idle_seconds=0, now=9e12)
    _store_in_session(service, "SESS-W", "work after days away")
    roots = [e for e in _episodes_by_id(service).values()
             if e["session_key"] == "SESS-W" and e["parent_id"] is None]
    assert len(roots) == 2                       # window 0 -> never resume
    assert any(e["id"] != first["id"] and e["ended_at"] is None for e in roots)


def test_episode_start_session_resumes_recently_reaped_root(pristine_service):
    """A SessionStart re-fire (source=resume/compact) after the idle reaper
    closed the root must resume it, not fork a second episode for the same
    session (finding 5, 2026-07-19). The store path already resumed via
    ``_ensure_session_episode``; the hook path (``episode_start_session``)
    did not, so a long session that got reaped then re-fired the hook
    fragmented into two roots."""
    service = pristine_service
    opened = service.episode_start_session("SESS-H", "session - 2026-07-19 10:00")
    _store_in_session(service, "SESS-H", "work before the idle gap")
    service.reap_idle_sessions(idle_seconds=0, now=9e12)   # reaper closes it
    reopened = service.episode_start_session("SESS-H", "session - 2026-07-19 16:00")
    roots = [e for e in _episodes_by_id(service).values()
             if e["session_key"] == "SESS-H" and e["parent_id"] is None]
    assert len(roots) == 1                       # resumed, not forked
    assert reopened["id"] == opened["id"]
    assert roots[0]["ended_at"] is None          # reopened


def test_episode_start_session_forks_outside_resume_window(
        pristine_service, monkeypatch):
    """With the resume window disabled the hook path opens a fresh root, same
    as the store path — the resume behavior is one shared window."""
    monkeypatch.setenv("PSEUDOLIFE_SESSION_RESUME_SECONDS", "0")
    service = pristine_service
    opened = service.episode_start_session("SESS-HF", "session - 2026-07-19 10:00")
    _store_in_session(service, "SESS-HF", "work before the long gap")
    service.reap_idle_sessions(idle_seconds=0, now=9e12)
    reopened = service.episode_start_session("SESS-HF", "session - 2026-07-19 22:00")
    roots = [e for e in _episodes_by_id(service).values()
             if e["session_key"] == "SESS-HF" and e["parent_id"] is None]
    assert len(roots) == 2                       # window 0 -> never resume
    assert reopened["id"] != opened["id"]


# ── auto-title on close ──────────────────────────────────────────────────────


def test_close_derives_title_for_generic_session(pristine_service):
    service = pristine_service
    _store_in_session(service, "SESS-G",
                      "Shipped the frobnicator refactor and deployed it live",
                      source="pseudolife")
    root = _session_root(service, "SESS-G")
    assert root["title"].startswith("session - ")     # lazy-open generic title
    service.episode_end_session("SESS-G", run_dream=False)
    closed = _episodes_by_id(service)[root["id"]]
    assert closed["title"].startswith("pseudolife - ")
    assert "Shipped the frobnicator" in closed["title"]
    # denormalised entry stamps follow the derived title
    entries = [e for e in service.recent(n=10)["entries"]
               if e["episode_id"] == root["id"]]
    assert entries and all(e["episode_title"] == closed["title"]
                           for e in entries)


def test_close_keeps_agent_set_title(pristine_service):
    service = pristine_service
    tok = set_writer_context("w", "SESS-N")
    try:
        service.store("some work", source="pseudolife")
        service.set_session_title("MyProject - big refactor")
    finally:
        reset_writer_context(tok)
    service.episode_end_session("SESS-N", run_dream=False)
    root = _session_root(service, "SESS-N")
    assert root["title"] == "MyProject - big refactor"


def test_reaper_also_derives_title(pristine_service):
    service = pristine_service
    _store_in_session(service, "SESS-RT", "Investigated the flaky test",
                      source="pseudolife")
    root = _session_root(service, "SESS-RT")
    service.reap_idle_sessions(idle_seconds=0, now=9e12)
    closed = _episodes_by_id(service)[root["id"]]
    assert closed["title"].startswith("pseudolife - ")


def test_derived_title_prefers_non_noise_source(pristine_service):
    service = pristine_service
    _store_in_session(service, "SESS-S", "progress log line one",
                      source="status")
    _store_in_session(service, "SESS-S", "the actual durable finding",
                      source="pseudolife")
    service.episode_end_session("SESS-S", run_dream=False)
    root = _session_root(service, "SESS-S")
    assert root["title"].startswith("pseudolife - ")


# ── early sub-episode nesting + untitled hint ────────────────────────────────


def test_early_sub_episode_nests_under_lazy_session_root(pristine_service):
    service = pristine_service
    tok = set_writer_context("w", "SESS-E")
    try:
        sub = service.episode_start("Named task before any store")
    finally:
        reset_writer_context(tok)
    assert sub["parent_id"] is not None
    root = _episodes_by_id(service)[sub["parent_id"]]
    assert root["session_key"] == "SESS-E" and root["parent_id"] is None


def test_store_hints_untitled_session(pristine_service):
    service = pristine_service
    out = _store_in_session(service, "SESS-H", "durable work item")
    assert "memory_session_title" in out.get("episode_hint", "")
    tok = set_writer_context("w", "SESS-H")
    try:
        service.set_session_title("MyProject - named now")
        out2 = service.store("more durable work")
    finally:
        reset_writer_context(tok)
    assert "episode_hint" not in out2
