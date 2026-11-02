"""Grant outbox recovery must not initialize a cold service."""
from types import SimpleNamespace
import threading

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
