"""Episode consolidation primitives + anti-fragmentation behavior.

Covers the 2026-07-02 episode rework (docs/superpowers/specs/
2026-07-02-episode-consolidation-design.md): rename-by-id, merge,
resume-on-return after an idle-reap, auto-titling generic sessions at close,
the early-sub-episode nesting fix, and the untitled-session store hint.
"""

import pytest

from pseudolife_memory.writer_context import (
    reset_writer_context, set_writer_context)


def _episodes_by_id(service):
    return {e["id"]: e
            for e in service.episode_list(limit=1000, include_open=True)["episodes"]}


def _store_in_session(service, session_key, text, source="claude"):
    tok = set_writer_context("w", session_key)
    try:
        return service.store(text, source=source)
    finally:
        reset_writer_context(tok)


def _session_root(service, session_key):
    roots = [e for e in service.episode_list(limit=1000,
                                             include_open=True)["episodes"]
             if e["session_key"] == session_key and e["parent_id"] is None]
    assert roots, f"no root episode for {session_key}"
    return roots[0]


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
