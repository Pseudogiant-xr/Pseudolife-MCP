"""Capacity guard (2026-09-23): warn before capacity eviction deletes, and
keep a durable record of every true drop.

Under the flat default every capacity eviction is a true drop — a Postgres
``DELETE`` with ``memory_traces`` cascading — and superseded entries go
first. The 2026-09-23 review measured the production bank reaching the
5,250-entry cap between roughly 9 Nov and 18 Dec 2026, with nothing to say
so beforehand: ``true_drops`` was a per-process counter that every restart
reset, and the only trace of a drop was an INFO log line.

These tests pin the three guards: ``capacity_warning`` at 80% of the band
whose evictions are true drops, a WARNING line per drop naming the entry,
and a durable ``meta`` record of the drop count that survives a restart and
commits or rolls back with the ``DELETE`` it describes.
"""

from __future__ import annotations

import logging
import time

import pytest
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import MemoryConfig, MIRASBandSpec
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401

DIM = 8


def _band(name: str, cap: int) -> MIRASBandSpec:
    return MIRASBandSpec(
        name=name, max_entries=cap, update_interval=1_000_000_000,
        promotion_access_count=1_000_000_000, promotion_surprise=1.1,
        retention_policy="balanced")


def _cfg(*bands: tuple[str, int]) -> MemoryConfig:
    cfg = MemoryConfig(embedding_dim=DIM)
    cfg.miras.preset = "custom"
    cfg.miras.bands = [_band(name, cap) for name, cap in bands]
    return cfg


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return torch.randn(DIM, generator=g)


def _fill(cms: ContinuumMemorySystem, n: int, *, start: int = 0) -> None:
    for i in range(start, start + n):
        cms.store(f"turn {i}", _emb(i), source="user")


class _EvictRecorder:
    """Storage double exposing the audited eviction write-through."""

    def __init__(self):
        self.evicted: list[tuple] = []
        self.deleted: list[int] = []
        self._next_id = 100

    def insert_entry(self, *args, **fields):
        self._next_id += 1
        return self._next_id

    def delete_evicted_entry(self, entry_id, *, source, superseded):
        self.evicted.append((entry_id, source, superseded))
        return len(self.evicted)

    def delete_entry_ids(self, ids):
        self.deleted.extend(ids)
        return len(ids)

    def __getattr__(self, name):
        return lambda *a, **k: None


# ── capacity_warning ────────────────────────────────────────────────────


class TestCapacityWarning:
    def test_threshold_is_eighty_percent(self):
        from pseudolife_memory.memory.cms import CAPACITY_WARNING_FRACTION

        assert CAPACITY_WARNING_FRACTION == 0.8

    def test_absent_below_the_threshold(self):
        cms = ContinuumMemorySystem(_cfg(("flat", 5)))
        _fill(cms, 3)
        stats = cms.stats()
        assert "capacity_warning" in stats
        assert stats["capacity_warning"] is None

    def test_fires_at_eighty_percent_of_the_flat_band(self):
        cms = ContinuumMemorySystem(_cfg(("flat", 5)))
        _fill(cms, 4)
        warning = cms.stats()["capacity_warning"]
        assert warning is not None
        assert warning["band"] == "flat"
        assert (warning["size"], warning["capacity"]) == (4, 5)
        assert warning["fill"] == pytest.approx(0.8)
        assert "permanently delete" in warning["message"]

    def test_says_drops_are_happening_once_full(self):
        cms = ContinuumMemorySystem(_cfg(("flat", 3)))
        _fill(cms, 5)
        warning = cms.stats()["capacity_warning"]
        assert warning["size"] == warning["capacity"] == 3
        assert "is full" in warning["message"]

    def test_a_full_demoting_band_is_not_a_warning(self):
        # Band "a" overflows into "b": being full there loses nothing, so
        # it must not raise the alarm. Only the terminal band's evictions
        # are true drops.
        cms = ContinuumMemorySystem(_cfg(("a", 2), ("b", 10)))
        _fill(cms, 4)
        assert cms.bands[0].size == cms.bands[0].max_entries
        assert cms.stats()["capacity_warning"] is None

    def test_the_terminal_band_filling_is_a_warning(self):
        cms = ContinuumMemorySystem(_cfg(("a", 2), ("b", 10)))
        _fill(cms, 10)
        assert cms.bands[1].size == 8
        warning = cms.stats()["capacity_warning"]
        assert warning is not None and warning["band"] == "b"


# ── per-drop WARNING line + durable write-through ───────────────────────


