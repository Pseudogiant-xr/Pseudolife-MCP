"""Retrieval event log (schema v31) — storage round-trips + service wiring.

Storage half: add_retrieval_event / record_retrieval_use /
prune_retrieval_events / retrieval_events_window against a live PG.
Service half: search() writes an event, get_entry()/reinforce() write
implicit use labels, the config kill-switch silences both.

Skips cleanly without a PG server (mirrors test_lessons_storage.py).
"""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    s = PostgresStorage(pg_url)
    yield s
    s.close()


def _served(*entry_ids: int) -> list[dict]:
    return [
        {"entry_id": eid, "score": 0.9 - 0.1 * rank, "rank": rank,
         "via": None, "bank": "flat"}
        for rank, eid in enumerate(entry_ids)
    ]


def test_add_event_round_trips(storage):
    eid = storage.add_retrieval_event(
        "how do I deploy", _served(7, 9), session_id="s-1",
        episode_id="ep-1", now=1000.0)
    assert eid > 0
    events = storage.retrieval_events_window()
    assert len(events) == 1
    ev = events[0]
    assert ev["query_text"] == "how do I deploy"
    assert ev["session_id"] == "s-1"
    assert ev["episode_id"] == "ep-1"
    assert [s["entry_id"] for s in ev["served"]] == [7, 9]
    assert ev["uses"] == []


def test_use_labels_most_recent_serving_event(storage):
    old = storage.add_retrieval_event("q1", _served(7), session_id="s-1",
                                      now=1000.0)
    new = storage.add_retrieval_event("q2", _served(7, 8), session_id="s-1",
                                      now=1050.0)
    wrote = storage.record_retrieval_use(7, "s-1", "get",
                                         window_s=3600, now=1100.0)
    assert wrote == 1
    events = {e["id"]: e for e in storage.retrieval_events_window()}
    assert events[new]["uses"] and events[new]["uses"][0]["entry_id"] == 7
    assert events[old]["uses"] == []


def test_use_window_and_session_are_strict(storage):
    storage.add_retrieval_event("q", _served(7), session_id="s-1", now=1000.0)
    # Outside the window: no label.
    assert storage.record_retrieval_use(7, "s-1", "get",
                                        window_s=60, now=2000.0) == 0
    # Wrong session: no label.
    assert storage.record_retrieval_use(7, "s-2", "get",
                                        window_s=3600, now=1100.0) == 0
    # None session only matches None-session events.
    assert storage.record_retrieval_use(7, None, "get",
                                        window_s=3600, now=1100.0) == 0
    storage.add_retrieval_event("q2", _served(7), session_id=None, now=1200.0)
    assert storage.record_retrieval_use(7, None, "get",
                                        window_s=3600, now=1210.0) == 1


def test_use_is_idempotent_per_via(storage):
    storage.add_retrieval_event("q", _served(7), session_id="s-1", now=1000.0)
    assert storage.record_retrieval_use(7, "s-1", "get", 3600, now=1010.0) == 1
    assert storage.record_retrieval_use(7, "s-1", "get", 3600, now=1020.0) == 0
    # A different via is a distinct label.
    assert storage.record_retrieval_use(7, "s-1", "reinforce", 3600,
                                        now=1030.0) == 1


def test_prune_cascades_uses(storage, pg_conn):
    storage.add_retrieval_event("old", _served(7), session_id="s-1",
                                now=1000.0)
    storage.record_retrieval_use(7, "s-1", "get", 3600, now=1010.0)
    storage.add_retrieval_event("new", _served(8), session_id="s-1",
                                now=5000.0)
    assert storage.prune_retrieval_events(2000.0) == 1
    events = storage.retrieval_events_window()
    assert [e["query_text"] for e in events] == ["new"]
    n_uses = pg_conn.execute(
        "SELECT COUNT(*) FROM retrieval_uses").fetchone()[0]
    assert n_uses == 0


def test_service_search_logs_and_get_reinforce_label(pg_conn, pg_url,
                                                     tmp_path):
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.store("the quick brown fox jumps over the lazy dog", source="test")
    before = len(svc._storage.retrieval_events_window())
    res = svc.search("the quick brown fox jumps over the lazy dog")
    assert res["count"] >= 1
    events = svc._storage.retrieval_events_window()
    assert len(events) == before + 1
    ev = events[-1]
    served_ids = [s["entry_id"] for s in ev["served"]]
    entry_id = res["entries"][0]["id"]
    assert entry_id in served_ids
    assert ev["served"][0]["rank"] == 0

    svc.get_entry(entry_id)
    svc.reinforce(entry_id)
    ev = svc._storage.retrieval_events_window()[-1]
    vias = {(u["entry_id"], u["used_via"]) for u in ev["uses"]}
    assert (entry_id, "get") in vias
    assert (entry_id, "reinforce") in vias

    # Kill-switch: disabled config logs no event and no label.
    svc.config.memory.retrieval_log.enabled = False
    svc.search("the quick brown fox jumps over the lazy dog")
    svc.get_entry(entry_id)
    after = svc._storage.retrieval_events_window()
    assert len(after) == before + 1

    # Pruning honours retention_days.
    svc.config.memory.retrieval_log.retention_days = 0
    assert svc.prune_retrieval_log() >= 1
    assert svc._storage.retrieval_events_window() == []


def test_prune_retrieval_log_holds_service_lock(pg_conn, pg_url, tmp_path):
    """prune_retrieval_events opens a psycopg transaction block on the shared
    connection; the dream-sweep thread calls prune_retrieval_log concurrently
    with lock-holding writers, so an unlocked call can interleave transaction
    blocks and wedge the connection INTRANS (2026-08-21 daemon incident:
    "transaction commit at the wrong nesting level"). The service lock must
    be held around the storage call, as prune_dream_runs does."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    with svc._lock:
        svc._ensure_init()
    assert svc._storage is not None
    seen: dict[str, bool] = {}

    def _probe(cutoff: float) -> int:
        seen["locked"] = svc._lock.locked()
        return 0

    svc._storage.prune_retrieval_events = _probe  # type: ignore[method-assign]
    svc.prune_retrieval_log()
    assert seen.get("locked"), \
        "prune_retrieval_log must call storage under self._lock"
