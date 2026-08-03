"""Schema v28 — chronicle events: dated occurrences as first-class records.

Adds ``chronicle_events`` (one row per extracted event: ``occurred_at`` =
event time, nullable — never fabricated; ``occurred_phrase`` = the source's
own words, kept verbatim; ``recorded_at`` = transaction time; additive-only
with ``invalidated_at`` for contradiction handling — invalidate, never
delete) and a nullable ``chronicle_event_id`` column on ``dream_run_slots``
so event writes journal into the existing rollback mechanism (kind
``"event"``, delete-on-rollback is safe for additive-only records).
``src_entry_id`` carries NO foreign key — entries are evictable, same
rationale as ``dream_run_slots`` (design doc
2026-08-03-aggregation-aware-recall-design.md, Phase 2).

Skips without a PG server (mirrors test_schema_v27).
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_28():
    assert SCHEMA_META_VERSION == 28


def test_chronicle_events_table_exists_with_bitemporal_columns(pg_conn):
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'chronicle_events'").fetchall()}
    assert {"id", "occurred_at", "occurred_phrase", "recorded_at", "actor",
            "actor_norm", "description", "description_norm", "episode",
            "src_entry_id", "hlc_phys", "hlc_logical", "writer_id",
            "invalidated_at"} <= cols


def test_chronicle_events_carries_no_foreign_keys(pg_conn):
    """Regression lock: entries are evictable, so ``src_entry_id`` must not
    reference them (the memory_traces FK is the origin of the reflush-stall
    class), and nothing else may grow a reference either."""
    fks = pg_conn.execute(
        "SELECT conname FROM pg_constraint c "
        "JOIN pg_class t ON c.conrelid = t.oid "
        "WHERE t.relname = 'chronicle_events' AND c.contype = 'f'"
    ).fetchall()
    assert fks == []


def test_occurred_at_is_nullable_and_orders_after_dated_rows(pg_conn):
    """Undated events (phrase-only) must be storable and sort AFTER dated
    ones under the serving order (occurred_at ASC NULLS LAST)."""
    pg_conn.execute(
        "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
        "recorded_at, actor, actor_norm, description, description_norm) "
        "VALUES ('2023-05-14', 'on May 14', 1.0, 'user', 'user', "
        "'adopted a kitten', 'adopted a kitten')")
    pg_conn.execute(
        "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
        "recorded_at, actor, actor_norm, description, description_norm) "
        "VALUES (NULL, 'a while back', 2.0, 'user', 'user', "
        "'visited Lisbon', 'visited lisbon')")
    pg_conn.commit()
    rows = pg_conn.execute(
        "SELECT description, occurred_at FROM chronicle_events "
        "ORDER BY occurred_at ASC NULLS LAST, recorded_at ASC").fetchall()
    assert [r[0] for r in rows] == ["adopted a kitten", "visited Lisbon"]
    assert rows[1][1] is None


def test_invalidated_at_defaults_null_and_round_trips(pg_conn):
    pg_conn.execute(
        "INSERT INTO chronicle_events (occurred_phrase, recorded_at, actor, "
        "actor_norm, description, description_norm) "
        "VALUES ('yesterday', 3.0, 'user', 'user', 'sold the road bike', "
        "'sold the road bike')")
    pg_conn.commit()
    ev_id, inv = pg_conn.execute(
        "SELECT id, invalidated_at FROM chronicle_events "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert inv is None
    pg_conn.execute(
        "UPDATE chronicle_events SET invalidated_at = 4.0 WHERE id = %s",
        (ev_id,))
    pg_conn.commit()
    inv = pg_conn.execute(
        "SELECT invalidated_at FROM chronicle_events WHERE id = %s",
        (ev_id,)).fetchone()[0]
    assert inv == 4.0


def test_dream_run_slots_gains_chronicle_event_id(pg_conn):
    """The journal column event rows carry (NULL for scalar/member rows) —
    rollback deletes the chronicle row it names."""
    cols = {r[0]: r[1] for r in pg_conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'dream_run_slots'").fetchall()}
    assert cols.get("chronicle_event_id") == "YES"


def test_ensure_schema_rerun_is_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)
    ensure_schema(pg_conn)
    tables = {r[0] for r in pg_conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'").fetchall()}
    assert "chronicle_events" in tables
