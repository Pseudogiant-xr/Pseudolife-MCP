"""Grant outbox recovery must not initialize a cold service."""
from types import SimpleNamespace
import threading
import logging
import pytest

from pseudolife_memory import coordination
from pseudolife_memory.memory.hlc import HybridLogicalClock
from pseudolife_memory.storage.coordination import CoordinationError
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.test_coordination_leases import creds, store  # noqa: F401
from tests.test_lease_grant_recovery import queue


def service(store, ready):
    return SimpleNamespace(
        config=SimpleNamespace(coordination=SimpleNamespace(enabled=True)),
        _db_url="disposable", _storage=store.storage, _coordination_lock=threading.Lock(),
        coordination_tier_ready=lambda: ready[0], _hlc=HybridLogicalClock(),
    )


def test_cold_drain_returns_and_startup_replays_after_ready(store, monkeypatch):
    a, b = queue(store, 2)
    store.release_lease(*creds(a), name="resource")
    ready = [False]
    svc = service(store, ready)
    monkeypatch.setattr(coordination, "_store", lambda _svc: store)
    tiers = []
    monkeypatch.setattr(coordination, "_ensure_tier", lambda *_a, **kw: tiers.append(kw))
    coordination.drain_lease_grants(svc)
    assert tiers == [], "a cold drain must not initialize any tier"
    assert not store.receive(*creds(b))["messages"]
    ready[0] = True
    coordination.drain_lease_grants(svc)
    assert len(store.receive(*creds(b))["messages"]) == 1
    del svc._daemon_board_identity
    coordination.drain_lease_grants(svc)
    assert len(store.receive(*creds(b))["messages"]) == 1


def test_full_mailbox_does_not_starve_another_grant_notice(store, monkeypatch):
    a, b = queue(store, 2)
    c, d = [store.register("alice") for _ in range(2)]
    for who in (c, d):
        store.acquire_lease(*creds(who), name="second")
    store.release_lease(*creds(a), name="resource")
    store.release_lease(*creds(c), name="second")
    svc = service(store, [True])
    monkeypatch.setattr(coordination, "_store", lambda _svc: store)
    monkeypatch.setattr(coordination, "_ensure_tier", lambda *_a, **_kw: None)
    original = store.send_lease_grant
    def full(*args, **kwargs):
        if kwargs["name"] == "resource":
            raise CoordinationError("queue_full")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "send_lease_grant", full)
    coordination.drain_lease_grants(svc)
    assert len(store.receive(*creds(d))["messages"]) == 1
    assert [r["name"] for r in store.pending_lease_grants()] == ["resource"]
    monkeypatch.setattr(store, "send_lease_grant", original)
    del svc._daemon_board_identity
    coordination.drain_lease_grants(svc)
    assert len(store.receive(*creds(b))["messages"]) == 1


def test_failed_send_leaves_outbox_for_next_drain(store, monkeypatch):
    a, b = queue(store, 2)
    store.release_lease(*creds(a), name="resource")
    svc = service(store, [True])
    monkeypatch.setattr(coordination, "_store", lambda _svc: store)
    monkeypatch.setattr(coordination, "_ensure_tier", lambda *_a, **_kw: None)
    original = store.send_lease_grant
    def fail(*_a, **_kw):
        raise RuntimeError("interrupted drain")
    monkeypatch.setattr(store, "send_lease_grant", fail)
    coordination.drain_lease_grants(svc)
    assert len(store.pending_lease_grants()) == 1
    monkeypatch.setattr(store, "send_lease_grant", original)
    coordination.drain_lease_grants(svc)
    assert len(store.receive(*creds(b))["messages"]) == 1


def test_unavailable_board_does_not_fail_warmup(store, monkeypatch):
    svc = service(store, [True])
    def unavailable(*_a, **_kw):
        raise RuntimeError("board connection unavailable")
    monkeypatch.setattr(coordination, "_ensure_tier", unavailable)
    coordination.drain_lease_grants(svc)


