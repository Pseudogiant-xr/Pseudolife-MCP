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
    v = torch.randn(1024, generator=g)
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


def test_promotion_moves_band_in_storage(storage):
    # Promotion needs a deeper band — the retained continuum preset
    # (the flat default has nowhere to promote to).
    from pseudolife_memory.utils.config import MIRASConfig
    cfg = MemoryConfig()
    cfg.miras = MIRASConfig(preset="continuum")
    cms = ContinuumMemorySystem(cfg, storage=storage)
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
    svc.config.memory.cortex.auto_promote = True   # opt-in (default off)
    r = svc.store("the quorvax pipeline default timeout is 250 ms",
                  source="wt-test")
    assert r["stored"] is True and r["cortex_promoted"] == 1
    svc.cortex_write("quorvax", "owner", "alice", support="user")
    svc.episode_start("restart check")
    svc.flush()
    svc._storage.close()  # the first daemon exits, releasing the bank

    svc2 = MemoryService(data_dir=tmp_path, database_url=pg_url)
    s = svc2.search("what is the quorvax timeout?")
    assert s["count"] >= 1 and "250 ms" in s["entries"][0]["text"]
    fact = svc2.cortex_lookup("quorvax", "owner")
    assert fact is not None and fact["value"] == "alice"
    eps = svc2.episode_list()
    assert any(e["title"] == "restart check" for e in eps["episodes"])


# ── Supersession marks ────────────────────────────────────────────────────
# ``supersede`` and ``consolidate`` mark band entries in memory
# (``superseded_at`` / ``superseded_by_text``). ``_persist_all`` syncs only
# ``access_count`` for entries, so a mark that is not written through when it
# is set is lost at the next ``hydrate_cms`` and the corrected entry comes
# back looking current. Asserted through a SECOND service on the same DSN —
# that is the restart the loss actually manifests in.


def _rehydrated(live, tmp_path, pg_url, text):
    """Stop ``live`` (a bank has one writer) and read ``text`` back through
    a restarted service, which releases the bank again afterwards."""
    from pseudolife_memory.service import MemoryService

    live._storage.close()
    svc = MemoryService(data_dir=tmp_path / "restart", database_url=pg_url)
    try:
        recent = svc.recent(n=50)
    finally:
        svc._storage.close()
    return next(e for e in recent["entries"] if e["text"] == text)


def test_consolidate_supersession_survives_restart(pg_conn, pg_url, tmp_path):
    """Exact-text match: the mark must reach Postgres, not just RAM."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path / "live", database_url=pg_url)
    svc.store("fact A v1", source="wt-test")

    out = svc.consolidate(
        replaces=["fact A v1"], new_text="Consolidated: fact A current",
    )
    assert out["superseded_count"] == 1

    entry = _rehydrated(svc, tmp_path, pg_url, "fact A v1")
    assert entry["superseded"] is True
    assert entry["superseded_by_text"] == "Consolidated: fact A current"


@pytest.mark.real_model
def test_consolidate_paraphrase_refusal_survives_restart(
    pg_conn, pg_url, tmp_path,
):
    """A close paraphrase must not change a source row or insert a note."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path / "live", database_url=pg_url)
    svc.store("the deploy target is the staging cluster", source="wt-test")

    out = svc.consolidate(
        replaces=["deploy target: staging cluster"],
        new_text="Consolidated: the deploy target is production",
    )
    assert out["superseded_count"] == 0
    assert out["new_memory_stored"] is False
    assert out["reason"] == "target_not_found"
    assert len(svc._storage.load_entries()) == 1

    entry = _rehydrated(
        svc, tmp_path, pg_url, "the deploy target is the staging cluster",
    )
    assert entry["superseded"] is False
    assert entry["superseded_by_text"] is None


def test_supersede_supersession_survives_restart(pg_conn, pg_url, tmp_path):
    """The reference implementation, pinned so it cannot regress alongside."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path / "live", database_url=pg_url)
    svc.store("Sky is green", source="wt-test")

    out = svc.supersede("Sky is green", "Sky is blue")
    assert out["superseded_count"] == 1

    entry = _rehydrated(svc, tmp_path, pg_url, "Sky is green")
    assert entry["superseded"] is True
    assert entry["superseded_by_text"] == "Sky is blue"


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
def test_superseded_hit_names_its_successor_row_after_restart(
    operation, pg_conn, pg_url, tmp_path,
):
    """Entries store the replacement's text, not its id, so the successor
    is resolved by exact text over the resident entries at serve time. It
    must name the replacement's real row id and mark an explicit
    correction verified, on both serving surfaces, after a rehydrate."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path / "live", database_url=pg_url)
    old_text, new_text = "Sky is green", "Consolidated: sky is blue"
    svc.store(old_text, source="wt-test")
    if operation == "supersede":
        svc.supersede(old_text, new_text)
    else:
        svc.consolidate([old_text], new_text)

    # "The daemon stopped": end the first service's Postgres session before
    # the restart builds a second one on the same database. Not touched
    # again — its storage would reconnect on next use.
    svc._storage.close()
    restarted = MemoryService(data_dir=tmp_path / "restart",
                              database_url=pg_url)
    recent = restarted.recent(n=50)["entries"]
    new_id = next(e["id"] for e in recent if e["text"] == new_text)
    assert isinstance(new_id, int)
    for entries in (recent, restarted.search(old_text)["entries"]):
        old = next(e for e in entries if e["text"] == old_text)
        assert old["superseded_by_id"] == new_id
        assert old["supersession_verified"] is True
        assert isinstance(old["superseded_at"], float)


