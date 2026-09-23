"""Coordination dispatch runs on its own connection and never waits on the
service lock.

A heartbeat, identity check or receive that queues behind a dream
consolidation pass loses: the adapter's context check times out at 5s and
the lease expires while the service lock is held for tens of seconds
(2026-09-20 daemon log: ``_dispatch waited 8.6s``, ``autosave_if_changed
waited 47s``). Mailbox rows are protected by their own SQL row locks, so
the store can safely use a dedicated connection serialized by a small
coordination lock instead of the service lock.
"""
from __future__ import annotations

import threading
import time

import pytest

from pseudolife_memory.coordination import dispatch
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401

PRINCIPAL = "agent-user"
HOLD_SECONDS = 3.0
# Well under HOLD_SECONDS so a dispatch that queued behind the lock cannot pass.
BUDGET_SECONDS = 1.0


@pytest.fixture
def coordinating(pg_service):
    """A served daemon: memory calls initialized the service before any
    mailbox call (``pg_service`` ran ``_ensure_init``), which is what lets
    ``send`` skip the service lock too."""
    pg_service.config.coordination.enabled = True
    pg_service.config.coordination.allowed_principals = [PRINCIPAL]
    assert pg_service.coordination_tier_ready()
    # Opens the dedicated mailbox connection so the timed calls measure only
    # lock waiting.
    dispatch(pg_service, "context", {}, headers={}, principal=PRINCIPAL)
    return pg_service


def _hold_service_lock(service, released: threading.Event, held: threading.Event):
    with service._lock:
        held.set()
        released.wait(HOLD_SECONDS)


def _timed(fn):
    t0 = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - t0


def test_dispatch_never_waits_on_service_lock(coordinating):
    released, held = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_service_lock, args=(coordinating, released, held))
    holder.start()
    try:
        assert held.wait(2), "lock holder did not start"
        context, context_seconds = _timed(lambda: dispatch(
            coordinating, "context", {}, headers={}, principal=PRINCIPAL))
        agent, register_seconds = _timed(lambda: dispatch(
            coordinating, "register", {"label": "held"}, headers={}, principal=PRINCIPAL))
        creds = {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}
        _, attach_seconds = _timed(lambda: dispatch(
            coordinating, "attach", {"attachment_id": "held-attach"},
            headers=creds, principal=PRINCIPAL))
        _, receive_seconds = _timed(lambda: dispatch(
            coordinating, "receive", {}, headers=creds, principal=PRINCIPAL))
        sent, send_seconds = _timed(lambda: dispatch(
            coordinating, "send", {"to": agent["agent_id"], "text": "note to self",
                                   "request_id": "held-send"},
            headers=creds, principal=PRINCIPAL))
    finally:
        released.set()
        holder.join()
    assert context["principal"] == PRINCIPAL
    assert sent["state"] == "queued"
    for name, seconds in (("context", context_seconds), ("register", register_seconds),
                          ("attach", attach_seconds), ("receive", receive_seconds),
                          ("send", send_seconds)):
        assert seconds < BUDGET_SECONDS, f"{name} waited {seconds:.2f}s behind the service lock"


def test_dispatch_uses_a_dedicated_committed_connection(coordinating):
    mailbox = coordinating._coordination_storage
    assert mailbox.conn is not coordinating._storage.conn
    assert mailbox.conn.autocommit, "reads must never leave an idle transaction open"
    agent = dispatch(coordinating, "register", {"label": "durable"}, headers={},
                     principal=PRINCIPAL)
    # Committed on the mailbox connection, visible from the service connection.
    row = coordinating._storage.conn.execute(
        "SELECT label FROM coordination_agents WHERE agent_id=%s",
        (agent["agent_id"],)).fetchone()
    assert row == ("durable",)


def test_dedicated_connection_reconnects_after_loss(coordinating):
    mailbox = coordinating._coordination_storage
    lost = mailbox.conn
    lost.close()
    context = dispatch(coordinating, "context", {}, headers={}, principal=PRINCIPAL)
    assert context["principal"] == PRINCIPAL
    assert mailbox.conn is not lost and not mailbox.conn.closed


def test_initialized_service_is_never_relocked_by_mailbox_calls(coordinating, monkeypatch):
    """On a served daemon the first memory call initialized the service long
    before any mailbox call; coordination must read that without the lock."""
    monkeypatch.setattr(coordinating, "_ensure_init",
                        lambda: pytest.fail("mailbox call re-entered service init"))
    agent = dispatch(coordinating, "register", {}, headers={}, principal=PRINCIPAL)
    assert agent["principal"] == PRINCIPAL


