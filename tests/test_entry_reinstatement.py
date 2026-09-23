"""Audited, failure-atomic reinstatement of one retired continuum entry."""

from __future__ import annotations

import hashlib
import threading
import uuid

import numpy as np
import pytest

from pseudolife_memory.service import EntryReinstatementReconciliationError
from pseudolife_memory.storage.postgres import PostgresStorage
from tests.test_correction_identity import _seed, _service
from tests.test_trace_invalidations_storage import (
    _PauseAfterExecute, _wait_for_row_lock,
)

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _entry(text: str = "synthetic retired source") -> dict:
    return {
        "band": "flat", "text": text,
        "embedding": np.zeros(1024, dtype=np.float32),
        "surprise": 0.5, "ts": 1000.0, "access_count": 0,
        "source": "test", "superseded_at": 20.0,
        "superseded_by_text": "synthetic replacement",
        "last_logical_turn": None, "episode_id": None,
        "episode_title": None, "tags": [], "slots": [],
    }


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    value = PostgresStorage(pg_url)
    yield value
    value.close()


def _request(entry_id: int, operation_id: str | None = None, **over) -> dict:
    value = {
        "entry_id": entry_id,
        "operation_id": operation_id or str(uuid.uuid4()),
        "expected_text_sha256": _sha("synthetic retired source"),
        "expected_source_sha256": _sha("test"),
        "expected_superseded_at": 20.0,
        "expected_superseded_by_text_sha256": _sha("synthetic replacement"),
        "evidence_packet_sha256": _sha("synthetic evidence packet"),
        "reviewer_ids": ["reviewer-a", "reviewer-b"],
        "reason": "restore a reviewed synthetic row",
        "decided_by": "named-test-principal",
    }
    value.update(over)
    return value


def _row(storage, entry_id: int) -> dict:
    return next(row for row in storage.load_entries() if row["id"] == entry_id)


@pytest.mark.parametrize("lose_connection", [False, True])
def test_entry_mutation_lock_requires_acknowledged_release(storage, lose_connection):
    from psycopg import OperationalError

    connection = storage.conn
    with pytest.raises(OperationalError, match="entry mutation lock"):
        with storage.entry_mutation_lock([123]):
            if lose_connection:
                connection.close()
            else:
                assert connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 41))",
                    ("entry-mutation:123",),
                ).fetchone()[0] is True
    assert storage._entry_mutation_connection is None
    assert connection.closed


def test_reinstate_entry_commits_audit_and_clear_together(storage):
    entry_id = storage.insert_entry(_entry())
    result = storage.reinstate_entry(**_request(entry_id))

    assert result["decision"] == "committed"
    assert result["changed_by_this_call"] is True
    assert _row(storage, entry_id)["superseded_at"] is None
    decision = storage.entry_reinstatement_decision(result["operation_id"])
    assert decision["entry_id"] == entry_id
    assert decision["prior_superseded_by_text"] == "synthetic replacement"
    assert decision["decided_by"] == "named-test-principal"
    for table in ("facts", "memory_traces", "memory_trace_invalidations"):
        assert storage.conn.execute(
            f"SELECT count(*) FROM {table}"  # table names are fixed test literals
        ).fetchone()[0] == 0


def test_reinstate_entry_refuses_any_trace_invalidation(storage):
    entry_id = storage.insert_entry(_entry())
    storage.conn.execute(
        "INSERT INTO memory_trace_invalidations "
        "(entity_norm, attribute_norm, source_entry_id, invalidated_at, cause) "
        "VALUES ('synthetic', 'slot', %s, 20.0, 'source_superseded')",
        (entry_id,),
    )

    with pytest.raises(ValueError, match="trace_invalidations_present"):
        storage.reinstate_entry(**_request(entry_id))
    assert _row(storage, entry_id)["superseded_at"] == 20.0
    assert storage.conn.execute(
        "SELECT count(*) FROM entry_reinstatement_decisions"
    ).fetchone()[0] == 0


