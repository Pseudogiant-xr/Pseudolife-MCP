"""Board leases (schema v45): a named, expiring hold on a shared resource.

A process-held lease's truth is an OS file lock on the host (``pseudolife-mcp
lease run``); the board row mirrors it so peers see the holder, queue in
arrival order and see the expected end. A session-held lease (``coordinator``,
``claim:<path>``) lives only here. Either way the board grants a freed lease
to the head of its queue, which must renew within a short window or lose the
grant to the next in line: a sleeping session cannot stall the queue the way
the 2026-09-24 02:39 relay stall did.
"""
from contextlib import contextmanager
import json

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from pseudolife_memory.storage.coordination import (
    COORDINATION_SCHEMA_SQL, LEASE_GRANT_WINDOW, LEASE_QUEUE_MAX, CoordinationError,
    CoordinationStore, audit_events, verify_audit_chain,
)


class Storage:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def _txn(self):
        with self.conn.transaction():
            yield


@pytest.fixture
def store(pg_conn):
    pg_conn.autocommit = True
    pg_conn.execute(COORDINATION_SCHEMA_SQL)
    from pseudolife_memory.storage.schema import assert_disposable_database
    assert_disposable_database(pg_conn)
    pg_conn.execute("TRUNCATE coordination_messages, coordination_agents, coordination_events, "
                    "coordination_leases, coordination_lease_waiters")
    now = [1000.0]
    out = CoordinationStore(Storage(pg_conn), clock=lambda: now[0])
    out.test_time = now
    return out


def creds(agent, principal="alice"):
    return principal, agent["agent_id"], agent["credential"]


def events(store, kind=None):
    rows = list(audit_events(store.storage.conn))
    return [r for r in rows if kind is None or r["event"] == kind]


def payload(row):
    return json.loads(row["payload"])


def test_free_lease_is_granted_with_fence_and_expiry(store):
    a = store.register("alice", label="suite-runner")
    out = store.acquire_lease(*creds(a), name="full-suite", ttl=120, expect=1200,
                              purpose="pytest tests/")
    assert out["state"] == "held"
    assert out["fence"] == 1
    assert out["expires_at"] == 1120.0
    assert out["expected_end"] == 2200.0
    assert out["position"] is None and out["queued"] == 0
    assert out["holder"]["agent_id"] == a["agent_id"]
    assert out["holder"]["label"] == "suite-runner"
    assert out["holder"]["purpose"] == "pytest tests/"
    [row] = events(store, "lease_acquire")
    assert row["agent_id"] == a["agent_id"]
    assert payload(row) == {"name": "full-suite", "fence": 1, "ttl": 120, "expect": 1200,
                            "purpose": "pytest tests/"}


def test_second_agent_queues_once_and_sees_the_holder(store):
    a, b = store.register("alice", label="a"), store.register("alice", label="b")
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    first = store.acquire_lease(*creds(b), name="gpu", ttl=120, purpose="bench")
    assert first["state"] == "queued"
    assert first["position"] == 1 and first["queued"] == 1
    assert first["fence"] is None
    assert first["holder"]["agent_id"] == a["agent_id"]
    store.test_time[0] += 5
    again = store.acquire_lease(*creds(b), name="gpu", ttl=120)
    assert again["position"] == 1 and again["queued"] == 1
    # A repeat poll keeps its place and writes nothing.
    assert len(events(store, "lease_queue")) == 1


def test_holder_renewal_moves_expiry_without_an_audit_row(store):
    a = store.register("alice")
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    before = len(events(store))
    store.test_time[0] += 60
    out = store.acquire_lease(*creds(a), name="gpu", ttl=120, expect=30)
    assert out["state"] == "held" and out["fence"] == 1
    assert out["expires_at"] == 1180.0
    assert out["expected_end"] == 1090.0
    assert len(events(store)) == before


def test_release_grants_the_queue_head_with_an_acceptance_window(store):
    a, b, c = (store.register("alice") for _ in range(3))
    store.acquire_lease(*creds(a), name="full-suite", ttl=120)
    store.acquire_lease(*creds(b), name="full-suite", ttl=3600, expect=900)
    store.test_time[0] += 1
    store.acquire_lease(*creds(c), name="full-suite", ttl=120)
    store.test_time[0] += 10
    out = store.release_lease(*creds(a), name="full-suite")
    assert out == {"name": "full-suite", "released": True, "dequeued": False}
    view = store.acquire_lease(*creds(c), name="full-suite", ttl=120)
    assert view["state"] == "queued" and view["position"] == 1
    holder = view["holder"]
    assert holder["agent_id"] == b["agent_id"]
    # The grantee has the shorter of its ttl and the window to take it up.
    assert holder["expires_at"] == store.test_time[0] + LEASE_GRANT_WINDOW
    assert holder["expected_end"] == store.test_time[0] + 900
    [grant] = events(store, "lease_grant")
    assert grant["agent_id"] == b["agent_id"]
    assert payload(grant)["fence"] == 2
    assert [e["event"] for e in events(store)][-2:] == ["lease_release", "lease_grant"]
    taken = store.acquire_lease(*creds(b), name="full-suite", ttl=3600)
    assert taken["state"] == "held" and taken["fence"] == 2
    assert taken["expires_at"] == store.test_time[0] + 3600