def test_cold_service_pays_full_init_only_for_send(pg_service, tmp_path, monkeypatch):
    """A cold daemon: context, a wrong-bank refusal, register and attach need
    only the durable tier (no embedder load, no service lock afterwards);
    the first send pays the full initialization once, for the HLC reseed."""
    from pseudolife_memory.service import MemoryService

    pg_service._storage.close()  # the cold daemon is the bank's one writer
    cold = MemoryService(data_dir=tmp_path / "cold")
    cold.config.coordination.enabled = True
    cold.config.coordination.allowed_principals = [PRINCIPAL]
    calls = []
    real_init = cold._ensure_init
    monkeypatch.setattr(cold, "_ensure_init", lambda: calls.append("init") or real_init())
    try:
        bank_id = dispatch(cold, "context", {}, headers={}, principal=PRINCIPAL)["bank_id"]
        wrong = {"x-pl-bank": "22222222-2222-4222-8222-222222222222",
                 "x-pl-principal": PRINCIPAL}
        with pytest.raises(ValueError, match="bank_identity_mismatch"):
            dispatch(cold, "register", {}, headers=wrong, principal=PRINCIPAL)
        right = {"x-pl-bank": bank_id, "x-pl-principal": PRINCIPAL}
        agent = dispatch(cold, "register", {}, headers=right, principal=PRINCIPAL)
        creds = {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}
        dispatch(cold, "attach", {"attachment_id": "cold-attach"}, headers=creds,
                 principal=PRINCIPAL)
        dispatch(cold, "receive", {}, headers=creds, principal=PRINCIPAL)
        assert calls == [], "durable-tier actions must not initialize the full service"
        message = {"to": agent["agent_id"], "text": "note to self", "request_id": "cold-send"}
        assert dispatch(cold, "send", message, headers=creds,
                        principal=PRINCIPAL)["state"] == "queued"
        assert calls == ["init"]
        dispatch(cold, "send", {**message, "request_id": "cold-send-2"}, headers=creds,
                 principal=PRINCIPAL)
        assert calls == ["init"], "full init is paid once per process"
    finally:
        mailbox = getattr(cold, "_coordination_storage", None)
        if mailbox is not None:
            mailbox.close()
        if cold._storage is not None:
            cold._storage.close()


def test_send_during_initial_hydration_waits_for_clock_history(
    pg_service, tmp_path, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor

    from pseudolife_memory.memory.hlc import HybridLogicalClock
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.storage import sync
    from pseudolife_memory.storage.coordination import HLC_META_KEY

    pg_service._storage.close()  # the cold daemon is the bank's one writer
    cold = MemoryService(data_dir=tmp_path / "clock-startup")
    cold.config.coordination.enabled = True
    cold.config.coordination.allowed_principals = [PRINCIPAL]
    cold._hlc = HybridLogicalClock(now_ms=lambda: 100)
    agent = dispatch(cold, "register", {}, headers={}, principal=PRINCIPAL)
    creds = {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}
    cold._storage.set_meta(HLC_META_KEY, [10000, 7])
    hydration_started = threading.Event()
    release_hydration = threading.Event()
    send_started = threading.Event()
    original_hydrate = sync.hydrate_cms

    def paused_hydrate(*args, **kwargs):
        hydration_started.set()
        assert release_hydration.wait(10), "hydration was not released"
        return original_hydrate(*args, **kwargs)

    monkeypatch.setattr(sync, "hydrate_cms", paused_hydrate)

    def initialize():
        with cold._lock:
            cold._ensure_init()

    def send():
        send_started.set()
        return dispatch(cold, "send", {
            "to": agent["agent_id"], "text": "startup message",
            "request_id": "startup-clock",
        }, headers=creds, principal=PRINCIPAL)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            initializing = pool.submit(initialize)
            try:
                assert hydration_started.wait(10), "initialization did not reach hydration"
                sending = pool.submit(send)
                assert send_started.wait(2), "sender did not start"
                # The send is concurrent with an already-published CMS, but
                # it must not stamp a message until stored history is loaded.
                from concurrent.futures import TimeoutError
                with pytest.raises(TimeoutError):
                    sending.result(timeout=0.2)
            finally:
                release_hydration.set()
            initializing.result(timeout=10)
            assert sending.result(timeout=10)["state"] == "queued"
        row = cold._storage.conn.execute(
            "SELECT hlc FROM coordination_messages WHERE request_id=%s",
            ("startup-clock",),
        ).fetchone()
        assert tuple(map(int, row[0].split(":"))) > (10000, 7)
    finally:
        release_hydration.set()
        mailbox = getattr(cold, "_coordination_storage", None)
        if mailbox is not None:
            mailbox.close()
        if cold._storage is not None:
            cold._storage.close()