def test_matching_replay_precedes_target_state_and_never_clears_new_history(storage):
    entry_id = storage.insert_entry(_entry())
    operation_id = str(uuid.uuid4())
    request = _request(entry_id, operation_id)
    storage.reinstate_entry(**request)
    storage.supersede_entries(
        [entry_id], superseded_at=30.0,
        superseded_by_text="newer synthetic correction",
    )

    replay = storage.reinstate_entry(**request)
    assert replay["decision"] == "committed"
    assert replay["changed_by_this_call"] is False
    assert replay["current_state"] == "retired_again"
    assert _row(storage, entry_id)["superseded_at"] == 30.0

    storage.delete_entry_ids([entry_id])
    replay = storage.reinstate_entry(**request)
    assert replay["current_state"] == "missing"


def test_operation_id_conflict_is_a_noop(storage):
    entry_id = storage.insert_entry(_entry())
    operation_id = str(uuid.uuid4())
    storage.reinstate_entry(**_request(entry_id, operation_id))

    with pytest.raises(ValueError, match="operation_conflict"):
        storage.reinstate_entry(**_request(
            entry_id, operation_id, reason="a different immutable request"))


def test_preimage_mismatch_is_a_noop(storage):
    entry_id = storage.insert_entry(_entry())
    with pytest.raises(ValueError, match="preimage_mismatch"):
        storage.reinstate_entry(**_request(
            entry_id, expected_text_sha256=_sha("wrong")))
    assert _row(storage, entry_id)["superseded_at"] == 20.0


def test_audit_insert_rolls_back_when_entry_clear_fails(storage):
    entry_id = storage.insert_entry(_entry())
    storage.conn.execute(
        "CREATE FUNCTION fail_synthetic_reinstate() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.superseded_at IS NULL THEN RAISE EXCEPTION 'synthetic clear failure'; "
        "END IF; RETURN NEW; END $$"
    )
    storage.conn.execute(
        "CREATE TRIGGER fail_synthetic_reinstate BEFORE UPDATE ON entries "
        "FOR EACH ROW EXECUTE FUNCTION fail_synthetic_reinstate()"
    )
    try:
        with pytest.raises(Exception, match="synthetic clear failure"):
            storage.reinstate_entry(**_request(entry_id))
        assert _row(storage, entry_id)["superseded_at"] == 20.0
        assert storage.conn.execute(
            "SELECT count(*) FROM entry_reinstatement_decisions"
        ).fetchone()[0] == 0
    finally:
        storage.conn.execute(
            "DROP TRIGGER IF EXISTS fail_synthetic_reinstate ON entries")
        storage.conn.execute("DROP FUNCTION IF EXISTS fail_synthetic_reinstate()")


@pytest.mark.parametrize("first", ["trace", "reinstate"])
def test_trace_and_reinstatement_serialize_on_source_row(
    pg_conn, pg_url, first,
):
    a = PostgresStorage(pg_url)
    # The racing peer is an out-of-contract second writer on purpose.
    b = PostgresStorage(pg_url, writer_lease=False)
    try:
        entry_id = a.insert_entry(_entry())
        locked = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        results, errors = [], []
        request = _request(entry_id)
        a._conn = _PauseAfterExecute(
            a._conn, "WHERE id = %s FOR UPDATE", locked, release)
        if first == "trace":
            first_call = lambda: a.add_trace(
                "synthetic", "slot", entry_id, 10.0)
            second_call = lambda: b.reinstate_entry(**request)
        else:
            first_call = lambda: a.reinstate_entry(**request)
            second_call = lambda: b.add_trace(
                "synthetic", "slot", entry_id, 10.0)

        def run(call, done=None):
            try:
                results.append(call())
            except BaseException as exc:
                errors.append(exc)
            finally:
                if done is not None:
                    done.set()

        one = threading.Thread(target=run, args=(first_call,))
        two = threading.Thread(target=run, args=(second_call, second_done))
        one.start()
        assert locked.wait(2), "first operation never acquired the source-row lock"
        two.start()
        _wait_for_row_lock(pg_conn, b.conn.info.backend_pid, a.conn.info.backend_pid)
        assert not second_done.is_set()
        release.set()
        one.join(3)
        two.join(3)
        assert second_done.is_set()
        if first == "trace":
            assert len(errors) == 1
            assert "trace_invalidations_present" in str(errors[0])
            assert _row(b, entry_id)["superseded_at"] == 20.0
        else:
            assert errors == []
            assert _row(b, entry_id)["superseded_at"] is None
            assert b.conn.execute(
                "SELECT count(*) FROM memory_trace_invalidations "
                "WHERE source_entry_id = %s", (entry_id,),
            ).fetchone()[0] == 0
    finally:
        a.close()
        b.close()