def test_grant_lapses_to_the_next_waiter_when_not_taken(store):
    a, b, c = (store.register("alice") for _ in range(3))
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    store.acquire_lease(*creds(b), name="gpu", ttl=120)
    store.test_time[0] += 1
    store.acquire_lease(*creds(c), name="gpu", ttl=120)
    store.release_lease(*creds(a), name="gpu")
    store.test_time[0] += 121  # b's grant (ttl 120 < window) lapses untaken
    out = store.acquire_lease(*creds(c), name="gpu", ttl=120)
    assert out["state"] == "held" and out["fence"] == 3
    expired = events(store, "lease_expire")
    assert [e["agent_id"] for e in expired] == [b["agent_id"]]
    # b lost it: asking again queues b behind nobody but the holder.
    again = store.acquire_lease(*creds(b), name="gpu", ttl=120)
    assert again["state"] == "queued" and again["position"] == 1


def test_expired_holder_loses_the_lease_to_the_queue(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="live-daemon", ttl=60)
    store.acquire_lease(*creds(b), name="live-daemon", ttl=60)
    store.test_time[0] += 61
    out = store.acquire_lease(*creds(b), name="live-daemon", ttl=60)
    assert out["state"] == "held" and out["fence"] == 2
    lost = store.acquire_lease(*creds(a), name="live-daemon", ttl=60)
    assert lost["state"] == "queued" and lost["position"] == 1


def test_fence_rises_on_every_grant(store):
    a = store.register("alice")
    fences = []
    for _ in range(3):
        fences.append(store.acquire_lease(*creds(a), name="coordinator:p", ttl=3600)["fence"])
        store.release_lease(*creds(a), name="coordinator:p")
    assert fences == [1, 2, 3]


def test_release_by_a_waiter_dequeues_and_by_a_stranger_refuses(store):
    a, b, c = (store.register("alice") for _ in range(3))
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    store.acquire_lease(*creds(b), name="gpu", ttl=120)
    assert store.release_lease(*creds(b), name="gpu") == {
        "name": "gpu", "released": False, "dequeued": True}
    assert len(events(store, "lease_dequeue")) == 1
    with pytest.raises(CoordinationError, match="lease_not_held"):
        store.release_lease(*creds(c), name="gpu")
    with pytest.raises(CoordinationError, match="lease_not_held"):
        store.release_lease(*creds(c), name="never-existed")


@pytest.mark.parametrize("kwargs,code", [
    ({"name": ""}, "invalid_lease"),
    ({"name": "x" * 121}, "invalid_lease"),
    ({"name": "bad\nname"}, "invalid_lease"),
    ({"name": 7}, "invalid_lease"),
    ({"name": "gpu", "ttl": 29}, "invalid_ttl"),
    ({"name": "gpu", "ttl": 86401}, "invalid_ttl"),
    ({"name": "gpu", "ttl": True}, "invalid_ttl"),
    ({"name": "gpu", "ttl": 60.5}, "invalid_ttl"),
    ({"name": "gpu", "expect": 0}, "invalid_expect"),
    ({"name": "gpu", "expect": 7 * 86400 + 1}, "invalid_expect"),
    ({"name": "gpu", "purpose": "p" * 241}, "invalid_purpose"),
])
def test_lease_arguments_are_validated(store, kwargs, code):
    a = store.register("alice")
    with pytest.raises(CoordinationError, match=code):
        store.acquire_lease(*creds(a), **{"ttl": 120, **kwargs})
    assert events(store, "lease_acquire") == []


def test_queue_is_bounded(store):
    holder = store.register("alice")
    store.acquire_lease(*creds(holder), name="gpu", ttl=120)
    for _ in range(LEASE_QUEUE_MAX):
        store.acquire_lease(*creds(store.register("alice")), name="gpu", ttl=120)
    with pytest.raises(CoordinationError, match="lease_queue_full"):
        store.acquire_lease(*creds(store.register("alice")), name="gpu", ttl=120)


def test_wrong_credential_cannot_acquire_or_release(store):
    a, b = store.register("alice"), store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.acquire_lease("alice", a["agent_id"], b["credential"], name="gpu", ttl=120)
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.release_lease("mallory", a["agent_id"], a["credential"], name="gpu")


def test_list_shows_held_and_queued_leases_with_staleness(store):
    a, b = store.register("alice", label="runner"), store.register("alice", label="next")
    store.acquire_lease(*creds(a), name="full-suite", ttl=3600, expect=600, purpose="suite")
    store.acquire_lease(*creds(b), name="full-suite", ttl=120, purpose="my turn")
    store.acquire_lease(*creds(a), name="gpu", ttl=120)
    store.release_lease(*creds(a), name="gpu")  # free rows are not listed
    listed = store.list_leases()
    assert [lease["name"] for lease in listed["leases"]] == ["full-suite"]
    [lease] = listed["leases"]
    assert lease["holder"]["label"] == "runner" and lease["fence"] == 1
    assert lease["stale"] is False
    assert lease["queued"] == 1
    assert [w["label"] for w in lease["queue"]] == ["next"]
    assert lease["queue"][0]["purpose"] == "my turn"
    store.test_time[0] += 601
    assert store.list_leases(name="full-suite")["leases"][0]["stale"] is True
    assert store.list_leases(name="gpu")["leases"] == []


