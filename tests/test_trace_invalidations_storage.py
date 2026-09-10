"""Durable source-supersession events below the serving layer."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


def _entry(text: str = "source memory", **over):
    row = {
        "band": "flat",
        "text": text,
        "embedding": np.zeros(1024, dtype=np.float32),
        "surprise": 0.5,
        "ts": 1000.0,
        "access_count": 0,
        "source": "test",
        "superseded_at": None,
        "superseded_by_text": None,
        "last_logical_turn": None,
        "episode_id": None,
        "episode_title": None,
        "tags": [],
        "slots": [],
    }
    row.update(over)
    return row


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    value = PostgresStorage(pg_url)
    yield value
    value.close()


def _events(storage, *slots):
    return storage.trace_invalidations_for_slots(slots)


def _loaded_entry(storage, entry_id):
    return next(row for row in storage.load_entries() if row["id"] == entry_id)


def test_supersede_entries_records_each_trace_once_and_survives_source_delete(storage):
    eid = storage.insert_entry(_entry())
    assert storage.add_trace("daemon", "host", eid, 10.0) is True
    assert storage.add_trace("daemon", "port", eid, 11.0) is True

    assert storage.supersede_entries(
        [eid, eid], superseded_at=20.0, superseded_by_text="corrected") == 1
    by_slot = _events(storage, ("daemon", "host"), ("daemon", "port"))
    assert by_slot == {
        ("daemon", "host"): [{
            "source_entry_id": eid,
            "invalidated_at": 20.0,
            "cause": "source_superseded",
        }],
        ("daemon", "port"): [{
            "source_entry_id": eid,
            "invalidated_at": 20.0,
            "cause": "source_superseded",
        }],
    }

    storage.delete_entry_ids([eid])
    assert storage.traces_for_slots([("daemon", "host")]) == {}
    assert _events(storage, ("daemon", "host")) == {
        ("daemon", "host"): [{
            "source_entry_id": eid,
            "invalidated_at": 20.0,
            "cause": "source_superseded",
        }],
    }


@pytest.mark.parametrize("bad", ["missing", "retired"])
def test_supersede_entries_rejects_the_whole_batch_before_mutation(storage, bad):
    first = storage.insert_entry(_entry("first"))
    second = storage.insert_entry(_entry(
        "second", superseded_at=5.0, superseded_by_text="old"
        if bad == "retired" else None))
    storage.add_trace("one", "role", first, 1.0)
    targets = [first, second] if bad == "retired" else [first, 999999999]

    with pytest.raises(ValueError, match="supersede_entries"):
        storage.supersede_entries(
            targets, superseded_at=20.0, superseded_by_text="new")

    row = _loaded_entry(storage, first)
    assert row["superseded_at"] is None
    assert _events(storage, ("one", "role")) == {}


@pytest.mark.parametrize("bad_id", [True, 1.5, "1"])
def test_supersede_entries_rejects_non_integer_entry_ids(storage, bad_id):
    eid = storage.insert_entry(_entry())

    with pytest.raises(ValueError, match="positive integers"):
        storage.supersede_entries(
            [bad_id], superseded_at=20.0, superseded_by_text="corrected")

    assert _loaded_entry(storage, eid)["superseded_at"] is None


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), float("-inf")])
def test_supersede_entries_rejects_non_finite_timestamp(storage, bad_time):
    eid = storage.insert_entry(_entry())
    storage.add_trace("daemon", "host", eid, 10.0)

    with pytest.raises(ValueError, match="superseded_at must be finite"):
        storage.supersede_entries(
            [eid], superseded_at=bad_time, superseded_by_text="corrected")

    assert _loaded_entry(storage, eid)["superseded_at"] is None
    assert _events(storage, ("daemon", "host")) == {}


def test_supersede_entries_rolls_back_entry_and_event_on_event_insert_failure(
    storage,
):
    eid = storage.insert_entry(_entry())
    storage.add_trace("daemon", "host", eid, 10.0)
    conn = storage.conn
    conn.execute(
        "CREATE FUNCTION reject_trace_invalidation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected event failure'; END $$"
    )
    conn.execute(
        "CREATE TRIGGER reject_trace_invalidation "
        "BEFORE INSERT ON memory_trace_invalidations "
        "FOR EACH ROW EXECUTE FUNCTION reject_trace_invalidation()"
    )
    try:
        with pytest.raises(Exception, match="injected event failure"):
            storage.supersede_entries(
                [eid], superseded_at=20.0, superseded_by_text="corrected")
        assert _loaded_entry(storage, eid)["superseded_at"] is None
        assert _events(storage, ("daemon", "host")) == {}
    finally:
        conn.execute(
            "DROP TRIGGER IF EXISTS reject_trace_invalidation "
            "ON memory_trace_invalidations"
        )
        conn.execute("DROP FUNCTION IF EXISTS reject_trace_invalidation()")


def test_add_trace_to_already_retired_entry_creates_one_event_even_on_retry(storage):
    eid = storage.insert_entry(_entry(
        superseded_at=20.0, superseded_by_text="corrected"))

    assert storage.add_trace("daemon", "host", eid, 30.0) is True
    assert storage.add_trace("daemon", "host", eid, 31.0) is False
    assert _events(storage, ("daemon", "host")) == {
        ("daemon", "host"): [{
            "source_entry_id": eid,
            "invalidated_at": 20.0,
            "cause": "source_superseded",
        }],
    }


def test_deleting_a_live_traced_entry_does_not_manufacture_an_invalidation(storage):
    eid = storage.insert_entry(_entry())
    storage.add_trace("daemon", "host", eid, 10.0)

    storage.delete_entry_ids([eid])

    assert _events(storage, ("daemon", "host"), ("daemon", "host")) == {}


def test_generic_update_entry_rejects_supersession_fields(storage):
    eid = storage.insert_entry(_entry())
    with pytest.raises(ValueError, match="non-updatable.*superseded_at"):
        storage.update_entry(
            eid, superseded_at=20.0, superseded_by_text="corrected")
    assert _loaded_entry(storage, eid)["superseded_at"] is None


def test_schema_creation_backfills_once_from_surviving_trace_pairs(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    eid = pg_conn.execute(
        "INSERT INTO entries (band, text, embedding, ts, superseded_at, "
        "superseded_by_text) VALUES ('flat', 'old source', %s::vector, 1.0, 20.0, "
        "'corrected') RETURNING id",
        ("[" + ",".join(["0"] * 1024) + "]",),
    ).fetchone()[0]
    pg_conn.execute(
        "INSERT INTO memory_traces "
        "(entity_norm, attribute_norm, entry_id, created_at) "
        "VALUES ('daemon', 'host', %s, 10.0)",
        (eid,),
    )
    pg_conn.execute("DROP TABLE memory_trace_invalidations")
    pg_conn.commit()

    ensure_schema(pg_conn)
    row = pg_conn.execute(
        "SELECT source_entry_id, invalidated_at, cause "
        "FROM memory_trace_invalidations"
    ).fetchone()
    assert row == (eid, 20.0, "source_superseded")

    # Once a v39 table exists its contents are authoritative; startup must
    # not repeatedly manufacture deliberately absent runtime events.
    pg_conn.execute("DELETE FROM memory_trace_invalidations")
    pg_conn.commit()
    ensure_schema(pg_conn)
    assert pg_conn.execute(
        "SELECT count(*) FROM memory_trace_invalidations").fetchone()[0] == 0


class _PauseAfterExecute:
    """Connection proxy that pauses after one statement has acquired its lock."""

    def __init__(self, conn, needle: str, locked: threading.Event,
                 release: threading.Event):
        self._conn = conn
        self._needle = needle
        self._locked = locked
        self._release = release

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, query, params=None, **kwargs):
        result = self._conn.execute(query, params, **kwargs)
        if self._needle in str(query):
            self._needle = ""
            self._locked.set()
            if not self._release.wait(3):
                raise TimeoutError("test did not release the acquired row lock")
        return result


def _wait_for_row_lock(observer, blocked_pid: int, blocker_pid: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        wait_type, blockers = observer.execute(
            "SELECT wait_event_type, pg_blocking_pids(%s) "
            "FROM pg_stat_activity WHERE pid = %s",
            (blocked_pid, blocked_pid),
        ).fetchone()
        if wait_type == "Lock" and blocker_pid in blockers:
            return
        time.sleep(0.01)
    pytest.fail(
        f"backend {blocked_pid} never waited on row lock from {blocker_pid}")


@pytest.mark.parametrize("first", ["trace", "supersede"])
def test_trace_and_supersession_serialize_on_the_source_row(pg_conn, pg_url, first):
    from pseudolife_memory.storage.postgres import PostgresStorage

    a = PostgresStorage(pg_url)
    b = PostgresStorage(pg_url)
    try:
        eid = a.insert_entry(_entry())
        locked = threading.Event()
        release = threading.Event()
        first_done = threading.Event()
        second_done = threading.Event()
        errors = []

        if first == "trace":
            a._conn = _PauseAfterExecute(
                a._conn, "WHERE id = %s FOR UPDATE", locked, release)
            first_call = lambda: a.add_trace("daemon", "host", eid, 10.0)
            second_call = lambda: b.supersede_entries(
                [eid], superseded_at=20.0, superseded_by_text="corrected")
        else:
            a._conn = _PauseAfterExecute(
                a._conn, "ORDER BY id FOR UPDATE", locked, release)
            first_call = lambda: a.supersede_entries(
                [eid], superseded_at=20.0, superseded_by_text="corrected")
            second_call = lambda: b.add_trace("daemon", "host", eid, 10.0)

        def run(call, done):
            try:
                call()
            except BaseException as exc:  # surfaced on the main test thread
                errors.append(exc)
            finally:
                done.set()

        one = threading.Thread(target=run, args=(first_call, first_done))
        two = threading.Thread(target=run, args=(second_call, second_done))
        one.start()
        assert locked.wait(2), "first operation never acquired the source-row lock"
        two.start()
        _wait_for_row_lock(pg_conn, b.conn.info.backend_pid, a.conn.info.backend_pid)
        assert not second_done.is_set(), "second operation bypassed the row lock"
        release.set()
        one.join(3)
        two.join(3)
        assert first_done.is_set() and second_done.is_set()
        assert errors == []
        assert b.trace_invalidations_for_slots([("daemon", "host")]) == {
            ("daemon", "host"): [{
                "source_entry_id": eid,
                "invalidated_at": 20.0,
                "cause": "source_superseded",
            }],
        }
    finally:
        a.close()
        b.close()
