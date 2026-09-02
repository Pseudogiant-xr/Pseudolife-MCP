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


# ── reverse direction: canonical / display name -> alias-keyed record ─────
#
# A graph merge folds the absorbed node's canonical into the survivor's
# aliases without rewriting the cortex records written under it. Since
# 2026-09-02 memory_recall / memory_graph / the Console dossier attach such
# a record to the surviving node (graph.alias_canonical_map), so a caller
# who follows recall output with the displayed name must get the fact it
# was just shown, not a miss.


def _merge_alias_into_canonical(svc):
    """The live shape (production bank, 2026-09-02): facts were written
    under ``pr-235`` and a later merge folded that node into ``PR #235``
    (canonical ``pr-#235``), so the record's entity string became an alias
    of the surviving node while the record itself kept its slot key."""
    svc.graph_relate("PR #235", "part-of", "pseudolife-mcp", origin="user")
    assert svc.graph_merge("pr-235", "PR #235")["merged"] is True
    node = svc.entity_ref("PR #235")
    assert node["canonical"] == "pr-#235" and "pr-235" in node["aliases"]


def test_cortex_lookup_finds_fact_written_under_an_alias(pg_conn, pg_url, tmp_path):
    """fact_get on the node's display or canonical name serves a record
    written under a name a later merge folded into that node; the record
    keeps its own slot key (where its traces, contenders and any
    ``correct_with`` call live); a genuine miss stays a miss."""
    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.cortex_write("pr-235", "branch", "fix/recall-alias-keyed-facts",
                     support="user")
    _merge_alias_into_canonical(svc)

    for name in ("PR #235", "pr-#235"):
        got = svc.cortex_lookup(name, "branch")
        assert got is not None, f"{name!r} should reach the alias-keyed slot"
        assert got["value"] == "fix/recall-alias-keyed-facts"
        assert got["entity"] == "pr-235"
    # Querying the alias itself is a direct hit, as before.
    assert svc.cortex_lookup("pr-235", "branch")["value"] == (
        "fix/recall-alias-keyed-facts")
    # Through a SECOND alias the list is (query, canonical, alias) — the
    # record sits behind both retries, not just the first.
    svc.graph_alias("PR #235", "pull-235")
    assert svc.cortex_lookup("pull-235", "branch")["entity"] == "pr-235"
    # Aliases present, none of them holding the attribute: still None.
    assert svc.cortex_lookup("PR #235", "reviewer") is None


def test_cortex_lookup_canonical_keyed_record_wins_over_alias_keyed(
        pg_conn, pg_url, tmp_path):
    """Lookup order is direct hit, then canonical, then aliases: a record
    written under the canonical is served over an alias-keyed one at the
    same attribute, and the alias spelling still serves its own record."""
    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.cortex_write("pr-235", "branch", "alias-keyed-branch", support="user")
    svc.cortex_write("PR #235", "branch", "canonical-keyed-branch",
                     support="user")
    _merge_alias_into_canonical(svc)

    assert svc.cortex_lookup("PR #235", "branch")["value"] == (
        "canonical-keyed-branch")
    assert svc.cortex_lookup("pr-#235", "branch")["value"] == (
        "canonical-keyed-branch")
    assert svc.cortex_lookup("pr-235", "branch")["value"] == (
        "alias-keyed-branch")


def test_cortex_lookup_set_slot_under_an_alias(pg_conn, pg_url, tmp_path):
    """The set-slot fallback walks the same widened name list, so a set
    written under the alias is served, in set shape, via the canonical."""
    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.set_add("pr-235", "reviewers", "alice")
    svc.set_add("pr-235", "reviewers", "bob")
    _merge_alias_into_canonical(svc)

    got = svc.cortex_lookup("PR #235", "reviewers")
    assert got is not None and got["kind"] == "set"
    assert got["entity"] == "pr-235"
    assert [m["value"] for m in got["members"]] == ["alice", "bob"]
    # And from a second alias, where the set sits behind the canonical retry.
    svc.graph_alias("PR #235", "pull-235")
    via = svc.cortex_lookup("pull-235", "reviewers")
    assert via is not None and via["kind"] == "set" and via["entity"] == "pr-235"


def test_chain_includes_alias_keyed_slot_history(pg_conn, pg_url, tmp_path):
    """``chain()`` keys its fact stream on the queried name, the canonical
    AND the node's aliases, so the assertion / supersession history of a
    slot written under a merged-away name shows under the surviving node,
    reached through either name."""
    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    svc.cortex_write("pr-235", "branch", "old-branch", support="user")
    svc.cortex_write("pr-235", "branch", "new-branch", support="user")
    _merge_alias_into_canonical(svc)

    out = svc.chain("PR #235")
    assert out["found"] is True and out["entity"] == "PR #235"
    facts = [(e["kind"], e["summary"]) for e in out["events"]
             if e["kind"] in ("fact_set", "superseded")]
    assert ("fact_set", "branch = old-branch") in facts
    assert ("fact_set", "branch = new-branch") in facts
    assert ("superseded", "branch: old-branch superseded by new-branch") in facts
    # Reaching the node through the alias yields the same fact history.
    via_alias = svc.chain("pr-235")
    assert [(e["kind"], e["summary"]) for e in via_alias["events"]
            if e["kind"] in ("fact_set", "superseded")] == facts
