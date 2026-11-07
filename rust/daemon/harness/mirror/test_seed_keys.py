"""Scenario seeds that write SQL directly still apply on a copy of a real bank.

Three w2e seeds wrote literal keys a real bank already holds: board events
numbered from 1 (hook-conductor-*), a community and sources for an entity
named ``postgres`` (graph-read) and an entity named ``release-checklist``
(dream-deep-exclusion). Each raised UniqueViolation on the daemon box's
restored bank (2026-11-07), so those scenarios compared nothing. Here each
seed's SQL runs against a bank already holding such rows.

It uses ``test_seed.py``'s throwaway PostgreSQL (``docker exec psql``, no
volume), so it runs wherever a Linux Docker engine answers, CI included.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import seed  # noqa: E402
from test_seed import _rows, throwaway_pg  # noqa: E402,F401  the shared fixture

SCENARIOS = ("graph-read", "dream-deep-exclusion", "hook-conductor-reminder")


@pytest.fixture
def occupied(throwaway_pg):
    """A bank whose keys a seed on an empty bank would take first."""
    seed.Docker().psql(throwaway_pg, "pl_cf_mir_seed", """
TRUNCATE entities, entity_communities, entity_sources, relations, edges,
  coordination_agents, coordination_leases, coordination_events CASCADE;
INSERT INTO entities (canonical, display, etype, created_at) VALUES
  ('postgres', 'Postgres', NULL, 1), ('release-checklist', 'release checklist', NULL, 1);
INSERT INTO entity_communities (entity_id, community_id, computed_at)
  SELECT id, 40, 1 FROM entities WHERE canonical = 'postgres';
INSERT INTO entity_sources (entity_id, source, count, origin, updated_at)
  SELECT id, 'claude-code', 9, 'derived', 1 FROM entities WHERE canonical = 'postgres';
-- What the scenario's own daemon ops create before its SQL runs.
INSERT INTO relations (name, description, created_at) VALUES ('prefers', 'x', 1);
INSERT INTO coordination_events (seq, event, actor, payload, created_at, prev_hash, hash)
  SELECT g, 'update', 'agent', '{}', 1, 'x', 'x' FROM generate_series(1, 20) g;
""")
    return throwaway_pg


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_seed_writing_sql_applies_on_an_occupied_bank(occupied, scenario):
    import w2e
    statements = [op["sql"] for op in w2e.SCENARIOS[scenario]["seed"] if op.get("op") == "sql"]
    assert statements, scenario
    for statement in statements:
        seed.Docker().psql(occupied, "pl_cf_mir_seed", statement + ";\n")  # UniqueViolation before the fix
    if scenario == "graph-read":
        # The seed's own community and sources win over the real entity's.
        assert _rows(occupied, "SELECT e.canonical, c.community_id FROM entity_communities c "
                     "JOIN entities e ON e.id = c.entity_id WHERE canonical = 'postgres'") == [["postgres", "1"]]
        assert _rows(occupied, "SELECT s.source, s.count FROM entity_sources s JOIN entities e "
                     "ON e.id = s.entity_id WHERE canonical = 'postgres' ORDER BY 1") == [
            ["agent", "1"], ["claude-code", "1"]]
    if scenario.startswith("hook-conductor"):
        # The seeded events follow the bank's own, in the order the cases list them.
        seqs = [int(r[0]) for r in _rows(occupied, "SELECT seq FROM coordination_events "
                                         "WHERE prev_hash = 'fixture' ORDER BY seq")]
        assert len(seqs) == 8 and seqs[0] == 21 and seqs == sorted(seqs)
