"""Schema v33 — read telemetry: slot reads + the explicit-reinforce split.

Adds the ``slot_reads`` table (per-slot read counters, keyed on the stable
``(entity_norm, attribute_norm)`` slot like ``memory_traces`` — NOT
``facts.id``, which is regenerated on every cortex snapshot save) and one
additive column, ``entries.explicit_reinforcements`` (bumped only by an
explicit ``memory_reinforce``; the pre-existing ``reinforcements`` counter
keeps counting dream-trace links too, so the retention formula is unchanged).

Skips without a PG server (mirrors test_schema_v32).
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_33():
    # The newest schema test carries the exact current-version pin — the
    # deliberate tripwire that forces a bump author through the shipping
    # checklist. On the v34 bump: relax this to >= 33 and pin == 34 in the
    # new test_schema_v34.py (two-file touch, not ten).
    assert SCHEMA_META_VERSION == 33


def test_slot_reads_table_exists(pg_conn):
    cols = {
        r[0]: r[1] for r in pg_conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'slot_reads'"
        ).fetchall()
    }
    assert cols, "slot_reads table not created"
    assert cols["entity_norm"] == "text"
    assert cols["attribute_norm"] == "text"
    assert cols["read_count"] == "bigint"
    assert cols["last_read_at"] == "double precision"
    meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert meta is not None and int(meta[0]) == SCHEMA_META_VERSION


def test_entries_explicit_reinforcements_column_exists(pg_conn):
    row = pg_conn.execute(
        "SELECT data_type, column_default FROM information_schema.columns "
        "WHERE table_name = 'entries' "
        "AND column_name = 'explicit_reinforcements'"
    ).fetchone()
    assert row is not None, "entries.explicit_reinforcements column not created"
    assert row[0] == "integer"
    assert "0" in (row[1] or ""), "explicit_reinforcements must default to 0"


def test_ensure_schema_rerun_is_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)
    ensure_schema(pg_conn)
    row = pg_conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'slot_reads'"
    ).fetchone()
    assert row is not None, "slot_reads table lost on re-run"