class TestTrueDropRecord:
    def test_true_drop_goes_through_the_audited_eviction(self):
        cms = ContinuumMemorySystem(_cfg(("flat", 3)))
        cms.storage = _EvictRecorder()
        _fill(cms, 3)
        victim = cms.bands[0].entries[1]
        victim.superseded_at = time.time()
        cms.store("turn overflow", _emb(99), source="user")

        assert cms.storage.evicted == [(victim.db_id, "user", True)]
        # One delete, not two: the audited path owns the row removal.
        assert cms.storage.deleted == []
        assert cms.stats()["true_drops"] == 1

    def test_true_drop_logs_a_warning_naming_the_entry(self, caplog):
        cms = ContinuumMemorySystem(_cfg(("flat", 3)))
        cms.storage = _EvictRecorder()
        _fill(cms, 3)
        victim = cms.bands[0].entries[0]
        victim.superseded_at = time.time()
        with caplog.at_level(logging.WARNING,
                             logger="pseudolife_memory.memory.cms"):
            cms.store("turn overflow", _emb(99), source="user")

        drops = [r for r in caplog.records
                 if r.levelno == logging.WARNING and "true drop" in r.message]
        assert len(drops) == 1
        line = drops[0].message
        assert f"entry_id={victim.db_id}" in line
        assert "source='user'" in line
        assert "superseded=True" in line
        # The durable all-time count the storage returned rides the line.
        assert "all-time 1" in line

    def test_file_mode_drop_still_logs_a_warning(self, caplog):
        cms = ContinuumMemorySystem(_cfg(("flat", 2)))
        _fill(cms, 2)
        with caplog.at_level(logging.WARNING,
                             logger="pseudolife_memory.memory.cms"):
            cms.store("turn overflow", _emb(99), source="user")
        assert any("true drop" in r.message and r.levelno == logging.WARNING
                   for r in caplog.records)

    def test_a_demotion_is_not_logged_as_a_drop(self, caplog):
        cms = ContinuumMemorySystem(_cfg(("a", 1), ("b", 5)))
        cms.storage = _EvictRecorder()
        with caplog.at_level(logging.WARNING,
                             logger="pseudolife_memory.memory.cms"):
            _fill(cms, 3)
        assert cms.storage.evicted == []
        assert not any("true drop" in r.message for r in caplog.records)


# ── the durable record in Postgres ──────────────────────────────────────


def _meta_record(conn):
    from pseudolife_memory.storage.postgres import CAPACITY_DROPS_META_KEY

    row = conn.execute(
        "SELECT value FROM meta WHERE key = %s",
        (CAPACITY_DROPS_META_KEY,)).fetchone()
    return None if row is None else row[0]


def _row_exists(conn, entry_id) -> bool:
    return conn.execute(
        "SELECT 1 FROM entries WHERE id = %s", (entry_id,)).fetchone() \
        is not None


