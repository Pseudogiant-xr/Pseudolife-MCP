"""One-time cortex sibling-slot cleanup (``cortex_dedup``).

Collapses paraphrase fragments past auto-promotes left behind, keyed on the
value-free slot embedding. PG-backed (real embedder); skips cleanly without a
test server. Measured slot cosines: ``payments-db host`` vs ``payments database
host`` ~0.95 (merge); ``invoice-service port`` vs ``region`` ~0.70 and
``ledger-db engine`` vs ``ledger-cache engine`` ~0.81 (stay distinct at 0.90).
"""
from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    s = MemoryService(data_dir=tmp_path, database_url=pg_url)
    yield s
    s.flush()


def test_cortex_dedup_merges_siblings(svc):
    # Two paraphrased slots for one fact (as past auto-promote would have forked).
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.cortex_write("payments database", "host", "db-prod-2", support="agent")

    rep = svc.cortex_dedup(threshold=0.85, dry_run=True)
    assert rep["merged"] >= 1                               # dry-run REPORTS a merge
    # ...but mutates nothing:
    assert svc.cortex_lookup("payments-db", "host")["value"] == "db-prod-1"
    assert svc.cortex_lookup("payments database", "host")["value"] == "db-prod-2"

    rep2 = svc.cortex_dedup(threshold=0.85, dry_run=False)  # apply
    assert rep2["merged"] >= 1
    survivors = svc._cortex.current_records()
    assert len(survivors) == 1                              # one canonical remains


def test_cortex_dedup_leaves_distinct_slots(svc):
    svc.cortex_write("invoice-service", "port", "7000", support="agent")
    svc.cortex_write("invoice-service", "region", "us-west-2", support="agent")

    rep = svc.cortex_dedup(threshold=0.90, dry_run=False)
    assert rep["merged"] == 0
    assert svc.cortex_lookup("invoice-service", "port")["value"] == "7000"
    assert svc.cortex_lookup("invoice-service", "region")["value"] == "us-west-2"


def test_cortex_dedup_canonical_prefers_user_tier(svc):
    svc.cortex_write("payments-db", "host", "db-prod-1", support="agent")
    svc.cortex_write("payments database", "host", "db-user", support="user")

    svc.cortex_dedup(threshold=0.85, dry_run=False)
    cur = svc._cortex.current_records()
    assert len(cur) == 1 and cur[0].value == "db-user"     # user tier wins


def test_cortex_dedup_backfill_skips_members(svc):
    """F1 (bank-corrupting, review-caught): the backfill loop must not give
    member records a slot embedding. All members of one slot share the same
    ``(entity, attribute)``, so backfilling the value-free
    ``f"{entity} {attribute}"`` embedding onto every member would make them
    cosine-IDENTICAL to each other; ``dedup_siblings`` would then cluster
    and supersede all but one, silently destroying the set. Members must
    come out of ``cortex_dedup`` untouched (no ``slot_embedding`` set) and
    all still current. Member values are deliberately semantically distinct
    (not "alpha"/"beta"/"gamma", whose real embeddings run above
    ``MEMBER_DEDUP_COSINE`` and would confirm-merge at insertion regardless
    of this bug) so a failure here can only be the backfill-loop defect."""
    svc.set_add("user", "tags", "mountain biking")
    svc.set_add("user", "tags", "competitive chess")
    svc.set_add("user", "tags", "watercolor painting")

    svc.cortex_dedup(threshold=0.85, dry_run=False)

    members = svc._cortex.members("user", "tags")
    assert len(members) == 3, [m.value for m in members]
    assert all(m.status == "current" for m in members)
    assert all(m.slot_embedding is None for m in members)