def _retired_service(tmp_path, monkeypatch, pg_url):
    storage = PostgresStorage(pg_url)
    svc = _service(tmp_path, monkeypatch, storage, embedding_dim=1024)
    old = _seed(svc, text="synthetic retired source", source="test")
    old.superseded_at = 20.0
    old.superseded_by_text = "synthetic replacement"
    storage.conn.execute(
        "UPDATE entries SET superseded_at = 20.0, "
        "superseded_by_text = 'synthetic replacement' WHERE id = %s",
        (old.db_id,),
    )
    return svc, storage, old


def test_service_publishes_after_commit_and_preserves_object_identity(
    tmp_path, monkeypatch, pg_conn, pg_url,
):
    svc, storage, old = _retired_service(tmp_path, monkeypatch, pg_url)
    try:
        svc._cms._true_drops = 7
        order_before = [
            entry.db_id for band in svc._cms.bands for entry in band.entries
        ]
        capacities_before = [band.max_entries for band in svc._cms.bands]
        result = svc.reinstate(**_request(old.db_id))
        resident = next(
            entry for band in svc._cms.bands for entry in band.entries
            if entry.db_id == old.db_id)
        assert result["current_state"] == "live"
        assert resident is old
        assert old.superseded_at is None
        assert [
            entry.db_id for band in svc._cms.bands for entry in band.entries
        ] == order_before
        assert [band.max_entries for band in svc._cms.bands] == capacities_before
        assert svc._cms._true_drops == 7
        assert svc._cms._slot_index_dirty is True
        assert result["cortex_changed"] is False
        assert result["trace_invalidations_changed"] == 0
    finally:
        storage.close()


def test_service_replay_rehydrates_later_retirement_and_deletion(
    tmp_path, monkeypatch, pg_conn, pg_url,
):
    svc, storage, old = _retired_service(tmp_path, monkeypatch, pg_url)
    # An out-of-process writer, deliberately outside the writer lease.
    external = PostgresStorage(pg_url, writer_lease=False)
    request = _request(old.db_id)
    try:
        external.reinstate_entry(**request)
        external.supersede_entries(
            [old.db_id], superseded_at=30.0,
            superseded_by_text="newer synthetic correction",
        )

        replay = svc.reinstate(**request)
        assert replay["current_state"] == "retired_again"
        assert old.superseded_at == 30.0
        assert old.superseded_by_text == "newer synthetic correction"

        external.delete_entry_ids([old.db_id])
        replay = svc.reinstate(**request)
        assert replay["current_state"] == "missing"
        assert all(
            entry.db_id != old.db_id
            for band in svc._cms.bands for entry in band.entries
        )
    finally:
        external.close()
        storage.close()


@pytest.mark.parametrize("resident_state", ["stale_live", "evicted"])
def test_service_rehydrates_authoritative_target_before_new_admission(
    tmp_path, monkeypatch, pg_conn, pg_url, resident_state,
):
    svc, storage, old = _retired_service(tmp_path, monkeypatch, pg_url)
    try:
        if resident_state == "stale_live":
            old.superseded_at = None
            old.superseded_by_text = None
        else:
            for band in svc._cms.bands:
                band.entries = [
                    entry for entry in band.entries if entry.db_id != old.db_id
                ]

        result = svc.reinstate(**_request(old.db_id))
        matches = [
            entry for band in svc._cms.bands for entry in band.entries
            if entry.db_id == old.db_id
        ]
        assert result["current_state"] == "live"
        assert len(matches) == 1
        assert matches[0].superseded_at is None
    finally:
        storage.close()