def test_list_settles_an_expired_hold_before_reporting(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="gpu", ttl=60)
    store.acquire_lease(*creds(b), name="gpu", ttl=60)
    store.test_time[0] += 61
    [lease] = store.list_leases()["leases"]
    assert lease["holder"]["agent_id"] == b["agent_id"] and lease["queued"] == 0
    assert [e["event"] for e in events(store)][-2:] == ["lease_expire", "lease_grant"]


def test_list_agents_carries_the_leases(store):
    a, b = store.register("alice", label="runner"), store.register("alice")
    store.acquire_lease(*creds(a), name="coordinator:pseudolife-mcp", ttl=3600)
    listed = store.list_agents(*creds(b))
    assert [lease["name"] for lease in listed["leases"]] == ["coordinator:pseudolife-mcp"]


def test_prune_keeps_a_live_holder_and_drops_a_departed_waiter(store):
    holder = store.register("alice", capabilities={"resumable": False})
    waiter = store.register("alice", capabilities={"resumable": False})
    store.acquire_lease(*creds(holder), name="coordinator:p", ttl=86400)
    store.acquire_lease(*creds(waiter), name="coordinator:p", ttl=3600)
    store.test_time[0] += 7200  # both idle past the ephemeral window
    store.prune()
    rows = store.storage.conn.execute(
        "SELECT agent_id FROM coordination_agents").fetchall()
    assert [r[0] for r in rows] == [holder["agent_id"]]
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_lease_waiters").fetchone()[0] == 0
    [lease] = store.list_leases()["leases"]
    assert lease["holder"]["agent_id"] == holder["agent_id"] and lease["queued"] == 0


def test_prune_settles_expired_holds_before_removing_their_holder(store):
    gone = store.register("alice", capabilities={"resumable": False})
    store.acquire_lease(*creds(gone), name="gpu", ttl=60)
    store.test_time[0] += 7200
    store.prune()
    # Checked before any listing, which would settle the hold itself.
    [expired] = events(store, "lease_expire")
    assert expired["agent_id"] == gone["agent_id"] and expired["actor"] == "daemon"
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_agents").fetchone()[0] == 0
    assert store.list_leases()["leases"] == []
    row = store.storage.conn.execute(
        "SELECT holder_agent_id, fence FROM coordination_leases WHERE name='gpu'").fetchone()
    assert row == (None, 1)


def test_operator_break_frees_the_lease_and_grants_the_next(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="live-daemon", ttl=3600)
    store.acquire_lease(*creds(b), name="live-daemon", ttl=3600)
    out = store.break_lease("live-daemon")
    assert out == {"name": "live-daemon", "broken": True, "was_held_by": a["agent_id"]}
    [row] = events(store, "lease_break")
    assert row["actor"] == "operator" and row["principal"] == ""
    [lease] = store.list_leases()["leases"]
    assert lease["holder"]["agent_id"] == b["agent_id"]
    assert store.break_lease("never-existed") == {
        "name": "never-existed", "broken": False, "was_held_by": None}


def test_status_expectation_marks_a_status_overdue(store):
    a, b = store.register("alice"), store.register("alice")
    store.update(*creds(a), status="suite=running", expect=1200)
    row = store.list_agents(*creds(b))["agents"][0]
    assert row["status_expires_at"] == 2200.0 and row["status_overdue"] is False
    store.test_time[0] += 1201
    assert store.list_agents(*creds(b))["agents"][0]["status_overdue"] is True
    # A new status without an expectation clears the old one.
    store.update(*creds(a), status="idle")
    row = store.list_agents(*creds(b))["agents"][0]
    assert row["status_expires_at"] is None and row["status_overdue"] is False
    [*_, last] = events(store, "update")
    assert payload(last)["fields"] == {"status": "idle", "status_expires_at": None}
    with pytest.raises(CoordinationError, match="invalid_expect"):
        store.update(*creds(a), status="x", expect=0)


def test_expect_alone_retimes_the_current_status(store):
    a, b = store.register("alice"), store.register("alice")
    store.update(*creds(a), status="deploying")
    store.update(*creds(a), expect=300)
    row = store.list_agents(*creds(b))["agents"][0]
    assert row["status"] == "deploying" and row["status_expires_at"] == 1300.0


def test_lease_history_keeps_the_audit_chain_valid(store):
    a, b = store.register("alice"), store.register("alice")
    store.acquire_lease(*creds(a), name="gpu", ttl=60)
    store.acquire_lease(*creds(b), name="gpu", ttl=60)
    store.test_time[0] += 61
    store.list_leases()
    store.release_lease(*creds(b), name="gpu")
    store.break_lease("gpu")
    report = verify_audit_chain(list(audit_events(store.storage.conn)))
    assert report["ok"] is True
