"""Dismissed duplicate pairs (2026-07-02 review fix 3).

The duplicate analyzer is stateless, so its token-Jaccard false positives
(postgres vs postgres.py, Atlas Review vs atlas_review.js, ...) re-flagged on
every run. These tests pin the persistence: a dismissed pair is stored
normalized + ordered, survives re-analysis, and is exposed end-to-end via
service.graph_dismiss_duplicate.
"""

from __future__ import annotations

import tempfile

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage
    st = PostgresStorage(pg_url)
    try:
        yield st
    finally:
        st.close()


def test_dismiss_pair_roundtrip_order_insensitive(storage):
    assert storage.dismiss_pair("zeta-norm", "alpha-norm") is True
    assert ("alpha-norm", "zeta-norm") in storage.dismissed_pairs()
    # idempotent: a second dismissal of either ordering inserts nothing
    assert storage.dismiss_pair("alpha-norm", "zeta-norm") is False


@pytest.fixture()
def svc(pg_conn, pg_url):
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def _dup_pairs(svc):
    out = svc.graph_review()
    return {frozenset(f["entities"]) for f in out["findings"]
            if f["type"] == "duplicate"}


def test_service_dismiss_duplicate_hides_finding_permanently(svc):
    svc.graph_relate("memcot_bench.py", "related-to", "memcot bench")
    pair = frozenset({"memcot_bench.py", "memcot bench"})
    assert pair in _dup_pairs(svc), "precondition: analyzer flags the pair"

    out = svc.graph_dismiss_duplicate("memcot bench", "memcot_bench.py")

    assert out["dismissed"] is True
    assert pair not in _dup_pairs(svc), "dismissed pair must not re-flag"