def test_ready_history_never_drains_pending_grant(store, monkeypatch):
    from tests.test_coordination_auth import service as auth_service
    a, b = queue(store, 2)
    store.release_lease(*creds(a), name="resource")
    svc = auth_service(allowed=["alice"])
    svc._storage = store.storage
    svc._coordination_ready = True
    svc._coordination_lock = threading.RLock()
    svc._hlc = HybridLogicalClock()
    monkeypatch.setattr(coordination, "_store", lambda _: store)
    monkeypatch.setattr(coordination, "_ensure_tier", lambda *_a, **_kw: None)
    coordination.dispatch(svc, "history", {}, principal="alice", headers={
        "x-pl-agent": b["agent_id"], "x-pl-agent-key": b["credential"]})
    assert store.receive(*creds(b))["messages"] == []


def test_refused_first_page_does_not_starve_later_grants(store, monkeypatch):
    a, b = [store.register("alice") for _ in range(2)]
    for n in range(51):
        name = f"resource-{n:02}"
        for who in (a, b):
            store.acquire_lease(*creds(who), name=name)
        store.release_lease(*creds(a), name=name)
    svc = service(store, [True])
    monkeypatch.setattr(coordination, "_store", lambda _svc: store)
    monkeypatch.setattr(coordination, "_ensure_tier", lambda *_a, **_kw: None)
    original = store.send_lease_grant
    attempted = []
    def full(*args, **kwargs):
        attempted.append(kwargs["name"])
        if kwargs["name"] != "resource-50":
            raise CoordinationError("queue_full")
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "send_lease_grant", full)
    coordination.drain_lease_grants(svc)
    assert len(store.receive(*creds(b))["messages"]) == 1
    assert len(attempted) == len(set(attempted)) == 51


def test_refused_grant_logs_once_until_it_leaves_the_outbox(store, monkeypatch, caplog):
    a, _b = queue(store, 2)
    store.release_lease(*creds(a), name="resource")
    svc = service(store, [True])
    monkeypatch.setattr(coordination, "_store", lambda _svc: store)
    monkeypatch.setattr(coordination, "_ensure_tier", lambda *_a, **_kw: None)
    def full(*_args, **_kwargs):
        raise CoordinationError("queue_full")
    monkeypatch.setattr(store, "send_lease_grant", full)
    with caplog.at_level(logging.INFO, logger=coordination.__name__):
        coordination.drain_lease_grants(svc)
        coordination.drain_lease_grants(svc)
    assert sum("lease grant notice refused" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("trigger", ["receive", "release", "leases"])
def test_ready_dispatch_delivers_a_committed_grant(store, monkeypatch, trigger):
    from tests.test_coordination_auth import service as auth_service
    a, b = queue(store, 2)
    if trigger == "receive":
        store.release_lease(*creds(a), name="resource")
    elif trigger == "leases":
        store.test_time[0] += 121
        store.update(*creds(b), status="ready")
    svc = auth_service(allowed=["alice"])
    svc._storage = store.storage
    svc._coordination_ready = True
    svc._coordination_lock = threading.RLock()
    svc._hlc = HybridLogicalClock()
    monkeypatch.setattr(coordination, "_store", lambda _: store)
    monkeypatch.setattr(coordination, "_ensure_tier", lambda *_a, **_kw: None)
    caller = a if trigger == "release" else b
    body = {"name": "resource"} if trigger == "release" else {}
    result = coordination.dispatch(svc, trigger, body, principal="alice", headers={
        "x-pl-agent": caller["agent_id"], "x-pl-agent-key": caller["credential"]})
    if trigger == "receive":
        assert len(result["messages"]) == 1
    assert len(store.receive(*creds(b))["messages"]) == 1


def test_acceptance_between_outbox_read_and_send_suppresses_notice(store):
    a, b = queue(store, 2)
    store.release_lease(*creds(a), name="resource")
    [grant] = store.pending_lease_grants()
    store.acquire_lease(*creds(b), name="resource")
    from tests.test_lease_grant_recovery import daemon
    sender = daemon(store)
    assert store.send_lease_grant(sender["agent_id"], sender["credential"], **grant) is None
    assert store.receive(*creds(b))["messages"] == []
