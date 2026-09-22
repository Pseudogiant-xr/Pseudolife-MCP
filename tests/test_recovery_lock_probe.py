"""PostgreSQL counterexamples for delayed/reconnected reinstatement recovery."""
from contextlib import contextmanager
from copy import deepcopy
import threading

import pytest

from pseudolife_memory.service import CorrectionReconciliationError
from tests.pg_fixtures import pg_conn, pg_url
from tests.test_entry_reinstatement import (
    EntryReinstatementReconciliationError,
    PostgresStorage,
    _request,
    _retired_service,
    _row,
    _service,
    _sha,
)
from tests.test_correction_atomicity import (
    NEW_TEXT,
    _call as correction_call,
    _seed as correction_seed,
    _service as correction_service,
)


def _entry_state(entry):
    state = deepcopy(entry.__dict__)
    embedding = state.pop("embedding")
    return state, embedding


def _assert_entry_state(entry, expected):
    state = deepcopy(entry.__dict__)
    embedding = state.pop("embedding")
    expected_state, expected_embedding = expected
    assert state == expected_state
    assert embedding.equal(expected_embedding)


def _lose_lock_session(storage, pg_conn, mode):
    if mode == "client_close":
        storage.conn.close()
        return
    backend_pid = storage.conn.info.backend_pid
    assert pg_conn.execute(
        "SELECT pg_terminate_backend(%s)", (backend_pid,)
    ).fetchone()[0]


@pytest.mark.parametrize("reconnect", [False, True])
def test_delayed_recovery_holds_lock_through_publication(
    tmp_path, monkeypatch, pg_conn, pg_url, reconnect,
):
    svc, storage, old = _retired_service(
        tmp_path / "primary", monkeypatch, pg_url)
    load = storage.load_entries
    reinstate = storage.reinstate_entry
    admission = iter([load()])

    def unavailable_after_admission():
        try:
            return next(admission)
        except StopIteration:
            raise RuntimeError("synthetic recovery read failure")

    def committed_response_lost(**kwargs):
        reinstate(**kwargs)
        raise RuntimeError("synthetic lost response")

    monkeypatch.setattr(storage, "load_entries", unavailable_after_admission)
    monkeypatch.setattr(storage, "reinstate_entry", committed_response_lost)
    peer_storage = None
    threads = []
    release = threading.Event()
    paused = threading.Event()
    done = threading.Event()
    errors = []
    try:
        with pytest.raises(EntryReinstatementReconciliationError):
            svc.reinstate(**_request(old.db_id))
        assert svc._entry_reinstatement_recovery is not None
        monkeypatch.setattr(storage, "load_entries", load)
        monkeypatch.setattr(storage, "reinstate_entry", reinstate)
        if reconnect:
            storage.conn.close()
        peer_storage = PostgresStorage(pg_url)
        peer = _service(
            tmp_path / "peer", monkeypatch, peer_storage,
            embedding_dim=1024)
        peer._cms = peer._hydrate_correction_rows(
            peer_storage.load_entries(), peer._cms)
        publish = svc._preserve_correction_entry_identity

        def paused_publish(*args):
            paused.set()
            assert release.wait(8), "probe publication release expired"
            return publish(*args)

        monkeypatch.setattr(
            svc, "_preserve_correction_entry_identity", paused_publish)

        def run(call, signal=None):
            try:
                call()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if signal is not None:
                    signal.set()

        def recover():
            with svc._lock:
                svc._recover_entry_reinstatement_locked()

        threads.append(threading.Thread(target=run, args=(recover,), daemon=True))
        threads[-1].start()
        assert paused.wait(5), "delayed recovery never reached publication"
        threads.append(threading.Thread(target=run, args=(
            lambda: peer.supersede(
                entry_id=old.db_id,
                new_text="newer synthetic correction"),
            done,
        ), daemon=True))
        threads[-1].start()
        peer_completed_before_publication = done.wait(2)
        release.set()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)
        assert not errors
        durable = _row(peer_storage, old.db_id)
        assert durable["superseded_at"] is not None
        stale_publication = (
            peer_completed_before_publication and old.superseded_at is None)
        assert not stale_publication, (
            "delayed recovery published stale live state after peer "
            "correction committed")
        assert not peer_completed_before_publication, (
            "peer bypassed recovery publication lock")
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        storage.close()
        if peer_storage is not None:
            peer_storage.close()


