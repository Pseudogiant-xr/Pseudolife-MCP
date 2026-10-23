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
    """The first start on a bank without the row imports weights.pt, writes
    the row at once and renames the file *.imported (never deleted), so a
    later bank cannot import counters frozen at the upgrade."""
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
    assert not (state_dir / "weights.pt").exists()
    assert (state_dir / "weights.pt.imported").exists()

    again = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert again._cms._interaction_count == 500  # noqa: SLF001
    finally:
        _close(again)


def test_a_start_refused_for_a_newer_bank_leaves_the_counters_alone(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """A bank a newer build upgraded is refused before anything is read
    (schema.ensure_schema), so weights.pt keeps its name and no counters
    row is written: rolling back to the build that wrote the file, or
    restarting a newer one, still finds both as they were."""
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    state_dir = tmp_path / "memory_state"
    _legacy_weights(state_dir, interactions=500)
    rows = pg_conn.execute("SELECT count(*) FROM cms_counters").fetchone()[0]
    pg_conn.execute(
        "UPDATE meta SET value = %s::jsonb WHERE key = 'schema_version'",
        (str(SCHEMA_META_VERSION + 1),))
    pg_conn.commit()
    try:
        with pytest.raises(RuntimeError, match="is newer than this build"):
            _service(tmp_path, monkeypatch, pg_url)
        assert (state_dir / "weights.pt").exists()
        assert not list(state_dir.glob("*.imported*"))
        assert pg_conn.execute(
            "SELECT count(*) FROM cms_counters").fetchone()[0] == rows
    finally:
        pg_conn.rollback()
        pg_conn.execute(
            "UPDATE meta SET value = %s::jsonb WHERE key = 'schema_version'",
            (str(SCHEMA_META_VERSION),))
        pg_conn.commit()


def test_a_bank_without_the_row_does_not_import_a_set_aside_file(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """A fresh database (a transfer target, a recreated bank) on a data dir
    whose weights.pt was imported long ago starts fresh counters rather than
    the ones frozen at the upgrade."""
    state_dir = tmp_path / "memory_state"
    _legacy_weights(state_dir, interactions=500)
    _close(_service(tmp_path, monkeypatch, pg_url))
    pg_conn.execute("DELETE FROM cms_counters")
    pg_conn.commit()

    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert svc._cms._interaction_count == 0  # noqa: SLF001
        assert svc._storage.cms_counters_get() is None  # noqa: SLF001
    finally:
        _close(svc)


def test_a_file_newer_than_the_row_is_imported_and_an_older_one_set_aside(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """After a rollback an older release writes weights.pt again; upgrading
    once more imports that newer file instead of dropping its counters. A
    file older than the row is set aside and the row wins."""
    import os
    import time

    state_dir = tmp_path / "memory_state"
    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        _count(svc, interactions=10, turns=1, queries=2)
        svc.save()
        saved_at = svc._storage.cms_counters_get()["updated_at"]  # noqa: SLF001
    finally:
        _close(svc)

    _legacy_weights(state_dir, interactions=99)  # the rollback's file
    later = saved_at + 60
    os.utime(state_dir / "weights.pt", (later, later))
    again = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert again._cms._interaction_count == 99  # noqa: SLF001
        assert again._storage.cms_counters_get()["interaction_count"] == 99  # noqa: SLF001
    finally:
        _close(again)
    assert not (state_dir / "weights.pt").exists()

    _legacy_weights(state_dir, interactions=5)  # older than the row now
    earlier = time.time() - 3600
    os.utime(state_dir / "weights.pt", (earlier, earlier))
    third = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert third._cms._interaction_count == 99  # noqa: SLF001
    finally:
        _close(third)
    assert not (state_dir / "weights.pt").exists()


def test_a_failed_counters_write_does_not_stop_the_save(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """The counters are a cache the next save rewrites; a failed write of
    the row is logged and the cortex, world and lesson saves still run."""
    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        def broken(state):
            raise RuntimeError("lock timeout")

        saved = []
        monkeypatch.setattr(svc._storage, "cms_counters_put", broken)  # noqa: SLF001
        monkeypatch.setattr(svc, "_save_cortex", lambda: saved.append("cortex"))
        svc._persist_all(kind="auto")  # noqa: SLF001
        assert saved == ["cortex"]
    finally:
        _close(svc)


def test_a_failed_counters_write_is_retried_by_the_next_autosave(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """The autosave skips a save while entries and cortex are unchanged.
    A counters write that failed is retried by the next autosave anyway,
    not left stale until something else changes; once it lands, an
    unchanged service saves nothing again."""
    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        storage = svc._storage  # noqa: SLF001
        real_put = storage.cms_counters_put
        attempts = []

        def flaky(state):
            attempts.append(state["interaction_count"])
            if len(attempts) == 1:
                raise RuntimeError("lock timeout")
            real_put(state)

        monkeypatch.setattr(storage, "cms_counters_put", flaky)
        _count(svc, interactions=12, turns=3, queries=5)
        svc._last_saved_fingerprint = None  # noqa: SLF001 — force a first save
        svc.autosave_if_changed()
        assert attempts == [12]

        svc.autosave_if_changed()
        assert attempts == [12, 12]
        assert storage.cms_counters_get()["interaction_count"] == 12

        svc.autosave_if_changed()
        assert attempts == [12, 12]
    finally:
        _close(svc)


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


def _saved_row(tmp_path, monkeypatch, url, interactions):
    svc = _service(tmp_path, monkeypatch, url)
    try:
        _count(svc, interactions=interactions, turns=1, queries=2)
        svc.save()
        return svc._storage.cms_counters_get()["updated_at"]  # noqa: SLF001
    finally:
        _close(svc)


def test_a_newer_file_that_counts_fewer_interactions_keeps_the_row(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """A rollback's older release finds no weights.pt (it was set aside) and
    counts from zero; upgrading again must not replace months of counts with
    that window's. Counters only grow, so the smaller file is set aside,
    beside (never over) the copy set aside before."""
    import os

    state_dir = tmp_path / "memory_state"
    _legacy_weights(state_dir, interactions=400)  # the upgrade-day file
    _close(_service(tmp_path, monkeypatch, pg_url))
    saved_at = _saved_row(tmp_path, monkeypatch, pg_url, interactions=900)
    first_copy = (state_dir / "weights.pt.imported").read_bytes()

    _legacy_weights(state_dir, interactions=12)  # the rollback window
    later = saved_at + 60
    os.utime(state_dir / "weights.pt", (later, later))
    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert svc._cms._interaction_count == 900  # noqa: SLF001
        assert svc._storage.cms_counters_get()["interaction_count"] == 900  # noqa: SLF001
    finally:
        _close(svc)
    assert (state_dir / "weights.pt.imported").read_bytes() == first_copy
    assert (state_dir / "weights.pt.imported-2").exists()
    assert not (state_dir / "weights.pt").exists()


def test_freshness_is_judged_by_the_file_actually_read(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """A torn weights.pt newer than the row falls back to a .bak older than
    the row; that stale .bak must not replace the row."""
    import os

    state_dir = tmp_path / "memory_state"
    saved_at = _saved_row(tmp_path, monkeypatch, pg_url, interactions=50)
    _legacy_weights(state_dir, interactions=1000)
    _legacy_weights(state_dir, interactions=2000)  # rotates 1000 into .bak
    old = saved_at - 3600
    os.utime(state_dir / "weights.pt.bak", (old, old))
    (state_dir / "weights.pt").write_bytes(b"torn")
    later = saved_at + 60
    os.utime(state_dir / "weights.pt", (later, later))

    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert svc._cms._interaction_count == 50  # noqa: SLF001
    finally:
        _close(svc)


@pytest.mark.parametrize("payload", [
    ["not", "a", "dict"],
    {"interaction_count": 3, "surprise_history": ["a", "list"]},
    {"interaction_count": None},
], ids=["list", "history-list", "none-count"])
def test_a_malformed_weights_file_does_not_block_the_start(
        tmp_path, pg_url, pg_conn, monkeypatch, payload):  # noqa: F811
    """A file that loads but is not counters is a bad file, as an unreadable
    one is: logged, skipped and reported as weights_reset, never a start
    that fails on every retry."""
    state_dir = tmp_path / "memory_state"
    state_dir.mkdir(parents=True)
    atomic_torch_save(payload, state_dir / "weights.pt")

    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert svc.stats()["weights_reset"] is True
        assert svc._cms._interaction_count == 0  # noqa: SLF001
    finally:
        _close(svc)