class TestDurableTrueDropRecord:
    def test_evicted_entry_is_deleted_and_counted_in_meta(self, pg_service):
        svc = pg_service
        svc._cms.bands[0].max_entries = 3
        svc.store("The staging cluster runs on three nodes.", source="alpha")
        svc.store("Lunch on Friday is at the harbour cafe.", source="beta")
        svc.store("The release branch is cut every second Tuesday.",
                  source="gamma")
        victim = next(e for e in svc._cms.bands[0].entries
                      if e.source == "beta")
        victim.superseded_at = time.time()
        svc.store("Backups rotate after fourteen days.", source="delta")

        conn = svc._storage.conn
        assert not _row_exists(conn, victim.db_id)
        record = _meta_record(conn)
        assert record["count"] == 1
        assert record["last_entry_id"] == victim.db_id
        assert record["last_source"] == "beta"
        assert record["last_superseded"] is True
        assert record["last_at"] == pytest.approx(time.time(), abs=60)

        stats = svc.stats()
        assert stats["true_drops"] == 1
        assert stats["true_drops_total"] == 1
        assert stats["last_true_drop"]["entry_id"] == victim.db_id
        assert stats["last_true_drop"]["superseded"] is True

    def test_count_survives_a_restart(
        self, pg_service, pg_url, tmp_path, monkeypatch,
    ):
        from pseudolife_memory.service import MemoryService

        svc = pg_service
        svc._cms.bands[0].max_entries = 2
        svc.store("The staging cluster runs on three nodes.", source="a")
        svc.store("Lunch on Friday is at the harbour cafe.", source="b")
        svc.store("The release branch is cut every second Tuesday.",
                  source="c")
        assert svc.stats()["true_drops_total"] == 1

        # A restart: the first process lets go of the bank before the next
        # one opens it (one daemon, one bank).
        svc._storage.close()
        monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
        restarted = MemoryService(data_dir=tmp_path / "restarted")
        restarted._ensure_init()
        stats = restarted.stats()
        # The session counter resets with the process; the record does not.
        assert stats["true_drops"] == 0
        assert stats["true_drops_total"] == 1
        assert stats["last_true_drop"]["source"] in {"a", "b", "c"}

    def test_a_bank_that_never_dropped_reports_zero(self, pg_service):
        pg_service.store("One ordinary memory.", source="alpha")
        stats = pg_service.stats()
        assert stats["true_drops_total"] == 0
        assert stats["last_true_drop"] is None

    def test_record_increments_across_drops(self, pg_conn, pg_url):
        from pseudolife_memory.storage.postgres import PostgresStorage

        storage = PostgresStorage(pg_url)
        try:
            assert storage.delete_evicted_entry(
                None, source="first", superseded=False) == 1
            assert storage.delete_evicted_entry(
                None, source="second", superseded=True) == 2
            record = _meta_record(pg_conn)
            assert record["count"] == 2
            assert record["last_source"] == "second"
            assert record["last_entry_id"] is None
        finally:
            storage.close()

    def test_record_rolls_back_with_the_delete(self, pg_service):
        # A correction stages its store inside storage.transaction(); a
        # true drop inside a correction that then fails must leave neither
        # a deleted row nor a counted drop behind.
        svc = pg_service
        svc.store("The staging cluster runs on three nodes.", source="alpha")
        entry = svc._cms.bands[0].entries[0]
        storage = svc._storage

        with pytest.raises(RuntimeError, match="injected"):
            with storage.transaction():
                storage.delete_evicted_entry(
                    entry.db_id, source="alpha", superseded=False)
                raise RuntimeError("injected correction failure")

        assert _row_exists(storage.conn, entry.db_id)
        assert _meta_record(storage.conn) is None

    def test_a_failed_count_keeps_the_row(self, pg_service, monkeypatch):
        # Never deleted without being counted: when the meta write fails,
        # the DELETE in the same transaction must roll back with it.
        import pseudolife_memory.storage.postgres as pg

        svc = pg_service
        svc.store("The staging cluster runs on three nodes.", source="alpha")
        entry = svc._cms.bands[0].entries[0]
        monkeypatch.setattr(pg, "CAPACITY_DROPS_META_KEY", None)

        with pytest.raises(Exception):
            svc._storage.delete_evicted_entry(
                entry.db_id, source="alpha", superseded=False)

        assert _row_exists(svc._storage.conn, entry.db_id)

    def test_the_drop_record_travels_with_a_logical_export(self):
        # Decision, pinned: the record is audit history of THIS bank's
        # entries, and logical transfer carries entry ids verbatim — the id
        # gaps the record explains travel with it. It is neither build-owned
        # nor transient, so it is not in the transfer skip list.
        from pseudolife_memory.storage.postgres import CAPACITY_DROPS_META_KEY
        from pseudolife_memory.transfer_cli import _skip_meta_key

        assert not _skip_meta_key(CAPACITY_DROPS_META_KEY)


# ── /health ─────────────────────────────────────────────────────────────


class _HealthStub:
    _db_url = "postgresql://fake"
    _persist_errors = 0
    _init_refusal = None
    _storage = None
    _migration_partial = None


class _CmsWith:
    def __init__(self, warning):
        self._warning = warning

    def capacity_warning(self):
        if isinstance(self._warning, Exception):
            raise self._warning
        return self._warning


class TestHealthCapacityFlag:
    def _payload(self, cms):
        from pseudolife_memory.daemon import _build_health_payload

        stub = _HealthStub()
        stub._cms = cms
        return _build_health_payload(stub, token_present=False)

    def test_flag_is_set_without_touching_status(self):
        payload = self._payload(_CmsWith({"band": "flat", "fill": 0.83}))
        assert payload["capacity_warning"] is True
        # web/api.py serves any non-ok payload as 503, which the Docker
        # healthcheck treats as fatal: a filling bank must not restart
        # the daemon.
        assert payload["status"] == "ok"

    def test_no_counts_leak_on_the_unauthenticated_probe(self):
        payload = self._payload(_CmsWith({"band": "flat", "size": 4321}))
        assert payload["capacity_warning"] is True
        assert "4321" not in repr(payload)

    def test_absent_below_threshold_or_before_init(self):
        assert "capacity_warning" not in self._payload(_CmsWith(None))
        assert "capacity_warning" not in self._payload(None)

    def test_a_failing_read_never_fails_health(self):
        payload = self._payload(_CmsWith(RuntimeError("boom")))
        assert "capacity_warning" not in payload
        assert payload["status"] == "ok"
