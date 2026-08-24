"""Schema v32 — per-event ranking params on the retrieval log.

Adds one additive nullable column: ``retrieval_events.params`` (JSONB), the
snapshot of the knobs in force for that query (fusion weights, margin gate,
recency ramp, filters). The served list widens inside the existing JSONB
column and needs no DDL; the params blob is per-event, so it needs its own.

Skips without a PG server (mirrors test_schema_v31).
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_32():
    # The newest schema test carries the exact current-version pin — the
    # deliberate tripwire that forces a bump author through the shipping
    # checklist. On the v33 bump: relax this to >= 32 and pin == 33 in the
    # new test_schema_v33.py (two-file touch, not ten).
    assert SCHEMA_META_VERSION == 32


def test_retrieval_events_params_column_exists(pg_conn):
    row = pg_conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'retrieval_events' AND column_name = 'params'"
    ).fetchone()
    assert row is not None, "retrieval_events.params column not created"
    assert row[0] == "jsonb"
    meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert meta is not None and int(meta[0]) == SCHEMA_META_VERSION


def test_ensure_schema_rerun_is_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)
    ensure_schema(pg_conn)
    row = pg_conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'retrieval_events' AND column_name = 'params'"
    ).fetchone()
    assert row is not None, "params column lost on re-run"
