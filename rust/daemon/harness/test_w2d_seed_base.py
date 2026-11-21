"""The w2d MCP stats seed works on top of a bank that already has a graph.

On a copy of the real bank, `w2d_seed.py --stats-extra` inserted community id
1 and the `graph_digest` meta key, both of which the bank already held:
UniqueViolation, and `mcp-stats` errored on every real-bank attempt (forced
real-data run, daemon box, 2026-11-07). The seed now takes the next free
community id and replaces the digest, which on an empty bank is the same as
before.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))


@pytest.fixture
def bank(monkeypatch):
    import pgdisposable
    if not pgdisposable.LOGIN_FILE.exists():
        import database_gate
        database_gate.unavailable("no disposable-bank login here",
                                  "create it once on the daemon host with `pseudolife-mcp test-login "
                                  "create`, or point PSEUDOLIFE_TEST_PG_LOGIN_FILE at it")
    import psycopg
    from rust.harness_support.import_blocker import cold_harness_imports
    # An empty bank of its own, never a copy of a mirror's base.
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE", raising=False)
    monkeypatch.delenv("PL_HARNESS_SEED_DSN", raising=False)
    monkeypatch.delenv("PL_HARNESS_SEED_RUST", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    pg = pgdisposable.Slice("w2d")
    name = pg.name("pl_cf_w2d_seed_base")
    dsn = pg.create(name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            with cold_harness_imports():
                from rust.harness_support.schema import ensure_schema

                ensure_schema(conn)
            yield conn
    finally:
        pg.drop(name)


def test_the_stats_seed_lands_on_a_bank_that_has_a_graph(bank):
    import w2d_seed
    # What a real bank already holds: its own communities and digest.
    bank.execute("INSERT INTO communities (id, label, size, cohesion, computed_at) VALUES "
                 "(1, 'existing', 4, 0.4, 1.0), (2, 'existing two', 2, 0.6, 1.0)")
    bank.execute("INSERT INTO meta (key, value) VALUES ('graph_digest', '{\"communities\": 2}')")
    w2d_seed.stats_extra(bank)
    rows = bank.execute("SELECT id, label FROM communities ORDER BY id").fetchall()
    assert rows[-1] == (3, "deploy tooling") and len(rows) == 3
    digest = bank.execute("SELECT value FROM meta WHERE key = 'graph_digest'").fetchone()[0]
    assert digest["communities"] == 1


def test_on_an_empty_bank_the_seed_is_unchanged(bank):
    """The near miss: the synthetic bank still gets community 1."""
    import w2d_seed
    w2d_seed.stats_extra(bank)
    assert bank.execute("SELECT id, label FROM communities").fetchall() == [(1, "deploy tooling")]


@pytest.mark.parametrize("name", ["pseudolife_memory", "outside_fixture_namespace"])
def test_seed_connection_refuses_foreign_names_before_connecting(monkeypatch, name):
    import w2d_seed

    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    monkeypatch.delenv("_PSEUDOLIFE_PRODUCTION_DB", raising=False)
    connect = MagicMock()
    monkeypatch.setattr(w2d_seed.psycopg, "connect", connect)
    with pytest.raises((RuntimeError, ValueError)):
        w2d_seed.open_bank(f"dbname={name}")
    connect.assert_not_called()


@pytest.mark.parametrize("missing", [None, "docs", "crafted", "root", "steps", "supersede"])
def test_seed_bank_witness_rejects_missing_inputs(missing):
    import w2d_seed as seed

    conn = MagicMock()
    docs = ["fixture document"]
    rows = {
        "docs": [(docs[0],)],
        "crafted": [(text, source, tags or []) for text, source, tags in seed.CRAFTED],
        "root": [("fixture-episode",)],
        "steps": [(text,) for text in seed.EPISODE_TEXTS],
        "supersede": [(seed.SUPERSEDED[0], seed.SUPERSEDED[1]), (seed.SUPERSEDED[1], None)],
    }
    if missing:
        rows[missing].pop()
    results = []
    for slot, entries in rows.items():
        cursor = MagicMock()
        if slot == "root":
            cursor.fetchone.return_value = entries[0] if entries else None
        elif slot in {"docs", "steps"}:
            cursor.__iter__.return_value = iter(entries)
        else:
            cursor.fetchall.return_value = entries
        results.append(cursor)
    conn.execute.side_effect = results
    if missing:
        with pytest.raises(RuntimeError, match="seed precondition not met"):
            seed.check_base(conn, docs, 42)
    else:
        seed.check_base(conn, docs, 42)
        assert conn.execute.call_args_list[0].args[1] == (42,)
