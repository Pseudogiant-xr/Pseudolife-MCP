"""Schema v29 — epistemic stance as a labelled field on cortex facts.

Adds a nullable ``stance`` TEXT column to ``facts``: the source's own hedge
words ("probably", "unconfirmed", "per the runbook"), kept verbatim and
SEPARATE from ``value`` so consolidation cannot silently convert a hedged
claim into a confident canonical fact (design doc
2026-08-12-stance-span-gate-design.md; the labelled-field-vs-inline result
it preregisters against is arXiv:2608.06953). ``stance`` is display/decision
metadata for the reader — it never feeds confidence, ranking, or
supersession. NULL means "asserted plainly", which is exactly how every
pre-v29 row behaved, so the migration is a no-op on existing banks.

Skips without a PG server (mirrors test_schema_v28).
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_at_least_29():
    # Relaxed on the v30 bump; the exact current-version pin lives in the
    # newest schema test (test_schema_v30.py).
    assert SCHEMA_META_VERSION >= 29


def test_facts_stance_column_exists_and_is_nullable(pg_conn):
    cols = {r[0]: r[1] for r in pg_conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'facts'").fetchall()}
    assert cols.get("stance") == "YES"


def test_stance_round_trips_through_storage(pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    row = {
        "entity": "user", "attribute": "database plan",
        "entity_norm": "user", "attribute_norm": "database plan",
        "value": "Postgres 18", "polarity": "+", "status": "current",
        "confidence": 0.6, "origin": "agent", "support": ["agent"],
        "provenance": [], "asserted_at": 1.0, "last_confirmed": 1.0,
        "supersedes_value": None, "superseded_by_value": None,
        "superseded_at": None, "embedding": None, "entity_id": None,
        "object_entity_id": None, "freshness_class": "evergreen",
        "kind": "scalar", "value_norm": None, "stance": "probably",
    }
    storage.upsert_fact(row)
    facts = [f for f in storage.load_facts()
             if f["attribute_norm"] == "database plan"]
    assert facts and facts[-1]["stance"] == "probably"


def test_stance_null_on_unhedged_insert(pg_url):
    """A row inserted without the key stores NULL — pre-v29 writer code and
    plainly asserted facts are indistinguishable, by design."""
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    row = {
        "entity": "svc", "attribute": "port",
        "entity_norm": "svc", "attribute_norm": "port",
        "value": "8080", "polarity": "+", "status": "current",
        "confidence": 0.9, "origin": "agent", "support": ["agent"],
        "provenance": [], "asserted_at": 1.0, "last_confirmed": 1.0,
        "supersedes_value": None, "superseded_by_value": None,
        "superseded_at": None, "embedding": None, "entity_id": None,
        "object_entity_id": None, "freshness_class": "evergreen",
        "kind": "scalar", "value_norm": None,
    }
    storage.upsert_fact(row)
    facts = [f for f in storage.load_facts() if f["attribute_norm"] == "port"]
    assert facts and facts[-1]["stance"] is None


def test_ensure_schema_rerun_is_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)
    ensure_schema(pg_conn)
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'facts'").fetchall()}
    assert "stance" in cols
