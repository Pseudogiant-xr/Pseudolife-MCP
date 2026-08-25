"""Schema v34 — served facts on the retrieval event log.

Adds one additive nullable column: ``retrieval_events.served_facts``
(JSONB) — the cortex slots the search's cortex-first block served beside
the entries, as ``[{entity_norm, attribute_norm, rank, score, kind,
contested}]``. NULL = a pre-v34 row, or a search that served no facts.
Written by an UPDATE keyed on the event id the search returned, so
attachment is exact — no session-window guessing. Closes the training
gap #200 documented: the event log recorded only the entry half of every
search response, so a Phase-1 learned reranker could not train on facts.

Skips without a PG server (mirrors test_schema_v33).
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_34():
    # The newest schema test carries the exact current-version pin — the
    # deliberate tripwire that forces a bump author through the shipping
    # checklist. On the v35 bump: relax this to >= 34 and pin == 35 in the
    # new test_schema_v35.py (two-file touch, not ten).
    assert SCHEMA_META_VERSION == 34


def test_retrieval_events_served_facts_column_exists(pg_conn):
    row = pg_conn.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'retrieval_events' "
        "AND column_name = 'served_facts'"
    ).fetchone()
    assert row is not None, "retrieval_events.served_facts column not created"
    assert row[0] == "jsonb"
    meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert meta is not None and int(meta[0]) == SCHEMA_META_VERSION


def test_ensure_schema_rerun_is_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)
    ensure_schema(pg_conn)
    row = pg_conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'retrieval_events' "
        "AND column_name = 'served_facts'"
    ).fetchone()
    assert row is not None, "served_facts column lost on re-run"
