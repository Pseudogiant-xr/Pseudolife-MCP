"""Write-through consistency: in-memory view == Postgres view after each op.

CMS-level tests use random embeddings (no embedder load — fast). One
service-level test exercises the full restart cycle with the real
embedder.
"""

from __future__ import annotations

import pytest
import torch

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import MemoryConfig


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(384, generator=g)
    return v / v.norm()


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    s = PostgresStorage(pg_url)
    yield s
    s.close()


@pytest.fixture()
def cms(storage):
    return ContinuumMemorySystem(MemoryConfig(), storage=storage)


def _pg_view(storage) -> dict[str, dict]:
    return {r["text"]: r for r in storage.load_entries()}


def _mem_view(cms) -> dict[str, object]:
    return {e.text: e for b in cms.bands for e in b.entries}


def _assert_consistent(cms, storage):
    pg = _pg_view(storage)
    mem = _mem_view(cms)
    assert set(pg) == set(mem)
    for text, entry in mem.items():
        row = pg[text]
        assert entry.db_id == row["id"]
        assert entry.bank == row["band"]
        assert (entry.superseded_at is None) == (row["superseded_at"] is None)


def test_store_writes_through(cms, storage):
    cms.store("write-through fact one", _emb(1), source="t", tags=["wt"])
    cms.store("write-through fact two", _emb(2), source="t")
    # NB: fresh-bank stores promote out of band 0 on the same call
    # (surprise 1.0 > promotion threshold) — _assert_consistent checks
    # the band column matches wherever each entry actually lives.
    _assert_consistent(cms, storage)
    assert _pg_view(storage)["write-through fact one"]["tags"] == ["wt"]


def test_promotion_moves_band_in_storage(cms, storage):
    cms.store("promotable fact", _emb(3), source="t")
    src_idx, entry = next(
        (i, e) for i, b in enumerate(cms.bands)
        for e in b.entries if e.text == "promotable fact"
    )
    entry.access_count = 99  # exceed promotion threshold
    cms._consolidate(src_idx, src_idx + 1)
    assert (_pg_view(storage)["promotable fact"]["band"]
            == cms.bands[src_idx + 1].name)
    _assert_consistent(cms, storage)


def test_delete_removes_rows(cms, storage):
    cms.store("doomed fact", _emb(4), source="junk")
    cms.store("kept fact", _emb(5), source="t")
    removed = cms.delete_entries(source="junk")
    assert removed == ["doomed fact"]
    assert set(_pg_view(storage)) == {"kept fact"}
    _assert_consistent(cms, storage)


def test_hydration_restores_bank(cms, storage):
    cms.store("survives restart alpha", _emb(6), source="t", tags=["h"])
    cms.store("survives restart beta", _emb(7), source="t")
    cms.episodes.start("hydration session")
    cms.store("episodic gamma", _emb(8), source="t")

    from pseudolife_memory.storage.sync import episode_row, hydrate_cms
    for ep in cms.episodes.episodes.values():
        storage.upsert_episode(episode_row(ep))

    cms2 = ContinuumMemorySystem(MemoryConfig(), storage=storage)
    n = hydrate_cms(cms2, storage)
    assert n == 3
    _assert_consistent(cms2, storage)
    mem = _mem_view(cms2)
    assert mem["episodic gamma"].episode_title == "hydration session"
    assert mem["survives restart alpha"].tags == ["h"]
    # The hydrated open episode is still current.
    assert cms2.episodes.current_id is not None
    # Retrieval works over hydrated entries.
    result = cms2.retrieve(_emb(6), top_k=2, query_text="survives restart alpha")
    assert any(e.text == "survives restart alpha" for e in result.entries)


def test_service_restart_roundtrip(pg_conn, pg_url, tmp_path):
    """Full service cycle with the real embedder: store + fact_set →
    new service instance → search + fact_get see everything."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)
    r = svc.store("the quorvax pipeline default timeout is 250 ms",
                  source="wt-test")
    assert r["stored"] is True and r["cortex_promoted"] == 1
    svc.cortex_write("quorvax", "owner", "alice", support="user")
    svc.episode_start("restart check")
    svc.flush()

    svc2 = MemoryService(data_dir=tmp_path, database_url=pg_url)
    s = svc2.search("what is the quorvax timeout?")
    assert s["count"] >= 1 and "250 ms" in s["entries"][0]["text"]
    fact = svc2.cortex_lookup("quorvax", "owner")
    assert fact is not None and fact["value"] == "alice"
    eps = svc2.episode_list()
    assert any(e["title"] == "restart check" for e in eps["episodes"])