@pytest.mark.parametrize("outcome", ["reinstated", "refused"])
def test_service_reinstate_keeps_resident_only_state(
    tmp_path, monkeypatch, pg_conn, pg_url, outcome,
):
    """A reinstatement changes one row, so it must not rebuild the resident
    bank from rows. Access counts are synced only at save time, and an entry
    whose write-through insert failed stays resident with no row until the
    dream pull re-flushes it; a whole-bank rebuild would discard both."""
    svc, storage, old = _retired_service(tmp_path, monkeypatch, pg_url)
    try:
        neighbour = _seed(svc, text="synthetic neighbour", source="test")
        neighbour.access_count = 9
        unpersisted = _seed(svc, text="synthetic unpersisted", source="test")
        storage.delete_entry_ids([unpersisted.db_id])
        unpersisted.db_id = None

        request = _request(old.db_id)
        if outcome == "refused":
            request["expected_text_sha256"] = _sha("not the reviewed text")
            with pytest.raises(ValueError, match="preimage_mismatch"):
                svc.reinstate(**request)
            assert old.superseded_at == 20.0
        else:
            assert svc.reinstate(**request)["current_state"] == "live"
            assert old.superseded_at is None
        residents = [
            entry for band in svc._cms.bands for entry in band.entries]
        assert any(entry is unpersisted for entry in residents)
        assert any(entry is neighbour for entry in residents)
        assert neighbour.access_count == 9
        assert any(entry is old for entry in residents)
    finally:
        storage.close()


def test_retirement_without_replacement_text_is_named(
    tmp_path, monkeypatch, pg_conn, pg_url, storage,
):
    """Pre-v5 migrations left retired rows with no replacement text; say so
    instead of claiming the row is not retired."""
    row = _entry()
    row["superseded_by_text"] = None
    entry_id = storage.insert_entry(row)
    with pytest.raises(ValueError, match="retirement_text_missing"):
        storage.reinstate_entry(**_request(entry_id))

    storage.close()  # one writer per bank: hand it to the service below
    svc, service_storage, old = _retired_service(tmp_path, monkeypatch, pg_url)
    try:
        service_storage.conn.execute(
            "UPDATE entries SET superseded_by_text = NULL WHERE id = %s",
            (old.db_id,))
        old.superseded_by_text = None
        with pytest.raises(ValueError, match="retirement_text_missing"):
            svc.reinstate(**_request(old.db_id))
    finally:
        service_storage.close()


def test_entry_mutation_lock_refusal_keeps_the_session(storage, pg_conn):
    """A routine refusal inside the lock releases its keys on the same
    session; closing the shared connection would log a spurious
    'postgres connection lost' and force a reconnect on every refusal."""
    connection = storage.conn
    backend = connection.info.backend_pid
    with pytest.raises(ValueError, match="routine refusal"):
        with storage.entry_mutation_lock([123, 124]):
            raise ValueError("routine refusal")
    assert storage._entry_mutation_connection is None
    assert not connection.closed
    assert storage.conn is connection
    assert storage.conn.info.backend_pid == backend
    for key in ("entry-mutation:123", "entry-mutation:124"):
        assert pg_conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 41))", (key,),
        ).fetchone()[0] is True
        pg_conn.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 41))", (key,))


def test_entry_mutation_lock_refusal_after_lost_key_closes_the_session(
    storage,
):
    """A refusal whose key was already gone leaves the release
    unacknowledged: the session closes, and the refusal still surfaces."""
    connection = storage.conn
    with pytest.raises(ValueError, match="routine refusal"):
        with storage.entry_mutation_lock([123]):
            assert connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 41))",
                ("entry-mutation:123",),
            ).fetchone()[0] is True
            raise ValueError("routine refusal")
    assert connection.closed
    assert storage._entry_mutation_connection is None


def test_entry_mutation_lock_refusal_on_a_dead_session_surfaces_it(storage):
    connection = storage.conn
    with pytest.raises(ValueError, match="routine refusal"):
        with storage.entry_mutation_lock([123]):
            connection.close()
            raise ValueError("routine refusal")
    assert storage._entry_mutation_connection is None
    assert storage.conn is not connection