def test_connection_loss_after_recovery_read_fails_closed_then_retries(
    tmp_path, monkeypatch, pg_conn, pg_url,
):
    svc, storage, old = _retired_service(
        tmp_path / "primary", monkeypatch, pg_url)
    load = storage.load_entries
    reinstate = storage.reinstate_entry
    admission = iter([load()])

    def unavailable_after_admission():
        try:
            return next(admission)
        except StopIteration:
            raise RuntimeError("synthetic recovery read failure")

    def committed_response_lost(**kwargs):
        reinstate(**kwargs)
        raise RuntimeError("synthetic lost response")

    monkeypatch.setattr(storage, "load_entries", unavailable_after_admission)
    monkeypatch.setattr(storage, "reinstate_entry", committed_response_lost)
    try:
        with pytest.raises(EntryReinstatementReconciliationError):
            svc.reinstate(**_request(old.db_id))
        pending = svc._entry_reinstatement_recovery
        assert pending is not None
        assert pending.entry_id == old.db_id
        monkeypatch.setattr(storage, "load_entries", load)
        monkeypatch.setattr(storage, "reinstate_entry", reinstate)
        hydrate = svc._hydrate_correction_rows

        def disconnect_after_hydration(*args):
            resident = hydrate(*args)
            storage.conn.close()
            return resident

        monkeypatch.setattr(
            svc, "_hydrate_correction_rows", disconnect_after_hydration)
        with svc._lock, pytest.raises(
            EntryReinstatementReconciliationError,
            match="lock connection was lost before reinstatement publication",
        ):
            svc._recover_entry_reinstatement_locked()
        assert svc._entry_reinstatement_recovery is pending
        assert old.superseded_at == 20.0

        monkeypatch.setattr(svc, "_hydrate_correction_rows", hydrate)
        svc.autosave_if_changed()
        assert svc._entry_reinstatement_recovery is None
        assert old.superseded_at is None
    finally:
        storage.close()


def test_delayed_correction_recovery_holds_lock_through_publication(
    tmp_path, monkeypatch, pg_conn, pg_url,
):
    storage = PostgresStorage(pg_url)
    svc = correction_service(
        tmp_path / "primary", monkeypatch, storage, embedding_dim=1024)
    old = correction_seed(svc)
    transaction = storage.transaction
    load = storage.load_entries

    @contextmanager
    def committed_but_lost():
        with transaction():
            yield
        raise RuntimeError("synthetic lost response")

    monkeypatch.setattr(storage, "transaction", committed_but_lost)
    monkeypatch.setattr(
        storage, "load_entries",
        lambda: (_ for _ in ()).throw(
            RuntimeError("synthetic recovery read failure")),
    )
    peer_storage = None
    release = threading.Event()
    paused = threading.Event()
    done = threading.Event()
    errors = []
    threads = []
    try:
        with pytest.raises(Exception, match="synthetic recovery read failure"):
            correction_call(svc, "supersede", ids=[old.db_id])
        assert svc._correction_recovery is not None
        monkeypatch.setattr(storage, "transaction", transaction)
        monkeypatch.setattr(storage, "load_entries", load)
        peer_storage = PostgresStorage(pg_url)
        peer = _service(
            tmp_path / "peer", monkeypatch, peer_storage,
            embedding_dim=1024)
        peer._cms = peer._hydrate_correction_rows(
            peer_storage.load_entries(), peer._cms)
        durable = _row(peer_storage, old.db_id)
        request = _request(
            old.db_id,
            expected_text_sha256=_sha(durable["text"]),
            expected_source_sha256=_sha(durable["source"]),
            expected_superseded_at=durable["superseded_at"],
            expected_superseded_by_text_sha256=
                _sha(durable["superseded_by_text"]),
        )
        publish = svc._preserve_correction_entry_identity

        def paused_publish(*args):
            paused.set()
            assert release.wait(8), "probe publication release expired"
            return publish(*args)

        monkeypatch.setattr(
            svc, "_preserve_correction_entry_identity", paused_publish)

        def run(call, signal=None):
            try:
                call()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if signal is not None:
                    signal.set()

        def recover():
            with svc._lock:
                svc._recover_correction_locked()

        threads.append(threading.Thread(target=run, args=(recover,), daemon=True))
        threads[-1].start()
        assert paused.wait(5), "correction recovery never reached publication"
        threads.append(threading.Thread(
            target=run, args=(lambda: peer.reinstate(**request), done),
            daemon=True))
        threads[-1].start()
        peer_completed_before_publication = done.wait(2)
        release.set()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)
        assert not errors
        assert not peer_completed_before_publication, (
            "peer bypassed correction recovery publication lock")
        assert _row(peer_storage, old.db_id)["superseded_at"] is None
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        storage.close()
        if peer_storage is not None:
            peer_storage.close()


