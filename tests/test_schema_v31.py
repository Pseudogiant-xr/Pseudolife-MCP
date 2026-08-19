"""Schema v31 — retrieval event log (learned-reranker Phase 0).

Adds two tables: ``retrieval_events`` (one append-only row per search that
served entries: query text, ranked served list as JSONB, writer session/
episode) and ``retrieval_uses`` (implicit relevance labels, written when a
served entry is later fetched/reinforced in the same session; CASCADEs from
its event). Together they are the (query, served, used) training tuples for
a future learned fusion/reranker stage. Purely observational — no retrieval
behaviour changes.

Skips without a PG server (mirrors test_schema_v30).
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_31():
    # The newest schema test carries the exact current-version pin — the
    # deliberate tripwire that forces a bump author through the shipping
    # checklist. On the v32 bump: relax this to >= 31 and pin == 32 in the
    # new test_schema_v32.py (two-file touch, not ten).
    assert SCHEMA_META_VERSION == 31


def test_retrieval_log_tables_exist(pg_conn):
    for t in ("retrieval_events", "retrieval_uses"):
        reg = pg_conn.execute(
            "SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()
        assert reg[0] is not None, f"{t} table not created"
    row = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row is not None and int(row[0]) == SCHEMA_META_VERSION


def test_ensure_schema_rerun_is_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)
    ensure_schema(pg_conn)
    for t in ("retrieval_events", "retrieval_uses"):
        reg = pg_conn.execute(
            "SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()
        assert reg[0] is not None, f"{t} lost on re-run"
