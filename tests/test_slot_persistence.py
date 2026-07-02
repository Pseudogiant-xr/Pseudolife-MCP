"""P1 (2026-07-02 review): per-slot persistence for the canonical stores.

The old model rewrote the ENTIRE facts/world_facts/lessons table on every
write (DELETE all + reinsert, embeddings included) — O(claims x total_facts)
per dream sweep, permanent id churn, autovacuum pressure, and a structural
blocker for the dormant OCC seam (per-row CAS is meaningless when every save
reassigns ids). These tests pin the new contract:

* a write to slot A leaves slot B's rows (and ids) untouched;
* a slot's full history (current + superseded audit rows) still round-trips;
* forget deletes exactly that slot's rows;
* auto-promoted facts carry writer/HLC stamps (they were unstamped, so they
  could never supersede stamped rows and got retro-labeled 'legacy');
* the HLC re-seeds from stored stamps on hydrate (a wall-clock step-back no
  longer parks user corrections as contenders);
* the DB itself enforces one current row per slot (partial unique index),
  and ensure_schema heals pre-existing duplicates by demoting all but the
  most recently confirmed.
"""

from __future__ import annotations

import tempfile
import time

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url):
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def _fact_ids(svc, entity_norm: str) -> list[int]:
    rows = svc._storage.conn.execute(
        "SELECT id FROM facts WHERE entity_norm = %s ORDER BY id",
        (entity_norm,)).fetchall()
    return [r[0] for r in rows]


# ── per-slot sync: cortex ─────────────────────────────────────────────────

def test_cortex_write_preserves_unrelated_fact_row_ids(svc):
    svc.cortex_write("slot-a", "x", "1", support="user")
    svc.cortex_write("slot-b", "y", "2", support="user")
    ids_b_before = _fact_ids(svc, "slot-b")
    assert ids_b_before, "sanity: slot-b persisted"

    svc.cortex_write("slot-a", "x", "3", support="user")  # touches slot-a only

    assert _fact_ids(svc, "slot-b") == ids_b_before, (
        "a write to slot-a must not rewrite slot-b's rows")


def test_supersede_round_trips_slot_history(svc):
    svc.cortex_write("hist", "port", "1111", support="user")
    svc.cortex_write("hist", "port", "2222", support="user")

    rows = svc._storage.conn.execute(
        "SELECT value, status FROM facts WHERE entity_norm = %s",
        ("hist",)).fetchall()
    by_value = {v: s for v, s in rows}
    assert by_value == {"1111": "superseded", "2222": "current"}

    # And a fresh service hydrates the same picture (svc's autocommit
    # connection holds no locks, so s2's ensure_schema DDL proceeds — H4).
    from pseudolife_memory.service import MemoryService
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s2 = MemoryService(data_dir=d, database_url=svc._db_url)
        try:
            cur = s2.cortex_lookup("hist", "port")
            assert cur is not None and cur["value"] == "2222"
        finally:
            if s2._storage is not None:
                s2._storage.close()


def test_cortex_forget_deletes_slot_rows(svc):
    svc.cortex_write("gone", "a", "1", support="user")
    svc.cortex_write("gone", "a", "2", support="user")   # history row too
    svc.cortex_write("stays", "b", "3", support="user")
    assert len(_fact_ids(svc, "gone")) == 2

    svc.cortex_forget("gone")

    assert _fact_ids(svc, "gone") == []
    assert len(_fact_ids(svc, "stays")) == 1


# ── per-slot sync: world + lessons ────────────────────────────────────────

def test_world_write_preserves_unrelated_row_ids(svc):
    svc.world_write("w-a", "x", "1", source_url="https://example.com/a")
    svc.world_write("w-b", "y", "2", source_url="https://example.com/b")
    ids_b = [r[0] for r in svc._storage.conn.execute(
        "SELECT id FROM world_facts WHERE entity_norm = %s", ("w-b",)).fetchall()]

    svc.world_write("w-a", "x", "3", source_url="https://example.com/a2")

    ids_b_after = [r[0] for r in svc._storage.conn.execute(
        "SELECT id FROM world_facts WHERE entity_norm = %s", ("w-b",)).fetchall()]
    assert ids_b_after == ids_b


