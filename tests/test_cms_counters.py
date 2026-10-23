"""The CMS counters in the bank (schema v57, decision D2).

Until v57 the counters (interaction and logical-turn counts, retrieval hits,
surprise history, consolidation events) lived only in
``<data_dir>/memory_state/weights.pt``, a torch file no backup dump, transfer
or Rust daemon reads. Storage mode now keeps them in the one-row
``cms_counters`` table and imports an existing ``weights.pt`` once, at the
first start on a bank without the row.
"""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.utils.atomic_io import atomic_torch_save


class _Embedder:
    """Enough of EmbeddingPipeline to start a service; nothing here embeds."""

    backend = "torch"
    embedding_dim = 1024


def _service(tmp_path, monkeypatch, url):
    from pseudolife_memory.service import MemoryService

    monkeypatch.setattr("pseudolife_memory.service.EmbeddingPipeline",
                        lambda config: _Embedder())
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", url)
    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()  # noqa: SLF001 — the start path under test
    return svc


def _close(svc) -> None:
    svc._storage.close()  # noqa: SLF001


def _count(svc, interactions: int, turns: int, queries: int) -> None:
    cms = svc._cms  # noqa: SLF001
    band = cms.bands[0].name
    cms._interaction_count = interactions
    cms._logical_turn_count = turns
    cms._tier_queries = queries
    cms._tier_hits[band] = queries - 1
    cms._surprise_history[band] = [0.25, 0.5]
    cms._consolidation_events = [{"timestamp": 1.0, "from_bank": band,
                                  "to_bank": band, "entries_moved": 2}]


def _legacy_weights(path, interactions: int) -> None:
    """A weights.pt as pre-v57 ``save_weights`` wrote it."""
    path.mkdir(parents=True, exist_ok=True)
    atomic_torch_save({
        "schema_version": 9, "kind": "weights", "preset_name": "flat",
        "interaction_count": interactions, "logical_turn_count": 7,
        "surprise_history": {"flat": [0.75]}, "consolidation_events": [],
        "tier_hits": {"flat": 3}, "tier_queries": 4,
    }, path / "weights.pt")


def test_counters_survive_a_restart_in_the_bank(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        _count(svc, interactions=41, turns=9, queries=12)
        expected = svc._cms.counters_state()  # noqa: SLF001
        svc.save()
    finally:
        _close(svc)
    assert not (tmp_path / "memory_state" / "weights.pt").exists()

    again = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert again._cms.counters_state() == expected  # noqa: SLF001
        stats = again.stats()
        assert stats["interaction_count"] == 41
        assert stats["retrieval_queries"] == 12
        assert stats["weights_reset"] is False
    finally:
        _close(again)


def test_a_pre_v57_weights_file_is_imported_once(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """The first start on a bank without the row imports weights.pt and
    writes the row at once; the file stays, and a later start reads the row,
    never the file again."""
    state_dir = tmp_path / "memory_state"
    _legacy_weights(state_dir, interactions=500)

    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert svc._cms._interaction_count == 500  # noqa: SLF001
        row = svc._storage.cms_counters_get()  # noqa: SLF001
        assert row["interaction_count"] == 500
        assert row["tier_hits"]["flat"] == 3 and row["tier_queries"] == 4
    finally:
        _close(svc)
    assert (state_dir / "weights.pt").exists()

    _legacy_weights(state_dir, interactions=1)  # a stale file left behind
    again = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert again._cms._interaction_count == 500  # noqa: SLF001
    finally:
        _close(again)


def test_the_import_falls_back_to_the_backup_file(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    state_dir = tmp_path / "memory_state"
    _legacy_weights(state_dir, interactions=60)
    _legacy_weights(state_dir, interactions=61)  # rotates 60 into .bak
    (state_dir / "weights.pt").write_bytes(b"torn write")

    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert svc._storage.cms_counters_get()["interaction_count"] == 60  # noqa: SLF001
        assert svc._cms.weights_reset is False  # noqa: SLF001
    finally:
        _close(svc)


def test_an_unreadable_weights_file_starts_fresh_and_says_so(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    state_dir = tmp_path / "memory_state"
    state_dir.mkdir(parents=True)
    (state_dir / "weights.pt").write_bytes(b"junk")

    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert svc.stats()["weights_reset"] is True
        assert svc._storage.cms_counters_get() is None  # noqa: SLF001
        svc.save()
        assert svc._storage.cms_counters_get()["interaction_count"] == 0  # noqa: SLF001
    finally:
        _close(svc)


def test_a_failed_counters_read_fails_the_start(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """Serving on with fresh counters after a failed read would let the next
    autosave overwrite the real row, so the start fails like any other
    failed hydration and is retried."""
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.storage.postgres import PostgresStorage

    def unavailable(self):
        raise RuntimeError("counters read failed")

    monkeypatch.setattr(PostgresStorage, "cms_counters_get", unavailable)
    monkeypatch.setattr("pseudolife_memory.service.EmbeddingPipeline",
                        lambda config: _Embedder())
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    try:
        with pytest.raises(Exception, match="counters read failed"):
            svc._ensure_init()  # noqa: SLF001
        assert svc._cms is None  # noqa: SLF001 — nothing half-built is served
    finally:
        if svc._storage is not None:  # noqa: SLF001
            svc._storage.close()  # noqa: SLF001