@pytest.mark.parametrize("loss_mode", ["client_close", "terminate_backend"])
def test_reinstatement_publication_loss_restores_resident_and_retries(
    tmp_path, monkeypatch, pg_conn, pg_url, loss_mode,
):
    svc, storage, old = _retired_service(
        tmp_path / "primary", monkeypatch, pg_url)
    load = storage.load_entries
    reinstate = storage.reinstate_entry
    admission = iter([load()])

    def unavailable_after_admission():
        try:
            return next(admission)
        except StopIteration:
            raise RuntimeError("synthetic recovery read failure")

    def committed_response_lost(**kwargs):
        reinstate(**kwargs)
        raise RuntimeError("synthetic lost response")

    monkeypatch.setattr(storage, "load_entries", unavailable_after_admission)
    monkeypatch.setattr(storage, "reinstate_entry", committed_response_lost)
    peer_storage = None
    try:
        with pytest.raises(EntryReinstatementReconciliationError):
            svc.reinstate(**_request(old.db_id))
        pending = svc._entry_reinstatement_recovery
        assert pending is not None
        assert pending.entry_id == old.db_id
        prior_cms = svc._cms
        prior_entry = _entry_state(old)

        monkeypatch.setattr(storage, "load_entries", load)
        monkeypatch.setattr(storage, "reinstate_entry", reinstate)
        peer_storage = PostgresStorage(pg_url)
        peer = _service(
            tmp_path / "peer", monkeypatch, peer_storage,
            embedding_dim=1024)
        peer._cms = peer._hydrate_correction_rows(
            peer_storage.load_entries(), peer._cms)
        publish = svc._preserve_correction_entry_identity

        def lose_during_publication(*args):
            publish(*args)
            assert old.superseded_at is None
            _lose_lock_session(storage, pg_conn, loss_mode)
            result = peer.supersede(
                entry_id=old.db_id,
                new_text="newer synthetic correction",
            )
            assert result["superseded_count"] == 1

        monkeypatch.setattr(
            svc, "_preserve_correction_entry_identity",
            lose_during_publication)

        with svc._lock, pytest.raises(
            EntryReinstatementReconciliationError,
        ):
            svc._recover_entry_reinstatement_locked()

        retained = svc._entry_reinstatement_recovery
        assert retained is pending
        assert retained.entry_id == old.db_id
        assert svc._cms is prior_cms
        _assert_entry_state(old, prior_entry)
        durable = _row(peer_storage, old.db_id)
        assert durable["superseded_at"] is not None
        assert durable["superseded_by_text"] == "newer synthetic correction"

        monkeypatch.setattr(
            svc, "_preserve_correction_entry_identity", publish)
        svc.autosave_if_changed()
        assert svc._entry_reinstatement_recovery is None
        assert old.superseded_at == durable["superseded_at"]
        assert old.superseded_by_text == "newer synthetic correction"
        assert any(
            entry is old
            for band in svc._cms.bands for entry in band.entries
        )
    finally:
        storage.close()
        if peer_storage is not None:
            peer_storage.close()