def test_lesson_write_preserves_unrelated_row_ids(svc):
    svc.lesson_write("task-a", "approach", "do the thing")
    svc.lesson_write("task-b", "pitfall", "avoid the thing")
    ids_b = [r[0] for r in svc._storage.conn.execute(
        "SELECT id FROM lessons WHERE entity_norm = %s", ("task-b",)).fetchall()]

    svc.lesson_write("task-a", "approach", "do the thing differently")

    ids_b_after = [r[0] for r in svc._storage.conn.execute(
        "SELECT id FROM lessons WHERE entity_norm = %s", ("task-b",)).fetchall()]
    assert ids_b_after == ids_b


# ── stamping + HLC re-seed ────────────────────────────────────────────────

def test_auto_promote_stamps_writer_and_hlc(svc):
    """_promote_slots wrote unstamped facts: (0,0) HLC could never supersede
    a stamped row, and the v11 backfill retro-labeled them writer_id='legacy'
    on every boot."""
    svc.config.memory.cortex.auto_promote = True   # opt-in (single-writer default off)
    svc.store("the relay port is 4001", source="notes")
    rec = svc._cortex.lookup("relay", "port")
    assert rec is not None, "sanity: auto-promote fired"
    assert rec.hlc_phys is not None and rec.hlc_phys > 0
    assert rec.writer_id is not None


def test_hlc_reseeds_from_stored_stamps_on_hydrate(svc):
    """A wall-clock step-back (NTP, resume) between daemon runs must not make
    stored stamps outrank every new write. The clock observes the stored
    maximum at hydrate, so a fresh user correction still supersedes."""
    svc.cortex_write("clock-probe", "value", "old", support="user")
    future_ms = int(time.time() * 1000) + 10**9   # ~11 days ahead
    with svc._storage._txn():
        svc._storage.conn.execute(
            "UPDATE facts SET hlc_phys = %s, hlc_logical = 0 "
            "WHERE entity_norm = %s", (future_ms, "clock-probe"))

    from pseudolife_memory.service import MemoryService
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s2 = MemoryService(data_dir=d, database_url=svc._db_url)
        try:
            s2.cortex_write("clock-probe", "value", "new", support="user")
            cur = s2.cortex_lookup("clock-probe", "value")
            assert cur is not None and cur["value"] == "new", (
                "correction was parked as contender: HLC not re-seeded")
        finally:
            if s2._storage is not None:
                s2._storage.close()


# ── DB-enforced slot invariant ────────────────────────────────────────────

def test_duplicate_current_rows_rejected_by_index(svc):
    import psycopg

    svc.cortex_write("uniq", "slot", "v1", support="user")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with svc._storage._txn():
            svc._storage.conn.execute(
                "INSERT INTO facts (entity, attribute, entity_norm, "
                "attribute_norm, value, polarity, status, confidence, "
                "asserted_at, last_confirmed) VALUES "
                "('uniq','slot','uniq','slot','v2','+','current',0.5,1.0,1.0)")


def test_ensure_schema_demotes_preexisting_duplicate_current(pg_conn):
    """Banks that acquired duplicate current rows (e.g. additive
    restore_from_pt) must heal at startup: keep the most recently confirmed,
    demote the rest — mirroring CortexStore._reindex_current."""
    from pseudolife_memory.storage.schema import ensure_schema

    pg_conn.execute("DROP INDEX IF EXISTS facts_slot_current_uq")
    pg_conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, polarity, status, confidence, asserted_at, last_confirmed) "
        "VALUES ('dup','k','dup','k','older','+','current',0.5,1.0,1.0), "
        "('dup','k','dup','k','newer','+','current',0.5,2.0,2.0)")
    pg_conn.commit()

    ensure_schema(pg_conn)

    rows = pg_conn.execute(
        "SELECT value, status FROM facts WHERE entity_norm='dup'").fetchall()
    by_value = {v: s for v, s in rows}
    assert by_value["newer"] == "current"
    assert by_value["older"] == "superseded"
    pg_conn.execute("DELETE FROM facts WHERE entity_norm='dup'")
    pg_conn.commit()
