"""Alias-aware cortex lookup.

Regression for the silent miss where canonical facts stored under a canonical
entity were unreachable via a colloquial alias: ``memory_fact_get`` ->
``cortex_lookup`` keyed the cortex slot on the raw (normalised) entity string
and never consulted the graph's ``entity_aliases``, contradicting the tool's own
docstring ("every fact lookup resolves aliases first").

PG-backed — the graph + aliases live only in Postgres. Skips cleanly when no
test PG is reachable (see tests/pg_fixtures.py).

Run: PYTHONPATH=. python -m pytest tests/test_cortex_alias.py -q
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.service import MemoryService


def test_cortex_lookup_resolves_alias_to_canonical(pg_conn, pg_url, tmp_path):
    """fact_get via an alias returns the canonical entity's current fact."""
    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    svc.graph_alias("dev-box", "4090")  # bind 4090 -> dev-box

    got = svc.cortex_lookup("4090", "gpu")
    assert got is not None, "alias should resolve to the canonical slot"
    assert got["value"] == "RTX 4090"


def test_cortex_lookup_direct_hit_unaffected(pg_conn, pg_url, tmp_path):
    """Canonical lookups still work (and shouldn't need the alias round-trip)."""
    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    got = svc.cortex_lookup("dev-box", "gpu")
    assert got is not None and got["value"] == "RTX 4090"


def test_cortex_lookup_unknown_entity_still_none(pg_conn, pg_url, tmp_path):
    """A genuine miss (no slot, no alias) still returns None — no crash."""
    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.cortex_write("dev-box", "gpu", "RTX 4090", support="user")
    assert svc.cortex_lookup("nonexistent-thing", "gpu") is None
