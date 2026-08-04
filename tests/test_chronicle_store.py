"""Chronicle-event storage CRUD (schema v28).

Additive-only records: writes insert or exact-dedup — nothing updates a
stored event except ``invalidated_at`` (contradiction handling) and the
rollback delete (safe precisely because records are additive-only).
Serving order is ``occurred_at ASC NULLS LAST`` so undated phrase-only
events trail dated ones. Design:
docs/superpowers/specs/2026-08-03-aggregation-aware-recall-design.md
(Phase 2 + 2026-08-04 amendment).
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401 (fixtures)
from pseudolife_memory.storage.postgres import PostgresStorage


@pytest.fixture()
def storage(pg_conn, pg_url):
    s = PostgresStorage(pg_url)
    yield s
    s.close()


def _event(**over):
    row = {"occurred_at": "2023-05-14", "occurred_phrase": "on May 14",
           "recorded_at": 100.0, "actor": "User", "actor_norm": "user",
           "description": "adopted a kitten",
           "description_norm": "adopted a kitten",
           "episode": None, "src_entry_id": None, "writer_id": "test"}
    row.update(over)
    return row


def test_add_inserts_and_returns_id(storage):
    ev_id, action = storage.add_chronicle_event(_event())
    assert action == "inserted" and isinstance(ev_id, int)


def test_add_exact_duplicate_dedupes(storage):
    ev_id, _ = storage.add_chronicle_event(_event())
    dup_id, action = storage.add_chronicle_event(
        _event(recorded_at=200.0, occurred_phrase="that Sunday"))
    assert action == "duplicate" and dup_id == ev_id


def test_undated_duplicate_dedupes_on_null_occurred_at(storage):
    """NULL occurred_at must still dedup (IS NOT DISTINCT FROM, not =)."""
    ev_id, _ = storage.add_chronicle_event(_event(occurred_at=None))
    dup_id, action = storage.add_chronicle_event(
        _event(occurred_at=None, recorded_at=300.0))
    assert action == "duplicate" and dup_id == ev_id


def test_different_date_is_a_different_event(storage):
    storage.add_chronicle_event(_event())
    _, action = storage.add_chronicle_event(_event(occurred_at="2023-06-01"))
    assert action == "inserted"


def test_search_matches_lexically_in_chronological_order(storage):
    storage.add_chronicle_event(_event(
        occurred_at="2023-06-01", description="kitten's first vet visit",
        description_norm="kitten's first vet visit"))
    storage.add_chronicle_event(_event())
    storage.add_chronicle_event(_event(
        occurred_at=None, occurred_phrase="a while back",
        recorded_at=50.0, description="kitten chose its name",
        description_norm="kitten chose its name"))
    storage.add_chronicle_event(_event(
        occurred_at="2023-01-05", description="bought a road bike",
        description_norm="bought a road bike"))
    hits = storage.chronicle_search("kitten", limit=10)
    assert [h["description"] for h in hits] == [
        "adopted a kitten", "kitten's first vet visit",
        "kitten chose its name"]
    assert hits[0]["occurred_date"] == "2023-05-14"
    assert hits[2]["occurred_date"] is None
    assert hits[2]["occurred_phrase"] == "a while back"


def test_invalidated_events_do_not_serve(storage):
    ev_id, _ = storage.add_chronicle_event(_event())
    storage.invalidate_chronicle_event(ev_id, 500.0)
    assert storage.chronicle_search("kitten", limit=10) == []


def test_invalidated_slot_can_be_restated(storage):
    """Additive-only contradiction handling: invalidating never blocks a
    corrected re-statement of the same (actor, date, description)."""
    ev_id, _ = storage.add_chronicle_event(_event())
    storage.invalidate_chronicle_event(ev_id, 500.0)
    new_id, action = storage.add_chronicle_event(_event(recorded_at=600.0))
    assert action == "inserted" and new_id != ev_id


def test_delete_removes_row_for_rollback(storage):
    ev_id, _ = storage.add_chronicle_event(_event())
    assert storage.delete_chronicle_event(ev_id) is True
    assert storage.chronicle_search("kitten", limit=10) == []
    assert storage.delete_chronicle_event(ev_id) is False
