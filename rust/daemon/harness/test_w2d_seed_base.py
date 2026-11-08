"""The w2d MCP stats seed works on top of a bank that already has a graph.

On a copy of the real bank, `w2d_seed.py --stats-extra` inserted community id
1 and the `graph_digest` meta key, both of which the bank already held:
UniqueViolation, and `mcp-stats` errored on every real-bank attempt (forced
real-data run, daemon box, 2026-11-07). The seed now takes the next free
community id and replaces the digest, which on an empty bank is the same as
before.
"""
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))


@pytest.fixture
def bank(monkeypatch):
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    import psycopg
    from pseudolife_memory.storage.schema import ensure_schema
    pg = pgdisposable.Slice("w2d")
    name = pg.name("pl_cf_w2d_seed_base")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            ensure_schema(conn)
            monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", dsn)
            yield conn
    finally:
        # The drop refuses a database PSEUDOLIFE_MCP_DATABASE_URL names.
        monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
        pg.drop(name)


def test_the_stats_seed_lands_on_a_bank_that_has_a_graph(bank):
    import w2d_seed
    # What a real bank already holds: its own communities and digest.
    bank.execute("INSERT INTO communities (id, label, size, cohesion, computed_at) VALUES "
                 "(1, 'existing', 4, 0.4, 1.0), (2, 'existing two', 2, 0.6, 1.0)")
    bank.execute("INSERT INTO meta (key, value) VALUES ('graph_digest', '{\"communities\": 2}')")
    w2d_seed.stats_extra()
    rows = bank.execute("SELECT id, label FROM communities ORDER BY id").fetchall()
    assert rows[-1] == (3, "deploy tooling") and len(rows) == 3
    digest = bank.execute("SELECT value FROM meta WHERE key = 'graph_digest'").fetchone()[0]
    assert digest["communities"] == 1


def test_on_an_empty_bank_the_seed_is_unchanged(bank):
    """The near miss: the synthetic bank still gets community 1."""
    import w2d_seed
    w2d_seed.stats_extra()
    assert bank.execute("SELECT id, label FROM communities").fetchall() == [(1, "deploy tooling")]