@pytest.mark.parametrize("loss_mode", ["client_close", "terminate_backend"])
def test_correction_publication_loss_restores_resident_and_retries(
    tmp_path, monkeypatch, pg_conn, pg_url, loss_mode,
):
    storage = PostgresStorage(pg_url)
    svc = correction_service(
        tmp_path / "primary", monkeypatch, storage, embedding_dim=1024)
    old = correction_seed(svc)
    transaction = storage.transaction
    load = storage.load_entries

    @contextmanager
    def committed_but_lost():
        with transaction():
            yield
        raise RuntimeError("synthetic lost response")

    monkeypatch.setattr(storage, "transaction", committed_but_lost)
    monkeypatch.setattr(
        storage, "load_entries",
        lambda: (_ for _ in ()).throw(
            RuntimeError("synthetic recovery read failure")),
    )
    peer_storage = None
    try:
        with pytest.raises(Exception, match="synthetic recovery read failure"):
            correction_call(svc, "supersede", ids=[old.db_id])
        pending = svc._correction_recovery
        assert pending is not None
        assert pending.source_ids == (old.db_id,)
        prior_cms = svc._cms
        prior_entry = _entry_state(old)

        monkeypatch.setattr(storage, "transaction", transaction)
        monkeypatch.setattr(storage, "load_entries", load)
        peer_storage = PostgresStorage(pg_url)
        peer = _service(
            tmp_path / "peer", monkeypatch, peer_storage,
            embedding_dim=1024)
        peer._cms = peer._hydrate_correction_rows(
            peer_storage.load_entries(), peer._cms)
        durable = _row(peer_storage, old.db_id)
        request = _request(
            old.db_id,
            expected_text_sha256=_sha(durable["text"]),
            expected_source_sha256=_sha(durable["source"]),
            expected_superseded_at=durable["superseded_at"],
            expected_superseded_by_text_sha256=
                _sha(durable["superseded_by_text"]),
        )
        publish = svc._preserve_correction_entry_identity

        def lose_during_publication(*args):
            publish(*args)
            assert old.superseded_at == durable["superseded_at"]
            _lose_lock_session(storage, pg_conn, loss_mode)
            result = peer.reinstate(**request)
            assert result["current_state"] == "live"

        monkeypatch.setattr(
            svc, "_preserve_correction_entry_identity",
            lose_during_publication)

        with svc._lock, pytest.raises(CorrectionReconciliationError):
            svc._recover_correction_locked()

        retained = svc._correction_recovery
        assert retained is not None
        assert retained.source_ids == (old.db_id,)
        assert getattr(retained, "durable_outcome", None) == "committed"
        assert svc._cms is prior_cms
        _assert_entry_state(old, prior_entry)
        assert _row(peer_storage, old.db_id)["superseded_at"] is None

        monkeypatch.setattr(
            svc, "_preserve_correction_entry_identity", publish)
        # The lightweight fixture disables initialization; restore the real
        # guard so this read exercises ordinary pending-recovery retry.
        monkeypatch.delattr(svc, "_ensure_init")
        recent = svc.recent(n=10)
        assert svc._correction_recovery is None
        assert old.superseded_at is None
        assert any(entry["id"] == old.db_id for entry in recent["entries"])
        assert any(
            entry is old
            for band in svc._cms.bands for entry in band.entries
        )
    finally:
        storage.close()
        if peer_storage is not None:
            peer_storage.close()


@pytest.mark.parametrize("loss_mode", ["client_close", "terminate_backend"])
def test_successful_correction_publication_loss_keeps_recovery_pending(
    tmp_path, monkeypatch, pg_conn, pg_url, loss_mode,
):
    storage = PostgresStorage(pg_url)
    svc = correction_service(
        tmp_path / "primary", monkeypatch, storage, embedding_dim=1024)
    old = correction_seed(svc)
    prior_cms = svc._cms
    prior_entry = _entry_state(old)
    peer_storage = PostgresStorage(pg_url)
    peer = _service(tmp_path / "peer", monkeypatch, peer_storage,
                    embedding_dim=1024)
    publish = svc._preserve_correction_entry_identity

    def lose_during_publication(*args):
        publish(*args)
        durable = _row(peer_storage, old.db_id)
        _lose_lock_session(storage, pg_conn, loss_mode)
        result = peer.reinstate(**_request(
            old.db_id,
            expected_text_sha256=_sha(durable["text"]),
            expected_source_sha256=_sha(durable["source"]),
            expected_superseded_at=durable["superseded_at"],
            expected_superseded_by_text_sha256=
                _sha(durable["superseded_by_text"]),
        ))
        assert result["current_state"] == "live"

    monkeypatch.setattr(svc, "_preserve_correction_entry_identity",
                        lose_during_publication)
    try:
        with pytest.raises(CorrectionReconciliationError):
            correction_call(svc, "supersede", ids=[old.db_id])
        assert svc._cms is prior_cms
        _assert_entry_state(old, prior_entry)
        assert svc._correction_recovery is not None
        assert svc._correction_recovery.durable_outcome == "committed"
        monkeypatch.setattr(svc, "_preserve_correction_entry_identity", publish)
        monkeypatch.delattr(svc, "_ensure_init")
        recent = svc.recent(n=10)
        assert svc._correction_recovery is None
        assert old.superseded_at is None
        assert any(row["id"] == old.db_id for row in recent["entries"])
    finally:
        storage.close()
        peer_storage.close()