def test_entry_mutation_lock_interrupt_abandons_the_session(storage):
    connection = storage.conn
    with pytest.raises(KeyboardInterrupt):
        with storage.entry_mutation_lock([123]):
            raise KeyboardInterrupt
    assert connection.closed
    assert storage._entry_mutation_connection is None


@pytest.mark.parametrize("first", ["correction", "reinstate"])
def test_service_correction_and_reinstatement_serialize_through_publication(
    tmp_path, monkeypatch, pg_conn, pg_url, first,
):
    primary_storage = PostgresStorage(pg_url)
    primary = _service(
        tmp_path / "primary", monkeypatch, primary_storage,
        embedding_dim=1024,
    )
    old = _seed(
        primary, text="synthetic retired source", source="test")
    if first == "reinstate":
        old.superseded_at = 20.0
        old.superseded_by_text = "synthetic replacement"
        primary_storage.conn.execute(
            "UPDATE entries SET superseded_at = 20.0, "
            "superseded_by_text = 'synthetic replacement' WHERE id = %s",
            (old.db_id,),
        )

    # The racing peer service is an out-of-contract second writer on purpose.
    peer_storage = PostgresStorage(pg_url, writer_lease=False)
    peer = _service(
        tmp_path / "peer", monkeypatch, peer_storage, embedding_dim=1024)
    peer._cms = peer._hydrate_correction_rows(
        peer_storage.load_entries(), peer._cms)
    committed = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    results, errors = [], []

    try:
        if first == "correction":
            original_publish = primary._preserve_correction_entry_identity

            def pause_publish(*args):
                committed.set()
                assert release.wait(3)
                return original_publish(*args)

            monkeypatch.setattr(
                primary, "_preserve_correction_entry_identity", pause_publish)
            first_call = lambda: primary.supersede(
                entry_id=old.db_id, new_text="newer synthetic correction")
        else:
            # The queued correction was admitted against the state the first
            # operation has durably committed but not yet published.
            peer_old = next(
                entry for band in peer._cms.bands for entry in band.entries
                if entry.db_id == old.db_id)
            peer_old.superseded_at = None
            peer_old.superseded_by_text = None
            original_recover = primary._recover_entry_reinstatement_locked

            def pause_recover():
                committed.set()
                assert release.wait(3)
                return original_recover()

            monkeypatch.setattr(
                primary, "_recover_entry_reinstatement_locked", pause_recover)
            first_call = lambda: primary.reinstate(**_request(old.db_id))

        def run(call, done=None):
            try:
                results.append(call())
            except BaseException as exc:
                errors.append(exc)
            finally:
                if done is not None:
                    done.set()

        one = threading.Thread(target=run, args=(first_call,))
        one.start()
        assert committed.wait(3), "first operation never reached publication"

        if first == "correction":
            durable = _row(primary_storage, old.db_id)
            second_call = lambda: peer.reinstate(**_request(
                old.db_id,
                expected_superseded_at=durable["superseded_at"],
                expected_superseded_by_text_sha256=_sha(
                    "newer synthetic correction"),
            ))
        else:
            second_call = lambda: peer.supersede(
                entry_id=old.db_id, new_text="newer synthetic correction")
        two = threading.Thread(target=run, args=(second_call, second_done))
        two.start()
        _wait_for_row_lock(
            pg_conn, peer_storage.conn.info.backend_pid,
            primary_storage.conn.info.backend_pid,
        )
        assert not second_done.is_set()
        release.set()
        one.join(4)
        two.join(4)
        assert second_done.is_set()
        assert errors == []
        durable = _row(peer_storage, old.db_id)
        if first == "correction":
            assert durable["superseded_at"] is None
        else:
            assert durable["superseded_at"] is not None
            assert durable["superseded_by_text"] == "newer synthetic correction"
    finally:
        release.set()
        primary_storage.close()
        peer_storage.close()


def test_service_refuses_file_mode_before_target_admission(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="requires_postgres"):
        svc.reinstate(**_request(41))