def test_memory_get_reports_supersession_only_on_a_superseded_entry(
    pg_conn, pg_url, tmp_path,
):
    """``memory_get`` is how an agent dereferences ``replaced_by.id``, so
    the fetched entry must say when it is itself superseded — with the
    same successor annotation search serves, one link at a time. A live
    entry's payload keeps exactly its pre-existing keys."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path / "live", database_url=pg_url)
    v1, v2, v3 = "Sky is green", "Sky is teal", "Sky is blue"
    svc.store(v1, source="wt-test")
    svc.supersede(v1, v2)
    svc.supersede(v2, v3)
    ids = {e["text"]: e["id"] for e in svc.recent(n=10)["entries"]}

    live = svc.get_entry(ids[v3])
    assert set(live) == {"found", "entry_id", "text", "source",
                         "reinforcements", "explicit_reinforcements",
                         "access_count", "consolidated_into"}

    first = svc.get_entry(ids[v1])
    assert first["superseded"] is True
    assert isinstance(first["superseded_at"], float)
    assert first["superseded_by_text"] == v2
    # Names the middle link, not the end of the chain, and says so.
    assert (first["superseded_by_id"], first["supersession_verified"],
            first["superseded_by_current"]) == (ids[v2], True, False)
    middle = svc.get_entry(ids[v2])
    assert (middle["superseded_by_id"], middle["superseded_by_current"]) == (
        ids[v3], True)


def test_memory_get_reads_supersession_from_the_row_it_serves(
    pg_conn, pg_url, tmp_path,
):
    """``memory_get`` serves a Postgres row, so its supersession state
    comes from that row too: a superseded row the CMS does not hold (a
    failed eviction write-through, or reinstatement recovery mid-flight)
    must not be served as live. The successor is still resolved over the
    resident entries, like search."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path / "live", database_url=pg_url)
    svc.store("Sky is green", source="wt-test")
    svc.supersede("Sky is green", "Sky is blue")
    ids = {e["text"]: e["id"] for e in svc.recent(n=10)["entries"]}
    for band in svc._cms.bands:
        band.entries = [e for e in band.entries
                        if e.db_id != ids["Sky is green"]]
    got = svc.get_entry(ids["Sky is green"])
    assert got["superseded"] is True
    assert (got["superseded_by_id"], got["superseded_by_current"]) == (
        ids["Sky is blue"], True)


def test_memory_get_never_names_the_entry_itself_as_its_successor(
    pg_conn, pg_url, tmp_path,
):
    """A verbatim re-assertion (superseded by its own text) that was later
    corrected leaves two retired entries with one text. The fetched entry
    is excluded from its own candidates by id, so the re-assertion is named
    (and marked not current) instead of the pair reading as ambiguous."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path / "live", database_url=pg_url)
    svc.store("Sky is green", source="wt-test")
    svc.supersede("Sky is green", "Sky is green")
    rows = svc._storage.conn.execute(
        "SELECT id, superseded_at FROM entries WHERE text = %s ORDER BY id",
        ("Sky is green",)).fetchall()
    (original, retired_at), (reassertion, live_at) = rows
    assert retired_at is not None and live_at is None
    svc.supersede(entry_id=reassertion, new_text="Sky is blue")
    got = svc.get_entry(original)
    assert (got["superseded_by_id"], got["superseded_by_current"]) == (
        reassertion, False)


@pytest.mark.parametrize('operation', ['supersede', 'consolidate'])
def test_explicit_replacement_bypasses_surprise_and_survives_restart(
    operation, pg_conn, pg_url, tmp_path, monkeypatch,
):
    """Retiring a selected note must not leave its replacement unrecorded."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_path / 'live', database_url=pg_url)
    old_text = 'The deployment target is staging.'
    new_text = 'The deployment target is production.'
    assert svc.store(old_text, source='test')['stored'] is True
    svc.config.memory.surprise_threshold = 0.5
    for band in svc._cms.bands:
        monkeypatch.setattr(band, 'compute_surprise', lambda _: 0.0)
    assert svc.config.memory.surprise_threshold > 0.0

    if operation == 'supersede':
        result = svc.supersede(old_text, new_text)
    else:
        result = svc.consolidate([old_text], new_text)

    assert result['superseded_count'] == 1
    assert result['new_memory_stored'] is True
    assert _rehydrated(svc, tmp_path, pg_url, old_text)['superseded'] is True
    replacement = _rehydrated(svc, tmp_path, pg_url, new_text)
    assert replacement['superseded'] is False


def test_possible_partial_correction_preserves_source_in_postgres(storage):
    """A candidate conflict must not retire the rest of a persisted note."""
    from pseudolife_memory.storage.sync import hydrate_cms

    cfg = MemoryConfig()
    cfg.surprise_threshold = -1.0
    cms = ContinuumMemorySystem(cfg, storage=storage)
    old_text = 'I have a cat named Mira. Its veterinary records are in the blue folder.'
    update = 'I no longer have a cat named Mira.'
    vector = _emb(100)
    assert cms.store(old_text, vector, source='notes')[0]
    old = _mem_view(cms)[old_text]
    original_surprise = old.surprise_score
    assert cms.store(update, vector, source='status')[0]

    current = _pg_view(storage)[old_text]
    assert current['superseded_at'] is None
    assert current['superseded_by_text'] is None
    assert current['surprise'] == pytest.approx(original_surprise)

    restarted = ContinuumMemorySystem(cfg, storage=storage)
    hydrate_cms(restarted, storage)
    restored = _mem_view(restarted)
    assert set(restored) == {old_text, update}
    assert restored[old_text].superseded_at is None
    assert restored[old_text].surprise_score == pytest.approx(original_surprise)