def _request_for_durable_row(row):
    return _request(
        row["id"],
        expected_text_sha256=_sha(row["text"]),
        expected_source_sha256=_sha(row["source"]),
        expected_superseded_at=row["superseded_at"],
        expected_superseded_by_text_sha256=
            _sha(row["superseded_by_text"]),
    )


def _reinstatement_decision_count(storage, entry_id):
    return storage.conn.execute(
        "SELECT count(*) FROM entry_reinstatement_decisions "
        "WHERE entry_id = %s",
        (entry_id,),
    ).fetchone()[0]


@pytest.mark.parametrize("loss_mode", ["client_close", "terminate_backend"])
@pytest.mark.parametrize("admission", ["first", "replay"])
def test_admission_publication_loss_restores_resident_and_retries(
    tmp_path, monkeypatch, pg_conn, pg_url, admission, loss_mode,
):
    svc, storage, old = _retired_service(
        tmp_path / "primary", monkeypatch, pg_url)
    retired = _row(storage, old.db_id)
    request = _request_for_durable_row(retired)
    peer_storage = PostgresStorage(pg_url)
    peer = _service(
        tmp_path / "peer", monkeypatch, peer_storage,
        embedding_dim=1024)
    publish = svc._preserve_correction_entry_identity
    try:
        if admission == "replay":
            result = svc.reinstate(**request)
            assert result["current_state"] == "live"
            assert result["changed_by_this_call"] is True
            assert old.superseded_at is None
            assert _row(storage, old.db_id)["superseded_at"] is None

        peer._cms = peer._hydrate_correction_rows(
            peer_storage.load_entries(), peer._cms)
        prior_cms = svc._cms
        prior_entry = _entry_state(old)
        _, request_sha256, _ = peer_storage._reinstatement_request(**request)

        def lose_during_admission_publication(*args):
            publish(*args)
            _lose_lock_session(storage, pg_conn, loss_mode)
            if admission == "first":
                result = peer.reinstate(**request)
                assert result["current_state"] == "live"
                assert result["changed_by_this_call"] is True
            else:
                result = peer.supersede(
                    entry_id=old.db_id,
                    new_text="newer synthetic correction",
                )
                assert result["superseded_count"] == 1

        monkeypatch.setattr(
            svc, "_preserve_correction_entry_identity",
            lose_during_admission_publication,
        )

        with pytest.raises(EntryReinstatementReconciliationError):
            svc.reinstate(**request)

        pending = svc._entry_reinstatement_recovery
        assert pending is not None
        assert pending.original is prior_cms
        assert pending.entry_id == old.db_id
        assert pending.operation_id == request["operation_id"]
        assert pending.request_sha256 == request_sha256
        assert svc._cms is prior_cms
        _assert_entry_state(old, prior_entry)
        assert any(
            entry is old
            for band in svc._cms.bands for entry in band.entries
        )

        durable = _row(peer_storage, old.db_id)
        if admission == "first":
            assert durable["superseded_at"] is None
            assert durable["superseded_by_text"] is None
        else:
            assert durable["superseded_at"] is not None
            assert durable["superseded_by_text"] == (
                "newer synthetic correction")
        assert _reinstatement_decision_count(peer_storage, old.db_id) == 1
        decision = peer_storage.entry_reinstatement_decision(
            request["operation_id"])
        assert decision is not None
        assert decision["request_sha256"] == request_sha256

        monkeypatch.setattr(
            svc, "_preserve_correction_entry_identity", publish)
        monkeypatch.delattr(svc, "_ensure_init")
        svc.recent(n=10)

        assert svc._entry_reinstatement_recovery is None
        assert old.superseded_at == durable["superseded_at"]
        assert old.superseded_by_text == durable["superseded_by_text"]
        assert any(
            entry is old
            for band in svc._cms.bands for entry in band.entries
        )
        assert _reinstatement_decision_count(peer_storage, old.db_id) == 1
    finally:
        storage.close()
        peer_storage.close()
