"""Scenario seeds that write SQL directly still apply on a copy of a real bank.

Three w2e seeds wrote literal keys a real bank already holds: board events
numbered from 1 (hook-conductor-*), a community for an entity named
``postgres`` (graph-read) and an entity named ``release-checklist``
(dream-deep-exclusion). Each raised UniqueViolation on the daemon box's
restored bank (2026-11-07), so those scenarios compared nothing. Here each
seed's SQL runs against a bank already holding such rows.

Needs the disposable-bank login (``pgdisposable``); without one it skips.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[3]))

import pgdisposable  # noqa: E402

pg = pgdisposable.Slice("mir")
SCENARIOS = ("graph-read", "dream-deep-exclusion", "hook-conductor-reminder")


@pytest.fixture
def occupied(monkeypatch):
    """A bank whose keys a seed on an empty bank would take first."""
    if not pgdisposable.LOGIN_FILE.exists():
        pytest.skip("no disposable-bank login here")
    monkeypatch.delenv(pgdisposable.BASE_ENV, raising=False)
    import psycopg
    from pseudolife_memory.storage.schema import SCHEMA_SQL
    name = pg.name("pl_cf_mir_seedkeys")
    dsn = pg.create(name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(SCHEMA_SQL)
        conn.execute("INSERT INTO entities (canonical, display, etype, created_at) VALUES "
                     "('postgres', 'Postgres', NULL, 1), ('release-checklist', 'release checklist', NULL, 1)")
        conn.execute("INSERT INTO entity_communities (entity_id, community_id, computed_at) "
                     "SELECT id, 40, 1 FROM entities WHERE canonical = 'postgres'")
        # What the scenario's own daemon ops create before its SQL runs.
        conn.execute("INSERT INTO relations (name, description, created_at) VALUES ('prefers', 'x', 1) "
                     "ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO coordination_events (seq, event, actor, payload, created_at, prev_hash, hash) "
                     "SELECT g, 'update', 'agent', '{}', 1, 'x', 'x' FROM generate_series(1, 20) g")
    yield dsn
    pg.drop(name)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_seed_writing_sql_applies_on_an_occupied_bank(occupied, scenario):
    import psycopg
    sys.path.insert(0, str(HERE.parent))
    import w2e
    statements = [op["sql"] for op in w2e.SCENARIOS[scenario]["seed"] if op.get("op") == "sql"]
    assert statements, scenario
    with psycopg.connect(occupied, autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)  # UniqueViolation before the fix
        if scenario.startswith("hook-conductor"):
            # The seeded events follow the bank's own, in the order the cases list them.
            seqs = [r[0] for r in conn.execute(
                "SELECT seq FROM coordination_events WHERE prev_hash = 'fixture' ORDER BY seq")]
            assert len(seqs) == 8 and seqs[0] == 21 and seqs == sorted(seqs)