@pytest.mark.parametrize("committed", [False, True])
def test_service_reconciles_lost_storage_response(
    tmp_path, monkeypatch, pg_conn, pg_url, committed,
):
    svc, storage, old = _retired_service(tmp_path, monkeypatch, pg_url)
    original = storage.reinstate_entry

    def uncertain(**kwargs):
        if committed:
            original(**kwargs)
        raise RuntimeError("synthetic lost response")

    monkeypatch.setattr(storage, "reinstate_entry", uncertain)
    try:
        if committed:
            result = svc.reinstate(**_request(old.db_id))
            assert result["decision"] == "committed"
            assert old.superseded_at is None
        else:
            with pytest.raises(RuntimeError, match="synthetic lost response"):
                svc.reinstate(**_request(old.db_id))
            assert old.superseded_at == 20.0
        assert svc._entry_reinstatement_recovery is None
    finally:
        storage.close()


def test_unreadable_reconciliation_gates_persistence(
    tmp_path, monkeypatch, pg_conn, pg_url,
):
    svc, storage, old = _retired_service(tmp_path, monkeypatch, pg_url)
    original_reinstate = storage.reinstate_entry
    original_load = storage.load_entry_row

    def committed_but_lost(**kwargs):
        original_reinstate(**kwargs)
        raise RuntimeError("synthetic lost response")

    reads = iter([original_load(old.db_id)])

    def readable_admission_then_unavailable(entry_id):
        try:
            return next(reads)
        except StopIteration:
            raise RuntimeError("synthetic read unavailable")

    monkeypatch.setattr(storage, "reinstate_entry", committed_but_lost)
    monkeypatch.setattr(
        storage, "load_entry_row",
        readable_admission_then_unavailable,
    )
    try:
        with pytest.raises(
            EntryReinstatementReconciliationError,
            match="synthetic read unavailable",
        ):
            svc.reinstate(**_request(old.db_id))
        assert svc._entry_reinstatement_recovery is not None
        monkeypatch.setattr(
            svc, "_ensure_init", svc._recover_entry_reinstatement_locked)
        with pytest.raises(EntryReinstatementReconciliationError):
            svc.search("synthetic read while reconciliation is unavailable")
        with pytest.raises(EntryReinstatementReconciliationError):
            svc.autosave_if_changed()
        monkeypatch.setattr(storage, "load_entry_row", original_load)
        monkeypatch.setattr(storage, "reinstate_entry", original_reinstate)
        svc.autosave_if_changed()
        assert svc._entry_reinstatement_recovery is None
        assert old.superseded_at is None
    finally:
        storage.close()


def test_mcp_requires_named_principal_and_derives_actor(monkeypatch):
    from pseudolife_memory import mcp_server, writer_context
    from pseudolife_memory.principals import DEFAULT_PRINCIPAL

    calls = []
    monkeypatch.setattr(
        mcp_server, "service",
        type("Recorder", (), {
            "reinstate": lambda self, **kwargs: calls.append(kwargs)
            or {"decision": "committed"},
        })(),
    )
    request = _request(41)
    request.pop("decided_by")
    monkeypatch.setattr(writer_context, "current_principal", lambda: DEFAULT_PRINCIPAL)
    with pytest.raises(ValueError, match="named_principal_required"):
        mcp_server.memory_reinstate(**request)
    assert calls == []

    monkeypatch.setattr(
        writer_context, "current_principal", lambda: "credential-principal")
    assert mcp_server.memory_reinstate(**request)["decision"] == "committed"
    assert calls[0]["decided_by"] == "credential-principal"


def test_schema_40_migrates_to_41_idempotently_and_audit_has_no_fk(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    pg_conn.execute("DROP TABLE entry_reinstatement_decisions")
    pg_conn.execute(
        "UPDATE meta SET value = '40'::jsonb WHERE key = 'schema_version'")
    ensure_schema(pg_conn)
    ensure_schema(pg_conn)

    assert pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0] == 41
    assert pg_conn.execute(
        "SELECT to_regclass('public.entry_reinstatement_decisions')"
    ).fetchone()[0] == "entry_reinstatement_decisions"
    assert pg_conn.execute(
        "SELECT count(*) FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "WHERE t.relname = 'entry_reinstatement_decisions' AND c.contype = 'f'"
    ).fetchone()[0] == 0
