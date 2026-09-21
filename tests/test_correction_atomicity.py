"""Failure-atomicity contracts for explicit entry corrections."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pytest

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.service import CorrectionReconciliationError
from pseudolife_memory.storage.postgres import PostgresStorage
from pseudolife_memory.storage.sync import hydrate_cms
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_correction_identity import (
    NEW_TEXT,
    Storage,
    _call,
    _entries,
    _seed,
    _service,
)


class TransactionalStorage(Storage):
    """Small rollback-capable storage double matching the product contract."""

    @contextmanager
    def transaction(self):
        before = deepcopy((self.rows, self.updates, self.supersessions))
        try:
            yield
        except Exception:
            self.rows, self.updates, self.supersessions = before
            raise

    def delete_entry_ids(self, ids):
        removed = 0
        for entry_id in ids:
            if self.rows.pop(entry_id, None) is not None:
                removed += 1
        return removed


class FaultStorage(TransactionalStorage):
    fail = None

    def insert_entry(self, row):
        if self.fail == "insert":
            raise RuntimeError("injected insert write-through failure")
        return super().insert_entry(row)

    def update_entry(self, entry_id, **fields):
        if self.fail == "update":
            raise RuntimeError("injected update write-through failure")
        return super().update_entry(entry_id, **fields)

    def delete_entry_ids(self, ids):
        if self.fail == "delete":
            raise RuntimeError("injected delete write-through failure")
        return super().delete_entry_ids(ids)


def _snapshot(svc):
    cms = svc._cms
    rows = deepcopy(getattr(svc._storage, "rows", None))
    if rows is not None:
        for row in rows.values():
            embedding = row.get("embedding")
            if hasattr(embedding, "tolist"):
                row["embedding"] = embedding.tolist()
    return {
        "entries": [
            (
                entry.text,
                entry.dream_id,
                entry.db_id,
                entry.superseded_at,
                entry.superseded_by_text,
                entry.cue_flags,
            )
            for band in cms.bands
            for entry in band.entries
        ],
        "history": deepcopy(cms._surprise_history),
        "interaction_count": cms._interaction_count,
        "true_drops": cms._true_drops,
        "slot_dirty": cms._slot_index_dirty,
        "slot_index": deepcopy(cms._slot_token_index),
        "rows": rows,
    }


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
@pytest.mark.parametrize("backend", ["file", "storage_double"])
@pytest.mark.parametrize("fault", ["encode_error", "store_refusal", "store_error"])
def test_failed_replacement_keeps_complete_before_state(
    tmp_path, monkeypatch, operation, backend, fault,
):
    storage = TransactionalStorage() if backend == "storage_double" else None
    svc = _service(tmp_path, monkeypatch, storage)
    old = _seed(svc)
    before = _snapshot(svc)

    def fail_encode(*args, **kwargs):
        raise RuntimeError("injected replacement failure")

    original_store = ContinuumMemorySystem.store

    def faulty_store(self, text, *args, **kwargs):
        if text == NEW_TEXT:
            if fault == "store_error":
                raise RuntimeError("injected replacement failure")
            if fault == "store_refusal":
                return False, 0.0
        return original_store(self, text, *args, **kwargs)

    if fault == "encode_error":
        monkeypatch.setattr(svc._embedder, "encode_single", fail_encode)
    else:
        monkeypatch.setattr(ContinuumMemorySystem, "store", faulty_store)

    if fault in {"encode_error", "store_error"}:
        with pytest.raises(RuntimeError, match="injected replacement failure"):
            _call(
                svc,
                operation,
                **({"ids": [old.db_id]} if storage else {"texts": [old.text]}),
            )
    else:
        result = _call(
            svc,
            operation,
            **({"ids": [old.db_id]} if storage else {"texts": [old.text]}),
        )
        assert result["reason"] == "replacement_rejected"
        assert result["superseded_count"] == 0
        assert result["new_memory_stored"] is False

    assert len(_entries(svc)) == 1
    assert _snapshot(svc) == before


def test_staged_clone_isolates_mutable_bank_state(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, TransactionalStorage())
    old = _seed(svc)
    svc._cms._surprise_history[svc._cms.bands[0].name].append(0.25)
    svc._cms._rebuild_slot_index()
    staged = svc._cms.clone_for_staged_store()

    staged.bands[0].entries[0].text = "changed only in stage"
    staged._surprise_history[staged.bands[0].name].append(0.5)
    staged._shadow_rng.random()

    assert old.text == "Gateway deployment evidence"
    assert svc._cms._surprise_history[svc._cms.bands[0].name] == [0.25]
    assert staged.storage is svc._cms.storage
    assert staged.config is svc._cms.config
    assert staged.bands[0].on_evict.func.__self__ is staged


@pytest.mark.parametrize("fault", ["insert", "update", "delete"])
def test_strict_write_through_fault_rolls_back_every_staged_side_effect(
    tmp_path, monkeypatch, fault,
):
    storage = FaultStorage()
    svc = _service(tmp_path, monkeypatch, storage)
    old = _seed(svc)
    if fault == "update":
        svc._cms.bands[0].promotion_access_count = 0
        svc._cms.bands[0].promotion_surprise = -1.0
        svc._cms.bands[1].update_interval = 1
    elif fault == "delete":
        svc._cms.bands = [svc._cms.bands[0]]
        svc._cms.bands[0].max_entries = 1
        svc._cms.instant = svc._cms.short_term = svc._cms.long_term = svc._cms.bands[0]
    before = _snapshot(svc)
    storage.fail = fault

    with pytest.raises(RuntimeError, match=f"injected {fault} write-through failure"):
        _call(svc, "supersede", ids=[old.db_id])

    assert _snapshot(svc) == before
    assert svc._cms._strict_storage is False


@pytest.mark.parametrize("operation", ["supersede", "consolidate"])
@pytest.mark.parametrize("fault", ["encode_error", "store_refusal", "store_error"])
def test_pg_failure_keeps_before_state_after_restart(
    tmp_path, monkeypatch, pg_conn, pg_url, operation, fault,
):
    storage = PostgresStorage(pg_url)
    try:
        svc = _service(tmp_path, monkeypatch, storage, embedding_dim=1024)
        old = _seed(svc)
        storage.add_trace("gateway", "deployment", old.db_id, 1.0)

        def fail_encode(*args, **kwargs):
            raise RuntimeError("injected replacement failure")

        original_store = ContinuumMemorySystem.store

        def faulty_store(self, text, *args, **kwargs):
            if text == NEW_TEXT:
                if fault == "store_error":
                    raise RuntimeError("injected replacement failure")
                if fault == "store_refusal":
                    return False, 0.0
            return original_store(self, text, *args, **kwargs)

        if fault == "encode_error":
            monkeypatch.setattr(svc._embedder, "encode_single", fail_encode)
        else:
            monkeypatch.setattr(ContinuumMemorySystem, "store", faulty_store)

        if fault == "store_refusal":
            result = _call(svc, operation, ids=[old.db_id])
            assert result["reason"] == "replacement_rejected"
        else:
            with pytest.raises(RuntimeError, match="injected replacement failure"):
                _call(svc, operation, ids=[old.db_id])
        storage.close()
        fresh = PostgresStorage(pg_url)
        try:
            restarted = ContinuumMemorySystem(svc.config.memory)
            hydrate_cms(restarted, fresh)
            entries = [entry for band in restarted.bands for entry in band.entries]
            assert len(entries) == 1
            assert entries[0].db_id == old.db_id
            assert entries[0].superseded_at is None
            invalidations = fresh.conn.execute(
                "SELECT count(*) FROM memory_trace_invalidations "
                "WHERE source_entry_id = %s AND cause = 'source_superseded'",
                (old.db_id,),
            ).fetchone()[0]
            assert invalidations == 0
        finally:
            fresh.close()
    finally:
        storage.close()


@pytest.mark.parametrize("committed", [False, True])
def test_pg_lost_response_reconciles_authoritative_state(
    tmp_path, monkeypatch, pg_conn, pg_url, committed,
):
    storage = PostgresStorage(pg_url)
    try:
        svc = _service(tmp_path, monkeypatch, storage, embedding_dim=1024)
        old = _seed(svc)
        storage.add_trace("gateway", "deployment", old.db_id, 1.0)
        original_transaction = storage.transaction

        @contextmanager
        def uncertain_transaction():
            if committed:
                with original_transaction():
                    yield
                raise RuntimeError("injected lost commit response")
            try:
                with original_transaction():
                    yield
                    raise RuntimeError("injected rollback before commit")
            except RuntimeError as exc:
                raise RuntimeError("injected lost rollback response") from exc

        monkeypatch.setattr(storage, "transaction", uncertain_transaction)
        if committed:
            result = _call(svc, "supersede", ids=[old.db_id])
            assert result["superseded_count"] == 1
            assert any(entry.text == NEW_TEXT for entry in _entries(svc))
            assert old.superseded_by_text == NEW_TEXT
            assert storage.conn.execute(
                "SELECT count(*) FROM memory_trace_invalidations "
                "WHERE source_entry_id = %s AND cause = 'source_superseded'",
                (old.db_id,),
            ).fetchone()[0] == 1
        else:
            with pytest.raises(RuntimeError, match="lost rollback response"):
                _call(svc, "supersede", ids=[old.db_id])
            assert [entry.text for entry in _entries(svc)] == [old.text]
            assert old.superseded_at is None
            assert storage.conn.execute(
                "SELECT count(*) FROM memory_trace_invalidations "
                "WHERE source_entry_id = %s AND cause = 'source_superseded'",
                (old.db_id,),
            ).fetchone()[0] == 0
        assert svc._correction_recovery is None
    finally:
        storage.close()


def test_pg_unreadable_recovery_blocks_save_until_retry(
    tmp_path, monkeypatch, pg_conn, pg_url,
):
    storage = PostgresStorage(pg_url)
    try:
        svc = _service(tmp_path, monkeypatch, storage, embedding_dim=1024)
        old = _seed(svc)
        original_transaction = storage.transaction
        original_load = storage.load_entries

        @contextmanager
        def committed_but_lost():
            with original_transaction():
                yield
            raise RuntimeError("injected lost commit response")

        monkeypatch.setattr(storage, "transaction", committed_but_lost)
        monkeypatch.setattr(
            storage, "load_entries",
            lambda: (_ for _ in ()).throw(RuntimeError("recovery read unavailable")),
        )
        with pytest.raises(CorrectionReconciliationError, match="recovery read unavailable"):
            _call(svc, "supersede", ids=[old.db_id])
        assert svc._correction_recovery is not None
        with pytest.raises(CorrectionReconciliationError, match="recovery read unavailable"):
            svc.autosave_if_changed()
        monkeypatch.setattr(storage, "load_entries", original_load)
        monkeypatch.setattr(storage, "transaction", original_transaction)
        svc.autosave_if_changed()
        assert svc._correction_recovery is None
        assert old.superseded_by_text == NEW_TEXT
    finally:
        storage.close()


@pytest.mark.parametrize("installed_after", [False, True])
def test_file_uncertain_snapshot_reconciles_before_or_after(
    tmp_path, monkeypatch, installed_after,
):
    svc = _service(tmp_path, monkeypatch)
    old = _seed(svc)
    for index, band in enumerate(svc._cms.bands):
        band.surprise_ema = 0.42 + index
    svc._cms._slot_index_shadow_divergences = 7
    svc._cms._shadow_rng.seed(418)
    svc._cms._true_drops = 3
    svc._cms._last_entity_seen = "gateway"

    def transient_state(cms):
        return {
            "surprise_ema": [band.surprise_ema for band in cms.bands],
            "shadow_divergences": cms._slot_index_shadow_divergences,
            "shadow_rng": cms._shadow_rng.getstate(),
            "true_drops": cms._true_drops,
            "last_entity_seen": cms._last_entity_seen,
        }

    expected_before = None
    expected_after = None
    original_save = ContinuumMemorySystem.save
    calls = 0

    def uncertain_save(self, *args, **kwargs):
        nonlocal calls, expected_before, expected_after
        calls += 1
        if calls == 1:
            expected_before = transient_state(self)
        if calls == 2:
            expected_after = transient_state(self)
        if calls == 2 and not installed_after:
            raise RuntimeError("injected uncertain replace")
        result = original_save(self, *args, **kwargs)
        if calls == 2:
            raise RuntimeError("injected uncertain replace")
        return result

    monkeypatch.setattr(ContinuumMemorySystem, "save", uncertain_save)
    if installed_after:
        result = _call(svc, "supersede", texts=[old.text])
        assert result["superseded_count"] == 1
        assert old.superseded_by_text == NEW_TEXT
    else:
        with pytest.raises(RuntimeError, match="uncertain replace"):
            _call(svc, "supersede", texts=[old.text])
        assert old.superseded_at is None
        assert [entry.text for entry in _entries(svc)] == [old.text]
    assert svc._correction_recovery is None
    assert transient_state(svc._cms) == (
        expected_after if installed_after else expected_before
    )
    assert svc._last_saved_fingerprint == svc._entry_fingerprint()


def test_file_stale_snapshot_cannot_replace_complete_before_state(
    tmp_path, monkeypatch,
):
    svc = _service(tmp_path, monkeypatch)
    old = _seed(svc)
    svc._cms.save(svc.config.memory.save_dir)
    unrelated = _seed(
        svc,
        text="Unrelated unsaved evidence",
        source="unrelated",
        episode="unrelated-episode",
    )
    original_cms = svc._cms

    def fail_before_save(*args, **kwargs):
        raise RuntimeError("injected first snapshot failure")

    monkeypatch.setattr(ContinuumMemorySystem, "save", fail_before_save)
    with pytest.raises(CorrectionReconciliationError, match="uniquely before nor after"):
        _call(svc, "supersede", texts=[old.text])

    assert svc._cms is original_cms
    assert unrelated in _entries(svc)
    assert [entry.text for entry in _entries(svc)] == [
        old.text,
        unrelated.text,
    ]
    assert svc._correction_recovery is not None


def test_file_uncertain_first_snapshot_recovers_complete_before_state(
    tmp_path, monkeypatch,
):
    svc = _service(tmp_path, monkeypatch)
    old = _seed(svc)
    unrelated = _seed(
        svc,
        text="Unrelated unsaved evidence",
        source="unrelated",
        episode="unrelated-episode",
    )
    svc._cms._interaction_count = 11
    episode = svc._cms.episodes.start_session(
        "Correction barrier", session_key="correction-barrier")
    original_save = ContinuumMemorySystem.save
    calls = 0

    def uncertain_first_save(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_save(self, *args, **kwargs)
        if calls == 1:
            raise RuntimeError("injected lost first snapshot response")
        return result

    monkeypatch.setattr(ContinuumMemorySystem, "save", uncertain_first_save)
    with pytest.raises(RuntimeError, match="lost first snapshot response"):
        _call(svc, "supersede", texts=[old.text])

    assert svc._correction_recovery is None
    assert [entry.text for entry in _entries(svc)] == [
        old.text,
        unrelated.text,
    ]
    assert old.superseded_at is None
    assert svc._cms._interaction_count == 11
    assert svc._cms.episodes.current_id == episode.id
    assert svc._last_saved_fingerprint == svc._entry_fingerprint()
