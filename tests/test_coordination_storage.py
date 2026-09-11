"""Mailbox ownership, leases, mutation durability and concurrent ordering."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import time

import psycopg
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from pseudolife_memory.storage.coordination import (
    COORDINATION_SCHEMA_SQL, CoordinationError, CoordinationStore,
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
    pg_conn.execute("TRUNCATE coordination_messages, coordination_agents")
    now = [1000.0]
    out = CoordinationStore(Storage(pg_conn), clock=lambda: now[0])
    out.test_time = now
    return out


def creds(a, principal="alice"):
    return principal, a["agent_id"], a["credential"]


def pair(store):
    return store.register("alice"), store.register("alice")


def test_credentials_and_public_views(store):
    a, b = pair(store)
    assert a["credential"] != b["credential"]
    for args in [("bob", a["agent_id"], a["credential"]),
                 ("alice", a["agent_id"], b["credential"])]:
        with pytest.raises(CoordinationError, match="unauthorized"):
            store.authenticate(*args)
    assert "credential" not in store.authenticate(*creds(a))
    raw = store.storage.conn.execute("SELECT credential_hash FROM coordination_agents WHERE agent_id=%s", (a["agent_id"],)).fetchone()[0]
    assert raw != a["credential"]
    store.update(*creds(a), project="p", task="t", status="reviewing")
    assert store.list_agents(*creds(b), project="p")["agents"][0]["status"] == "reviewing"


def test_malformed_credential_rejected_without_encoding_error(store):
    agent = store.register("alice")
    with pytest.raises(CoordinationError, match="unauthorized"):
        store.authenticate("alice", agent["agent_id"], "\ud800")


def test_attach_expiry_fences_stale_adapter(store):
    a = store.register("alice", wake_enabled=True)
    first = store.attach(*creds(a), attachment_id="one", wake_enabled=True)
    assert store.attach(*creds(a), attachment_id="one")["generation"] == first["generation"]
    with pytest.raises(CoordinationError, match="attachment_busy"):
        store.attach(*creds(a), attachment_id="two")
    store.test_time[0] += 61
    second = store.attach(*creds(a), attachment_id="two")
    assert second["generation"] > first["generation"]
    with pytest.raises(CoordinationError, match="stale_attachment"):
        store.detach(*creds(a), attachment_id="one", generation=first["generation"])
    assert store.heartbeat(*creds(a), attachment_id="two", generation=second["generation"])["lease_until"] > store.test_time[0]
    store.detach(*creds(a), attachment_id="two", generation=second["generation"])


def test_send_retry_receive_ack_and_reply_ownership(store):
    a, b = pair(store)
    msg = store.send(*creds(a), to=b["agent_id"], text="review", request_id="r1")
    store.test_time[0] += 2
    assert store.send(*creds(a), to=b["agent_id"], text="review", request_id="r1")["message_id"] == msg["message_id"]
    with pytest.raises(CoordinationError, match="request_conflict"):
        store.send(*creds(a), to=b["agent_id"], text="changed", request_id="r1")
    got = store.receive(*creds(b))
    assert got["messages"][0]["text"] == "review"
    assert len(store.receive(*creds(b))["messages"]) == 1
    assert store.receive(*creds(b), after=got["after"])["messages"] == []
    with pytest.raises(CoordinationError, match="invalid_cursor"):
        store.receive(*creds(a), after=got["after"])
    with pytest.raises(CoordinationError, match="message_not_found"):
        store.ack(*creds(a), message_id=msg["message_id"])
    reply = store.send(*creds(b), to=a["agent_id"], text="done", request_id="reply", reply_to=msg["message_id"])
    assert reply["state"] == "queued"
    c = store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_reply"):
        store.send(*creds(c), to=a["agent_id"], text="spoof", request_id="r2", reply_to=msg["message_id"])
    assert store.ack(*creds(b), message_id=msg["message_id"]) == store.ack(*creds(b), message_id=msg["message_id"])
    assert store.receive(*creds(b))["messages"] == []


def test_expiry_retains_dedupe_and_prune_removes_body(store):
    a, b = pair(store)
    msg = store.send(*creds(a), to=b["agent_id"], text="ephemeral", request_id="r")
    store.test_time[0] += 86401
    assert store.receive(*creds(b))["messages"] == []
    store.prune()
    assert store.storage.conn.execute("SELECT text FROM coordination_messages WHERE message_id=%s", (msg["message_id"],)).fetchone() == (None,)
    assert store.send(*creds(a), to=b["agent_id"], text="ephemeral", request_id="r")["message_id"] == msg["message_id"]
    store.test_time[0] += 7 * 86400
    store.prune()
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_messages").fetchone() == (0,)


def test_attempt_is_not_ack_and_restore_revokes_credentials(store):
    a = store.register("alice")
    b = store.register("alice", wake_enabled=True)
    msg = store.send(*creds(a), to=b["agent_id"], text="x", request_id="r")
    attachment = store.attach(*creds(b), attachment_id="one", wake_enabled=True)
    args = dict(message_id=msg["message_id"], attachment_id="one", generation=attachment["generation"])
    assert store.mark_attempt(*creds(b), **args)["state"] == "attempted"
    store.mark_attempt(*creds(b), **args)
    assert store.storage.conn.execute("SELECT attempts FROM coordination_messages").fetchone() == (1,)
    assert len(store.receive(*creds(b))["messages"]) == 1
    store.recover()
    with pytest.raises(CoordinationError, match="unauthorized"):
        store.mark_attempt(*creds(b), **args)
    recovered = store.rebind(b["agent_id"], "alice")
    assert recovered["wake_enabled"] is False
    assert store.receive(*creds(recovered))["messages"][0]["message_id"] == msg["message_id"]


def test_attachment_resume_requires_fresh_wake_opt_in(store):
    a = store.register("alice", wake_enabled=True)
    first = store.attach(*creds(a), attachment_id="one", wake_enabled=True)
    store.attach(*creds(a), attachment_id="one")
    state = store.check_attachment(*creds(a), attachment_id="one", generation=first["generation"])
    assert state["wake_enabled"] is False


def test_registration_contract_and_bounded_metadata(store):
    a = store.register("alice", status="reviewing", capabilities={"pull": True, "channel": False})
    assert a["capabilities"] == {"pull": True, "channel": False}
    with pytest.raises(CoordinationError, match="invalid_status"):
        store.update(*creds(a), status="x" * 241)
    with pytest.raises(CoordinationError, match="invalid_capabilities"):
        store.update(*creds(a), capabilities={"pull": "yes"})


def test_send_rejects_unencodable_body_without_leaking_it(store):
    a, b = pair(store)
    with pytest.raises(CoordinationError, match="invalid_text"):
        store.send(*creds(a), to=b["agent_id"], text="\ud800", request_id="r")


def test_limits_are_explicit_and_send_rate_is_atomic(store):
    a, b = pair(store)
    with pytest.raises(CoordinationError, match="invalid_text"):
        store.send(*creds(a), to=b["agent_id"], text="é" * 4097, request_id="large")
    for n in range(60):
        store.send(*creds(a), to=b["agent_id"], text="x", request_id=str(n))
    with pytest.raises(CoordinationError, match="rate_limited"):
        store.send(*creds(a), to=b["agent_id"], text="x", request_id="over")
    assert len(store.receive(*creds(b), limit=50)["messages"]) == 50
    with pytest.raises(CoordinationError, match="invalid_limit"):
        store.receive(*creds(b), limit=51)


def test_queue_capacity_never_discards_pending(store):
    a, b = pair(store)
    for n in range(256):
        store.test_time[0] += 2
        store.send(*creds(a), to=b["agent_id"], text="x", request_id=str(n))
    with pytest.raises(CoordinationError, match="queue_full"):
        store.send(*creds(a), to=b["agent_id"], text="x", request_id="over")


def test_commit_order_does_not_skip_blocked_sender(store, pg_url):
    a, b = pair(store)
    c = store.register("alice")
    started = threading.Event()
    with psycopg.connect(pg_url, autocommit=True) as conn:
        other = CoordinationStore(Storage(conn), clock=store.clock)
        def send_later():
            started.set()
            return other.send(*creds(c), to=b["agent_id"], text="second", request_id="r2")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with store.storage._txn():
                first = store.send(*creds(a), to=b["agent_id"], text="first", request_id="r1")
                future = pool.submit(send_later)
                assert started.wait(2)
                # The second connection cannot observe or skip an uncommitted row.
                with psycopg.connect(pg_url, autocommit=True) as reader:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        waiting = reader.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                                                 (conn.info.backend_pid,)).fetchone()
                        if waiting == ("Lock",):
                            break
                        time.sleep(0.01)
                    assert waiting == ("Lock",), "second sender never reached the locked recipient"
                    assert reader.execute("SELECT count(*) FROM coordination_messages").fetchone() == (0,)
            second = future.result(timeout=6)
    page = store.receive(*creds(b), limit=1)
    assert page["messages"][0]["message_id"] == first["message_id"]
    assert store.receive(*creds(b), after=page["after"])["messages"][0]["message_id"] == second["message_id"]


def test_reconnect_keeps_mail_and_credentials_and_lease(store, pg_url):
    a, b = pair(store)
    store.attach(*creds(b), attachment_id="original", wake_enabled=True)
    sent = store.send(*creds(a), to=b["agent_id"], text="pending", request_id="r")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        resumed = CoordinationStore(Storage(conn), clock=store.clock)
        assert resumed.receive(*creds(b))["messages"][0]["message_id"] == sent["message_id"]
        with pytest.raises(CoordinationError, match="attachment_busy"):
            resumed.attach(*creds(b), attachment_id="different")


def test_two_connections_cannot_attach_simultaneously(store, pg_url):
    a = store.register("alice")
    barrier = threading.Barrier(2)
    def attach(attachment):
        with psycopg.connect(pg_url, autocommit=True) as conn:
            other = CoordinationStore(Storage(conn), clock=store.clock)
            barrier.wait(timeout=3)
            try:
                return other.attach(*creds(a), attachment_id=attachment)
            except CoordinationError as exc:
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attach, ["one", "two"]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "attachment_busy" in results


def test_concurrent_retries_return_one_receipt(store, pg_url):
    a, b = pair(store)
    barrier = threading.Barrier(2)
    def send(_):
        with psycopg.connect(pg_url, autocommit=True) as conn:
            other = CoordinationStore(Storage(conn), clock=store.clock)
            barrier.wait(timeout=3)
            return other.send(*creds(a), to=b["agent_id"], text="once", request_id="same")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, [1, 2]))
    assert results[0] == results[1]
    assert len(store.receive(*creds(b))["messages"]) == 1


def test_hlc_highwater_is_numeric_transactional_and_survives_pruning(store):
    store.storage.conn.execute("DELETE FROM meta WHERE key='coordination_hlc_highwater'")
    a, b = pair(store)
    for number, stamp in enumerate(("9000:9", "10000:1", "9000:10")):
        store.send(*creds(a), to=b["agent_id"], text="clock", request_id=str(number), hlc=stamp)
    def highwater():
        row = store.storage.conn.execute(
            "SELECT value FROM meta WHERE key='coordination_hlc_highwater'").fetchone()
        return row[0] if row else None
    assert highwater() == [10000, 1]
    with pytest.raises(RuntimeError, match="rollback"):
        with store.storage._txn():
            store.send(*creds(a), to=b["agent_id"], text="rollback", request_id="rolled-back", hlc="20000:0")
            raise RuntimeError("rollback")
    assert highwater() == [10000, 1]
    store.test_time[0] += 8 * 86400
    store.prune()
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_messages").fetchone() == (0,)
    assert highwater() == [10000, 1]


def test_attachment_metadata_counts_only_unacknowledged_unexpired_mail(store):
    a, b = pair(store)
    attachment = store.attach(*creds(b), attachment_id="one")
    assert attachment["pending_count"] == 0
    first = store.send(*creds(a), to=b["agent_id"], text="one", request_id="one")
    store.send(*creds(a), to=b["agent_id"], text="two", request_id="two")
    heartbeat = dict(attachment_id="one", generation=attachment["generation"])
    assert store.heartbeat(*creds(b), **heartbeat)["pending_count"] == 2
    store.ack(*creds(b), message_id=first["message_id"])
    assert store.heartbeat(*creds(b), **heartbeat)["pending_count"] == 1
    store.test_time[0] += 86401
    assert store.attach(*creds(b), attachment_id="two")["pending_count"] == 0
