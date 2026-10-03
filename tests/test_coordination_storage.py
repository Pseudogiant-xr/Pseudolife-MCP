"""Mailbox ownership, leases, mutation durability and concurrent ordering."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
import time

import psycopg
import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from pseudolife_memory.storage.coordination import (
    COORDINATION_SCHEMA_SQL, DONE_REOPEN_BY, WITHHELD_RETRY, CoordinationError,
    CoordinationStore,
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
                    "coordination_wakes, coordination_leases, coordination_lease_waiters")
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
        with pytest.raises(CoordinationError, match="invalid_credential"):
            store.authenticate(*args)
    with pytest.raises(CoordinationError, match="instance_not_found"):
        store.authenticate("alice", "missing-agent", a["credential"])
    assert "credential" not in store.authenticate(*creds(a))
    raw = store.storage.conn.execute("SELECT credential_hash FROM coordination_agents WHERE agent_id=%s", (a["agent_id"],)).fetchone()[0]
    assert raw != a["credential"]
    store.update(*creds(a), project="p", task="t", status="reviewing")
    assert store.list_agents(*creds(b), project="p")["agents"][0]["status"] == "reviewing"


def test_malformed_credential_rejected_without_encoding_error(store):
    agent = store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_credential"):
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
    with pytest.raises(CoordinationError, match="invalid_credential"):
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


def test_reopen_replay_preserves_mail_and_fences_takeover_expiry_and_redaction(store, pg_url):
    """One disposable history across a new connection, not a server restart."""
    import uuid
    from pseudolife_memory.storage.coordination import (
        CoordinationConnection, audit_events, verify_audit_chain,
    )

    tag = uuid.uuid4().hex
    name = "recovery:" + tag
    a, b = pair(store)
    first = store.attach(*creds(b), attachment_id="first")
    held = store.acquire_lease(*creds(a), name=name, ttl=60)
    assert store.acquire_lease(*creds(b), name=name, ttl=60)["state"] == "queued"
    kept = store.send(*creds(a), to=b["agent_id"], text="synthetic retained note", request_id=tag)
    removed = store.send(*creds(a), to=b["agent_id"], text="synthetic removed note", request_id=tag + "-redact")
    connection = CoordinationConnection(pg_url)
    try:
        reopened = CoordinationStore(connection, clock=lambda: store.test_time[0])
        # A fresh connection authenticates the same durable addresses.
        assert [m["message_id"] for m in reopened.receive(*creds(b))["messages"]] == [
            kept["message_id"], removed["message_id"]]
        assert reopened.send(*creds(a), to=b["agent_id"], text="synthetic retained note",
                             request_id=tag)["message_id"] == kept["message_id"]
        store.test_time[0] += 61
        replacement = reopened.attach(*creds(b), attachment_id="replacement")
        assert replacement["generation"] > first["generation"]
        with pytest.raises(CoordinationError, match="^stale_attachment$"):
            reopened.heartbeat(*creds(b), attachment_id="first", generation=first["generation"])
        taken = reopened.acquire_lease(*creds(b), name=name, ttl=60)
        assert taken["state"] == "held" and taken["fence"] > held["fence"]
        with pytest.raises(CoordinationError, match="^lease_not_held$"):
            reopened.release_lease(*creds(a), name=name)
        assert reopened.release_lease(*creds(b), name=name)["released"]
        redaction = reopened.redact(removed["message_id"], "synthetic removal")
        assert redaction["live_body_cleared"] and redaction["audit_copy"] == "removed"
        assert [m["message_id"] for m in reopened.receive(*creds(b))["messages"]] == [kept["message_id"]]
        reopened.ack(*creds(b), message_id=kept["message_id"])
        expired = reopened.send(*creds(a), to=b["agent_id"], text="synthetic expiring note",
                                request_id=tag + "-expire")
        store.test_time[0] += 86401
        assert reopened.receive(*creds(b))["messages"] == []
        reopened.prune()
        assert connection.conn.execute(
            "SELECT text FROM coordination_messages WHERE message_id=%s",
            (expired["message_id"],)).fetchone() == (None,)
        assert reopened.send(*creds(a), to=b["agent_id"], text="synthetic expiring note",
                             request_id=tag + "-expire")["message_id"] == expired["message_id"]
        rows = list(audit_events(connection.conn))
        assert verify_audit_chain(rows)["ok"]
        sent = next(row for row in rows if row["event"] == "send"
                    and row["message_id"] == removed["message_id"])
        assert sent["body"] is None and sent["body_salt"] is None
    finally:
        connection.close()


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


def test_prune_removes_idle_unattached_agents_and_keeps_active_or_referenced_ones(store):
    """A shim without a state path registers a new address per launch, so
    idle addresses accumulate and crowd the peer list. Pruning removes an
    address once it has been inactive for the retention window, holds no
    lease, and is referenced by no retained message; an active lease, recent
    activity or retained mail keeps it."""
    from pseudolife_memory.storage.coordination import AGENT_RETENTION, DEDUPE_RETENTION
    assert AGENT_RETENTION == DEDUPE_RETENTION
    ghost = store.register("alice")
    active = store.register("alice")
    attached = store.register("alice")
    sender, referenced = store.register("alice"), store.register("alice")
    store.test_time[0] += AGENT_RETENTION - 3600
    store.send(*creds(sender), to=referenced["agent_id"], text="keep", request_id="r")
    store.test_time[0] += 3601
    store.update(*creds(active), status="still here")
    store.attach(*creds(attached), attachment_id="live")
    store.prune()
    remaining = {row[0] for row in store.storage.conn.execute(
        "SELECT agent_id FROM coordination_agents").fetchall()}
    assert ghost["agent_id"] not in remaining
    assert remaining == {active["agent_id"], attached["agent_id"],
                         sender["agent_id"], referenced["agent_id"]}
    with pytest.raises(CoordinationError, match="instance_not_found"):
        store.authenticate(*creds(ghost))


def test_attempts_are_bounded_across_generations_for_live_delivery_only(store):
    """Each new attachment may attempt an unacknowledged message once, but
    only up to ``MAX_ATTEMPTS`` in total: past that, live delivery skips the
    message so it cannot wake the host on every restart, while an explicit
    ``receive`` still returns it for the recipient to read and acknowledge."""
    from pseudolife_memory.storage.coordination import MAX_ATTEMPTS
    a = store.register("alice")
    b = store.register("alice", wake_enabled=True)
    msg = store.send(*creds(a), to=b["agent_id"], text="x", request_id="r")
    for _ in range(MAX_ATTEMPTS):
        attachment = store.attach(*creds(b), attachment_id="att", wake_enabled=True)
        args = dict(message_id=msg["message_id"], attachment_id="att",
                    generation=attachment["generation"])
        assert store.mark_attempt(*creds(b), **args)["state"] == "attempted"
        store.detach(*creds(b), **{k: args[k] for k in ("attachment_id", "generation")})
    attachment = store.attach(*creds(b), attachment_id="att", wake_enabled=True)
    args = dict(message_id=msg["message_id"], attachment_id="att",
                generation=attachment["generation"])
    with pytest.raises(CoordinationError, match="attempts_exhausted"):
        store.mark_attempt(*creds(b), **args)
    assert store.receive(*creds(b), for_delivery=True)["messages"] == []
    delivered = store.receive(*creds(b))["messages"]
    assert [m["message_id"] for m in delivered] == [msg["message_id"]]
    assert store.ack(*creds(b), message_id=msg["message_id"])["state"] == "acknowledged"


def test_acknowledging_counts_as_activity_for_retention(store):
    """A client that only reads and acknowledges, holding no lease, is not
    idle: its acknowledgment refreshes last_activity, so it survives the
    retention sweep that removes a sender who did nothing since."""
    from pseudolife_memory.storage.coordination import AGENT_RETENTION
    sender, reader = pair(store)
    msg = store.send(*creds(sender), to=reader["agent_id"], text="x", request_id="r")
    store.test_time[0] += AGENT_RETENTION - 3600
    store.ack(*creds(reader), message_id=msg["message_id"])
    store.test_time[0] += 2 * 3600 + 1
    store.prune()
    remaining = {row[0] for row in store.storage.conn.execute(
        "SELECT agent_id FROM coordination_agents").fetchall()}
    assert remaining == {reader["agent_id"]}


def test_received_messages_are_labelled_agent_origin(store):
    """Every message a recipient reads carries its origin label beside the
    daemon-verified sender fields, so a consumer never has to infer it from
    the text."""
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="please review", request_id="r")
    message = store.receive(*creds(b))["messages"][0]
    assert message["origin"] == "agent"
    assert message["sender_principal"] == "alice"


def _children(row):
    return [(child["label"], child["since"]) for child in row["children"]]


def test_children_are_set_kept_and_cleared_by_their_parent(store):
    """A parent names the subagents working under its address. The daemon
    stamps ``since``; a label carried into the next update keeps its time."""
    a, b = pair(store)
    assert store.authenticate(*creds(a))["children"] == []
    store.update(*creds(a), status="orchestrating", children=["review storage", "tests"])
    listed = store.list_agents(*creds(b))["agents"][0]
    assert _children(listed) == [("review storage", 1000.0), ("tests", 1000.0)]
    store.test_time[0] = 1010.0
    store.update(*creds(a), status="still orchestrating")
    assert _children(store.authenticate(*creds(a))) == [("review storage", 1000.0),
                                                         ("tests", 1000.0)]
    store.test_time[0] = 1020.0
    out = store.update(*creds(a), children=["tests", "docs"])
    assert _children(out) == [("tests", 1000.0), ("docs", 1020.0)]
    # A children-only update is not a new status.
    assert store.list_agents(*creds(b))["agents"][0]["status_set_at"] == 1010.0
    assert store.update(*creds(a), children=[])["children"] == []
    assert store.list_agents(*creds(b))["agents"][0]["children"] == []


@pytest.mark.parametrize("children", [
    [f"child {i}" for i in range(9)],  # over the 8-entry cap
    ["y" * 41],                                     # over the 40-character cap
    [""], ["   "], ["line\nbreak"], [7], "tests", {"label": "tests"},
    ["tests", "tests"],
])
def test_children_are_bounded(store, children):
    a = store.register("alice")
    store.update(*creds(a), children=["kept"])
    with pytest.raises(CoordinationError, match="invalid_children"):
        store.update(*creds(a), children=children)
    assert _children(store.authenticate(*creds(a))) == [("kept", 1000.0)]


def test_children_at_the_caps_are_accepted(store):
    a = store.register("alice")
    labels = [f"{i}" + "z" * 39 for i in range(8)]
    assert [c["label"] for c in store.update(*creds(a), children=labels)["children"]] == labels


def test_credential_shaped_child_label_is_refused(store):
    a = store.register("alice")
    shaped = "gh" + "p_" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2"  # 40 characters
    assert len(shaped) == 40
    with pytest.raises(CoordinationError, match="secret_like_body"):
        store.update(*creds(a), children=[shaped])


# --- subagents: the parent link and hook-kept children (schema v50) --------
# A Codex native child (collaboration.spawn_agent) has its own thread and so
# its own row; the shim passes the parent thread it read from Codex's turn
# metadata, and the daemon links the row to the parent's row under the same
# principal and the Codex namespace, whenever the parent registers. A Claude
# Code subagent shares its parent's row; the plugin's SubagentStart and
# SubagentStop hooks keep it listed among the parent's children.

PARENT_THREAD = "01a0ec35-a19d-7043-9336-ac6b9863afd7"
CHILD_THREAD = "01a0ec35-a4b3-7651-a945-81ed1f6cb638"
CODEX = {"pull": True, "channel": False, "codex": False, "resumable": True}


def codex_agent(store, thread, principal="alice", **kwargs):
    return store.register(principal, episode=thread, capabilities=dict(CODEX), **kwargs)


def test_a_codex_child_links_to_its_registered_parent(store):
    parent = codex_agent(store, PARENT_THREAD)
    child = codex_agent(store, CHILD_THREAD, parent_thread=PARENT_THREAD)
    peer = store.register("alice")
    assert child["parent_agent_id"] == parent["agent_id"] and child["subagent"] is True
    assert parent["parent_agent_id"] is None and parent["subagent"] is False
    assert peer["parent_agent_id"] is None and peer["subagent"] is False
    listed = {a["agent_id"]: a for a in store.list_agents(*creds(peer))["agents"]}
    assert listed[child["agent_id"]]["parent_agent_id"] == parent["agent_id"]
    assert listed[parent["agent_id"]]["parent_agent_id"] is None
    # The register event records the thread it named and the row it found.
    payload = store.storage.conn.execute(
        "SELECT payload FROM coordination_events WHERE event='register' AND agent_id=%s",
        (child["agent_id"],)).fetchone()[0]
    assert PARENT_THREAD in payload and parent["agent_id"] in payload


def test_a_child_registered_before_its_parent_links_when_the_parent_registers(store):
    """Codex registers a thread on its first Pseudolife call, and a parent
    may make none before it spawns: the link is made when it does."""
    child = codex_agent(store, CHILD_THREAD, parent_thread=PARENT_THREAD)
    assert child["parent_agent_id"] is None and child["subagent"] is True
    store.test_time[0] = 1005.0
    parent = codex_agent(store, PARENT_THREAD)
    assert store.authenticate(*creds(child))["parent_agent_id"] == parent["agent_id"]
    # The late link is a logged mutation of the child's row.
    rows = store.storage.conn.execute(
        "SELECT actor, payload FROM coordination_events WHERE event='update' AND agent_id=%s",
        (child["agent_id"],)).fetchall()
    assert [(actor, parent["agent_id"] in payload) for actor, payload in rows] == [("daemon", True)]


def test_the_parent_link_stays_inside_the_principal_and_the_codex_namespace(store):
    child = codex_agent(store, CHILD_THREAD, parent_thread=PARENT_THREAD)
    codex_agent(store, PARENT_THREAD, principal="bob")         # another principal
    store.register("alice", episode=PARENT_THREAD)             # not a Codex thread
    assert store.authenticate(*creds(child))["parent_agent_id"] is None
    parent = codex_agent(store, PARENT_THREAD)
    assert store.authenticate(*creds(child))["parent_agent_id"] == parent["agent_id"]


def test_a_pruned_parent_unlinks_and_its_next_registration_relinks(store):
    parent = codex_agent(store, PARENT_THREAD, wake_enabled=False)
    child = codex_agent(store, CHILD_THREAD, parent_thread=PARENT_THREAD)
    # Only the parent goes idle past the retention window.
    store.test_time[0] += 8 * 86400
    store.storage.conn.execute("UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s",
                               (store.test_time[0], child["agent_id"]))
    store.prune()
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_agents WHERE agent_id=%s",
                                      (parent["agent_id"],)).fetchone()[0] == 0
    assert store.authenticate(*creds(child))["parent_agent_id"] is None
    # The prune event names the removed parent, and the chain still verifies.
    from pseudolife_memory.storage.coordination import audit_events, verify_audit_chain
    events = list(audit_events(store.storage.conn))
    [pruned] = [e for e in events if e["event"] == "prune"]
    assert parent["agent_id"] in pruned["payload"]
    assert verify_audit_chain(events)["ok"] is True
    again = codex_agent(store, PARENT_THREAD)
    assert store.authenticate(*creds(child))["parent_agent_id"] == again["agent_id"]


def test_a_parent_registered_again_takes_over_a_live_link(store):
    """A thread that registers a new address while its old row is still
    live (its state file was lost, say) relinks its children to the new
    row; the daemon's update event records the row it replaced."""
    first = codex_agent(store, PARENT_THREAD)
    child = codex_agent(store, CHILD_THREAD, parent_thread=PARENT_THREAD)
    store.test_time[0] = 1005.0
    second = codex_agent(store, PARENT_THREAD)
    assert store.authenticate(*creds(child))["parent_agent_id"] == second["agent_id"]
    [payload] = [json.loads(p) for (p,) in store.storage.conn.execute(
        "SELECT payload FROM coordination_events WHERE event='update' AND actor='daemon' "
        "AND agent_id=%s", (child["agent_id"],)).fetchall()]
    assert payload == {"fields": {"parent_agent_id": second["agent_id"]},
                       "before": {"parent_agent_id": first["agent_id"]}}
    # A child registering now picks the most recently active of the two.
    store.test_time[0] = 1010.0
    store.update(*creds(first), status="still here")
    late = codex_agent(store, "01a0ec35-ffff-7651-a945-81ed1f6cb638", parent_thread=PARENT_THREAD)
    assert late["parent_agent_id"] == first["agent_id"]


@pytest.mark.parametrize("parent_thread,capabilities,episode", [
    ("not-a-uuid", CODEX, CHILD_THREAD),
    (PARENT_THREAD.upper(), CODEX, CHILD_THREAD),              # not canonical
    ("{" + PARENT_THREAD + "}", CODEX, CHILD_THREAD),
    (7, CODEX, CHILD_THREAD),
    (CHILD_THREAD, CODEX, CHILD_THREAD),                       # its own thread
    (PARENT_THREAD, {"pull": True, "channel": False}, CHILD_THREAD),  # not a Codex row
])
def test_the_parent_thread_is_validated_before_anything_is_written(
        store, parent_thread, capabilities, episode):
    with pytest.raises(CoordinationError, match="invalid_parent"):
        store.register("alice", episode=episode, capabilities=dict(capabilities),
                       parent_thread=parent_thread)
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_agents").fetchone()[0] == 0


def test_a_subagent_does_not_send_but_keeps_its_own_mailbox(store):
    """Maintainer decision 2026-09-30: a subagent asks its parent to send.
    Its own mail, status and park stay its own."""
    parent = codex_agent(store, PARENT_THREAD)
    child = codex_agent(store, CHILD_THREAD, parent_thread=PARENT_THREAD)
    orphan = codex_agent(store, "01a0ec35-ffff-7651-a945-81ed1f6cb638",
                         parent_thread="01a0ec35-eeee-7651-a945-81ed1f6cb638")
    peer = store.register("alice")
    for sender in (child, orphan):  # linked, and a parent not registered yet
        with pytest.raises(CoordinationError, match="child_send_refused") as refused:
            store.send(*creds(sender), to=peer["agent_id"], text="hello", request_id="r1")
        assert "ask your parent session" in refused.value.detail
        with pytest.raises(CoordinationError, match="child_send_refused"):
            store.send(*creds(sender), to="all", text="hello all", request_id="r2")
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_messages").fetchone()[0] == 0
    # The parent and an unlinked peer still send, including to the child.
    store.send(*creds(parent), to=child["agent_id"], text="status?", request_id="p1")
    store.send(*creds(peer), to=child["agent_id"], text="from a peer", request_id="q1")
    store.send(*creds(peer), to=parent["agent_id"], text="to the parent", request_id="q2")
    got = store.receive(*creds(child))["messages"]
    assert [m["text"] for m in got] == ["status?", "from a peer"]
    store.ack(*creds(child), message_id=got[0]["message_id"])
    out = store.update(*creds(child), status="reviewing", park_reason="waiting_peer")
    assert out["status"] == "reviewing" and out["park_reason"] == "waiting_peer"


def _hook_children(row):
    return [(c["label"], c.get("agent_id")) for c in row["children"]]


def test_hook_children_are_added_and_removed_by_their_agent_id(store):
    a, b = pair(store)
    started = store.subagent_started(a["agent_id"], "alice", child="a698026ca4ba524e9",
                                     kind="general-purpose")
    assert started["recorded"] is True
    assert _hook_children(store.authenticate(*creds(a))) == [
        ("general-purpose#a698026c", "a698026ca4ba524e9")]
    # Idempotent: a repeated start neither duplicates nor re-stamps it.
    store.test_time[0] = 1010.0
    store.subagent_started(a["agent_id"], "alice", child="a698026ca4ba524e9", kind="Explore")
    [entry] = store.list_agents(*creds(b))["agents"][0]["children"]
    assert entry["since"] == 1000.0 and entry["label"] == "general-purpose#a698026c"
    # A type-less start still gets a label.
    store.subagent_started(a["agent_id"], "alice", child="b1", kind="")
    assert _hook_children(store.authenticate(*creds(a)))[1] == ("subagent#b1", "b1")
    assert store.subagent_stopped(a["agent_id"], "alice", child="a698026ca4ba524e9")["recorded"]
    assert _hook_children(store.authenticate(*creds(a))) == [("subagent#b1", "b1")]
    # Stopping one that is not listed changes nothing and logs nothing.
    events = store.storage.conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0]
    assert store.subagent_stopped(a["agent_id"], "alice", child="gone")["recorded"] is False
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_events").fetchone()[0] == events
    # Another principal's address, or none, is not touched.
    assert store.subagent_started(a["agent_id"], "bob", child="x1", kind="t")["recorded"] is False
    assert store.subagent_started("f" * 32, "alice", child="x1", kind="t")["recorded"] is False
    assert _hook_children(store.authenticate(*creds(a))) == [("subagent#b1", "b1")]


@pytest.mark.parametrize("child,kind", [
    ("", "t"), ("a" * 65, "t"), ("a/b", "t"), ("a b", "t"), (7, "t"), ("ok", "x" * 65),
    ("ok", "bad type"), ("ok", "line\nbreak"), ("ok", 7),
])
def test_hook_children_are_validated(store, child, kind):
    a = store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_children"):
        store.subagent_started(a["agent_id"], "alice", child=child, kind=kind)
    assert store.authenticate(*creds(a))["children"] == []


def test_a_long_subagent_type_is_cut_to_fit_the_label(store):
    a = store.register("alice")
    store.subagent_started(a["agent_id"], "alice", child="c0ffee00c0ffee00c", kind="k" * 64)
    [(label, _)] = _hook_children(store.authenticate(*creds(a)))
    assert label == "k" * 31 + "#c0ffee00" and len(label) == 40


def test_parent_labels_and_hook_children_do_not_drop_each_other(store):
    """The parent's replace-all update rewrites only its own labels; a hook
    adds or removes only its own entry."""
    a = store.register("alice")
    store.update(*creds(a), children=["review storage"])
    store.subagent_started(a["agent_id"], "alice", child="c1", kind="Explore")
    out = store.update(*creds(a), children=["docs"])
    assert _hook_children(out) == [("docs", None), ("Explore#c1", "c1")]
    out = store.update(*creds(a), children=[])
    assert _hook_children(out) == [("Explore#c1", "c1")]
    store.update(*creds(a), children=["tests"])
    store.subagent_stopped(a["agent_id"], "alice", child="c1")
    assert _hook_children(store.authenticate(*creds(a))) == [("tests", None)]
    # A parent label spelled like a live hook entry names that same child.
    store.subagent_started(a["agent_id"], "alice", child="c2", kind="Plan")
    out = store.update(*creds(a), children=["Plan#c2", "tests"])
    assert _hook_children(out) == [("tests", None), ("Plan#c2", "c2")]


def test_hook_children_have_a_cap_of_their_own(store):
    """Hook entries never count against the parent's eight labels, so a
    parent update is never refused because of them (PR #481 review). They
    have their own eight: past it a new start replaces the oldest hook entry
    (whose stop was likely missed), never a parent label."""
    a = store.register("alice")
    parent_labels = [f"p{i}" for i in range(8)]
    store.update(*creds(a), children=parent_labels)
    for i in range(8):
        store.test_time[0] = 1000.0 + i
        store.subagent_started(a["agent_id"], "alice", child=f"h{i}", kind="t")
    # Eight parent labels beside eight live hook entries: accepted.
    out = store.update(*creds(a), status="orchestrating", children=[f"q{i}" for i in range(8)])
    assert out["status"] == "orchestrating"
    assert [c["label"] for c in out["children"]][:8] == [f"q{i}" for i in range(8)]
    assert len(out["children"]) == 16
    store.test_time[0] = 1100.0
    store.subagent_started(a["agent_id"], "alice", child="h8", kind="t")
    children = store.authenticate(*creds(a))["children"]
    hooked = [c["agent_id"] for c in children if c.get("agent_id")]
    assert len(hooked) == 8 and "h0" not in hooked and hooked[-1] == "h8"
    assert [c["label"] for c in children if not c.get("agent_id")] == [f"q{i}" for i in range(8)]


def test_a_refused_children_update_applies_nothing_and_says_so(store):
    """The update is one transaction: a refused ``children`` leaves the
    status and park sent beside it unapplied, and the detail says so."""
    a = store.register("alice")
    store.update(*creds(a), status="working", children=["kept"])
    for children in ([f"c{i}" for i in range(9)], ["dup", "dup"], ["y" * 41]):
        with pytest.raises(CoordinationError, match="invalid_children") as refused:
            store.update(*creds(a), status="parked now", park_reason="blocked",
                         park_needs="a review", children=children)
        assert "nothing was updated" in refused.value.detail
    row = store.authenticate(*creds(a))
    assert (row["status"], row["park_reason"], _children(row)) == ("working", None,
                                                                  [("kept", 1000.0)])


def test_a_hook_child_whose_stop_was_missed_ages_out(store):
    """A killed session never sends SubagentStop: past HOOK_CHILD_TTL the
    entry is no longer listed, and the next write drops it from the row."""
    from pseudolife_memory.storage.coordination import HOOK_CHILD_TTL
    a, b = pair(store)
    store.update(*creds(a), children=["review"])
    store.subagent_started(a["agent_id"], "alice", child="old", kind="t")
    store.test_time[0] = 1000.0 + HOOK_CHILD_TTL - 1
    store.subagent_started(a["agent_id"], "alice", child="new", kind="t")
    peer_view = next(r for r in store.list_agents(*creds(b))["agents"]
                     if r["agent_id"] == a["agent_id"])
    assert _hook_children(peer_view) == [("review", None), ("t#old", "old"), ("t#new", "new")]
    store.test_time[0] = 1000.0 + HOOK_CHILD_TTL
    assert _hook_children(store.authenticate(*creds(a))) == [("review", None), ("t#new", "new")]
    # Stopping the aged-out child is not a change; the next write drops it.
    assert store.subagent_stopped(a["agent_id"], "alice", child="old")["recorded"] is False
    store.update(*creds(a), children=["review"])
    stored = store.storage.conn.execute("SELECT children FROM coordination_agents "
                                        "WHERE agent_id=%s", (a["agent_id"],)).fetchone()[0]
    assert [c.get("agent_id") for c in stored] == [None, "new"]


def test_a_detach_clears_the_hook_children_and_keeps_the_parent_labels(store):
    """A detach is the shim ending with its session, and every subagent the
    hooks listed under it; the change is logged by the daemon."""
    a = store.register("alice")
    attached = store.attach(*creds(a), attachment_id="one")
    store.update(*creds(a), children=["review"])
    store.subagent_started(a["agent_id"], "alice", child="c1", kind="Explore")
    store.detach(*creds(a), attachment_id="one", generation=attached["generation"])
    assert _hook_children(store.authenticate(*creds(a))) == [("review", None)]
    actors = [actor for (actor,) in store.storage.conn.execute(
        "SELECT actor FROM coordination_events WHERE event='update' AND agent_id=%s "
        "ORDER BY seq", (a["agent_id"],)).fetchall()]
    assert actors == ["agent", "hook", "daemon"]
    # A detach with no hook entries writes no update.
    again = store.attach(*creds(a), attachment_id="two")
    store.detach(*creds(a), attachment_id="two", generation=again["generation"])
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_events WHERE event='update' AND agent_id=%s",
        (a["agent_id"],)).fetchone()[0] == 3


# --- park records (schema v49) ---------------------------------------------

def _park(row):
    return {key: row[key] for key in ("park_reason", "park_needs", "park_clear_by",
                                      "park_resume", "park_expires", "park_set_at")}


def test_a_park_record_is_set_kept_cleared_and_listed(store):
    """A session parks with why it stopped and what clears it; omitted fields
    stay, ``park_reason=None`` clears the record, and a plain status update
    while parked clears it too (a session that is working is not parked)."""
    a, b = pair(store)
    assert _park(store.authenticate(*creds(a))) == {
        "park_reason": None, "park_needs": "", "park_clear_by": "", "park_resume": "",
        "park_expires": None, "park_set_at": None}
    out = store.update(*creds(a), status="parked: waiting on GPU", park_reason="needs_resource",
                       park_needs="GPU free", park_clear_by="anyone",
                       park_resume="rerun the bench", park_expires=5000.0)
    assert _park(out) == {"park_reason": "needs_resource", "park_needs": "GPU free",
                          "park_clear_by": "anyone", "park_resume": "rerun the bench",
                          "park_expires": 5000.0, "park_set_at": 1000.0}
    listed = store.list_agents(*creds(b))["agents"][0]
    assert _park(listed) == _park(out)
    store.test_time[0] = 1010.0
    # A park update that names only the reason keeps the rest.
    out = store.update(*creds(a), park_reason="blocked")
    assert _park(out) == {**_park(listed), "park_reason": "blocked", "park_set_at": 1010.0}
    # Fields alone, while parked, refine the record and re-stamp it.
    store.test_time[0] = 1020.0
    out = store.update(*creds(a), park_needs="GPU free for 20 min")
    assert (out["park_needs"], out["park_set_at"]) == ("GPU free for 20 min", 1020.0)
    # Working again: a status without park fields clears the park.
    out = store.update(*creds(a), status="benchmarking")
    assert _park(out) == {"park_reason": None, "park_needs": "", "park_clear_by": "",
                          "park_resume": "", "park_expires": None, "park_set_at": None}
    store.update(*creds(a), park_reason="done", park_resume="nothing")
    assert store.authenticate(*creds(a))["park_reason"] == "done"
    # An explicit null clears it without touching the status.
    out = store.update(*creds(a), park_reason=None)
    assert out["park_reason"] is None and out["status"] == "benchmarking"
    # A task or children update while parked leaves the park alone.
    store.update(*creds(a), park_reason="waiting_peer", park_clear_by=b["agent_id"])
    out = store.update(*creds(a), task="t2", children=["review"])
    assert out["park_reason"] == "waiting_peer"


def test_park_fields_without_a_reason_need_a_park_to_refine(store):
    a = store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_park"):
        store.update(*creds(a), park_needs="GPU free")
    assert store.authenticate(*creds(a))["park_reason"] is None


@pytest.mark.parametrize("fields", [
    {"park_reason": "sleeping"}, {"park_reason": 7},
    {"park_reason": "blocked", "park_needs": "x" * 121},
    {"park_reason": "blocked", "park_resume": "x" * 241},
    {"park_reason": "blocked", "park_clear_by": "x" * 121},
    {"park_reason": "blocked", "park_expires": "soon"},
    {"park_reason": "blocked", "park_expires": -1},
    {"park_reason": "blocked", "park_expires": True},
    {"park_reason": "blocked", "park_needs": 5},
])
def test_park_records_are_bounded(store, fields):
    a = store.register("alice")
    store.update(*creds(a), park_reason="blocked", park_needs="kept")
    with pytest.raises(CoordinationError, match="invalid_park"):
        store.update(*creds(a), **fields)
    assert store.authenticate(*creds(a))["park_needs"] == "kept"


def test_credential_shaped_park_text_is_refused(store):
    a = store.register("alice")
    shaped = "gh" + "p_" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2"
    for field in ("park_needs", "park_resume", "park_clear_by"):
        with pytest.raises(CoordinationError, match="secret_like_body"):
            store.update(*creds(a), park_reason="blocked", **{field: shaped})


def test_a_park_update_is_logged_with_what_it_replaced(store):
    """The log records the park fields and what they replaced, as it does
    for the status, so the board's history says when a session parked."""
    import json
    a, _ = pair(store)
    store.update(*creds(a), park_reason="needs_info", park_needs="which judge")
    row = store.storage.conn.execute(
        "SELECT payload FROM coordination_events WHERE event='update' ORDER BY seq DESC LIMIT 1"
    ).fetchone()[0]
    payload = json.loads(row)
    assert payload["fields"]["park_reason"] == "needs_info"
    assert payload["fields"]["park_needs"] == "which judge"
    assert payload["before"]["park_reason"] is None


# --- the wake decision at send ---------------------------------------------

def _wake(store, sender, recipient, text="note", request_id=None, **kw):
    _wake.n = getattr(_wake, "n", 0) + 1
    return store.send(*creds(sender, sender.get("principal", "alice")), to=recipient["agent_id"],
                      text=text, request_id=request_id or f"w{_wake.n}", **kw)["wake"]


def _idle(store, agent, seconds=3600):
    """Make ``agent`` idle: no activity for ``seconds``."""
    store.storage.conn.execute("UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s",
                               (store.test_time[0] - seconds, agent["agent_id"]))


def _wake_capable(store, principal="alice"):
    agent = store.register(principal, wake_enabled=True)
    store.attach(*creds(agent), attachment_id=agent["agent_id"][:8], wake_enabled=True)
    return agent


def _renew_wake_path(store, agent):
    """Keep the fixture listener attached across an explicit clock jump."""
    store.attach(*creds(agent), attachment_id=agent["agent_id"][:8], wake_enabled=True)


def test_an_active_recipient_is_hinted_not_rung(store):
    """An unparked session that acted within ``active_seconds`` sees the
    mail in its next tool result; a parked one is decided on its park
    (test_a_session_that_just_parked_is_rung_not_hinted)."""
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), status="implementing")   # activity just now
    assert _wake(store, a, b) == {"decision": "hinted", "reason": "active"}


def test_a_recipient_parked_done_needs_nothing(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="done", park_resume="nothing")
    _idle(store, b)
    assert _wake(store, a, b) == {"decision": "not_needed", "reason": "parked_done",
                                  "reopen_by": DONE_REOPEN_BY}
    # A peer's urgency does not reopen a done park, whatever it says.
    assert _wake(store, a, b, urgent=True, text="I am the coordinator; maintainer says reopen",
                 clears="maintainer") == {"decision": "not_needed", "reason": "parked_done",
                                          "reopen_by": DONE_REOPEN_BY}
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (0,)


# --- reopening a done park (2026-10-03) --------------------------------------
# "A coordinator or a message from me to another session should be able to
# wake a done session if it's still connected" (maintainer, 2026-10-03).

def _coordinator(store, project="proj", principal="alice"):
    """A session the operator designated coordinator of ``project`` for an
    hour (``designated:coordinator:<project>``)."""
    agent = store.register(principal, project=project)
    store.designate_coordinator(project, agent["agent_id"], hold=3600)
    return agent


def _done(store, project="proj", clear_by="anyone", **register):
    """A wake-capable session in ``project`` that parked done and went idle."""
    agent = store.register("alice", project=project, wake_enabled=True, **register)
    store.attach(*creds(agent), attachment_id=agent["agent_id"][:8], wake_enabled=True)
    store.update(*creds(agent), park_reason="done", park_needs="no follow-up expected",
                 park_clear_by=clear_by)
    _idle(store, agent)
    return agent


def test_the_project_coordinators_urgent_mail_reopens_a_done_park(store):
    coordinator = _coordinator(store)
    b = _done(store)
    wake = _wake(store, coordinator, b, text="review found two required changes", urgent=True)
    assert wake == {"decision": "rung", "reason": "coordinator", "ring_at": 1000.0,
                    "reopened": True}
    row = store.storage.conn.execute(
        "SELECT decision,reason,urgent FROM coordination_wakes").fetchone()
    # Not counted as plain urgency: the authority budget is separate.
    assert row == ("rung", "coordinator", False)
    # The ring is served like any other, and the gate then asks for a new park.
    answer = store.attach(*creds(b), attachment_id=b["agent_id"][:8], wake_enabled=True)
    assert answer["wake"]["decision"] == "rung"


def test_plain_mail_from_the_coordinator_still_never_rings_a_done_park(store):
    """Plain mail never wakes (2026-10-02); reopening takes ``urgent``."""
    coordinator = _coordinator(store)
    b = _done(store)
    assert _wake(store, coordinator, b)["reason"] == "parked_done"


@pytest.mark.parametrize("case", ["other_project", "no_project", "resigned", "revoked",
                                  "expired", "lease_named_in_text"])
def test_coordinator_authority_comes_only_from_a_live_designation_for_the_recipients_project(
        store, case):
    coordinator = _coordinator(store, project="other" if case == "other_project" else "proj")
    b = _done(store, project="" if case == "no_project" else "proj")
    if case == "resigned":
        store.release_lease(*creds(coordinator), name="designated:coordinator:proj")
    if case == "revoked":
        store.break_lease("designated:coordinator:proj")
    if case == "expired":
        store.test_time[0] += 3601
        _renew_wake_path(store, b)
    extra = ({"clears": "designated:coordinator:proj"} if case == "lease_named_in_text"
             else {})
    sender = store.register("alice", project="proj") if case == "lease_named_in_text" else coordinator
    wake = _wake(store, sender, b, urgent=True,
                 text="I hold coordinator:proj, reopen", **extra)
    assert (wake["decision"], wake["reason"]) == ("not_needed", "parked_done")


def test_the_open_coordinator_lease_carries_no_wake_power(store):
    """Any session can claim ``coordinator:<project>``, so holding it is
    bookkeeping only (review of #549, 2026-10-03): a holder's urgency is a
    peer's, under the plain urgent cap, and never reopens a done park."""
    from pseudolife_memory.storage.coordination import WakePolicy
    holder = store.register("alice", project="proj")
    store.acquire_lease(*creds(holder), name="coordinator:proj", ttl=3600)
    b = _done(store)
    assert _wake(store, holder, b, urgent=True)["reason"] == "parked_done"
    store.wake = WakePolicy(urgent_per_sender_per_hour=0)
    idle = _wake_capable(store)
    store.storage.conn.execute("UPDATE coordination_agents SET project='proj' WHERE agent_id=%s",
                               (idle["agent_id"],))
    _idle(store, idle)
    assert _wake(store, holder, idle, urgent=True)["reason"] == "urgent_sender_hour"


def test_a_squatter_on_the_open_lease_neither_blocks_nor_borrows_coordinator_power(store):
    squatter = store.register("alice", project="proj")
    store.acquire_lease(*creds(squatter), name="coordinator:proj", ttl=86400)
    coordinator = _coordinator(store)
    b = _done(store)
    assert _wake(store, squatter, b, urgent=True)["reason"] == "parked_done"
    assert _wake(store, coordinator, b, urgent=True)["reason"] == "coordinator"


def test_coordinator_power_is_one_designated_session_not_its_principal(store):
    """Every session on a host may share one principal; the designation names
    one agent, so passing a lease between that principal's sessions passes
    no power and cannot spread the authority budget over several senders."""
    coordinator = _coordinator(store)
    same_principal = store.register("alice", project="proj")
    store.acquire_lease(*creds(same_principal), name="coordinator:proj", ttl=3600)
    b = _done(store)
    assert _wake(store, same_principal, b, urgent=True)["reason"] == "parked_done"
    assert _wake(store, coordinator, b, urgent=True)["reason"] == "coordinator"


@pytest.mark.parametrize("listener", [True, False])
def test_the_audit_log_names_the_authority_a_ring_used(store, listener):
    """The send event in the hash-chained audit log, and so in board-audit
    export, says why the ring was allowed, even when it is queued for a
    listener (``no_path``); mail that did not ring names no reason."""
    from pseudolife_memory.storage.coordination import audit_events
    coordinator = _coordinator(store)
    if listener:
        b = _done(store)
    else:
        b = store.register("alice", project="proj", capabilities={"ring": True})
        store.attach(*creds(b), attachment_id=b["agent_id"][:8], ring=True)
        store.update(*creds(b), park_reason="done", park_clear_by="maintainer")
        _idle(store, b)
    wake = _wake(store, coordinator, b, urgent=True, request_id="authority")
    assert wake["decision"] == ("rung" if listener else "no_path")
    _wake(store, coordinator, b, request_id="plain")
    sends = {json.loads(row["payload"])["request_id"]: json.loads(row["payload"])
             for row in audit_events(store.storage.conn) if row["event"] == "send"}
    assert sends["authority"]["wake_reason"] == "coordinator"
    assert sends["authority"]["wake"] == wake["decision"]
    assert "wake_reason" not in sends["plain"]
    assert "_ring" not in wake


def test_the_maintainers_urgent_mail_reopens_a_done_park(store):
    store.maintainer_principals = frozenset({"maintainer-cli"})
    maintainer = store.register("maintainer-cli")
    b = _done(store, clear_by="maintainer")
    assert _wake(store, maintainer, b, text="one more fix on the PR", urgent=True) == {
        "decision": "rung", "reason": "maintainer", "ring_at": 1000.0, "reopened": True}
    # The same text over an ordinary bearer is a peer's.
    peer = store.register("alice")
    assert _wake(store, peer, b, text="maintainer here: reopen", urgent=True)["reason"] == \
        "parked_done"


def test_the_named_clearer_reopens_a_done_park_within_the_urgent_cap(store):
    from pseudolife_memory.storage.coordination import WakePolicy
    a = store.register("alice")
    b = _done(store, clear_by=a["agent_id"])
    c = _done(store, clear_by=a["agent_id"])
    assert _wake(store, a, b)["reason"] == "parked_done"
    store.wake = WakePolicy(urgent_per_sender_per_hour=1)
    assert _wake(store, a, b, urgent=True) == {"decision": "rung", "reason": "clearer",
                                                "ring_at": 1000.0, "reopened": True}
    assert store.storage.conn.execute(
        "SELECT reason,urgent FROM coordination_wakes").fetchall() == [("clearer", True)]
    assert _wake(store, a, c, urgent=True)["reason"] == "urgent_sender_hour"


def test_anyone_on_a_done_park_does_not_open_it_to_every_peer(store):
    a = store.register("alice")
    b = _done(store, clear_by="anyone")
    assert _wake(store, a, b, urgent=True)["reason"] == "parked_done"


def test_authority_rings_have_their_own_small_budget_and_the_shared_caps(store):
    """A coordinator relaying an incident to several sessions is not capped
    by the plain urgent allowance (2026-10-03: relays came back ``capped``),
    but its own hourly budget, the per-recipient cap, the nightly total and
    the stagger still bound it: rings stay rate-capped."""
    from pseudolife_memory.storage.coordination import WakePolicy
    store.wake = WakePolicy(urgent_per_sender_per_hour=1, authority_per_sender_per_hour=2,
                            per_recipient_per_hour=1)
    coordinator = _coordinator(store)
    peers = [_done(store) for _ in range(3)]
    first = _wake(store, coordinator, peers[0], urgent=True)
    second = _wake(store, coordinator, peers[1], urgent=True)
    assert (first["reason"], second["reason"]) == ("coordinator", "coordinator")
    assert (first["ring_at"], second["ring_at"]) == (1000.0, 1030.0)
    assert _wake(store, coordinator, peers[2], urgent=True) == {
        "decision": "capped", "reason": "authority_sender_hour"}
    store.test_time[0] += 3601
    for peer in peers:
        _renew_wake_path(store, peer)
        store.update(*creds(peer), park_reason="done", park_clear_by="anyone")
        _idle(store, peer, 120)
    store.designate_coordinator("proj", coordinator["agent_id"], hold=3600)
    assert _wake(store, coordinator, peers[2], urgent=True)["reason"] == "coordinator"
    assert _wake(store, coordinator, peers[2], urgent=True) == {
        "decision": "capped", "reason": "recipient_hour"}


def test_authority_on_a_parked_or_unparked_session_spends_the_authority_budget(store):
    from pseudolife_memory.storage.coordination import WakePolicy
    store.wake = WakePolicy(urgent_per_sender_per_hour=0)
    coordinator = _coordinator(store)
    parked = _wake_capable(store)
    store.storage.conn.execute("UPDATE coordination_agents SET project='proj' WHERE agent_id=%s",
                               (parked["agent_id"],))
    store.update(*creds(parked), park_reason="waiting_peer", park_needs="a verdict",
                 park_clear_by="someone-else")
    _idle(store, parked)
    assert _wake(store, coordinator, parked, urgent=True) == {
        "decision": "rung", "reason": "coordinator", "ring_at": 1000.0}
    idle = _wake_capable(store)
    store.storage.conn.execute("UPDATE coordination_agents SET project='proj' WHERE agent_id=%s",
                               (idle["agent_id"],))
    _idle(store, idle)
    assert _wake(store, coordinator, idle, urgent=True)["reason"] == "coordinator"
    # A peer without authority is still under the plain urgent cap.
    peer = store.register("alice")
    assert _wake(store, peer, idle, urgent=True)["reason"] == "urgent_sender_hour"


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_reopening_a_done_park_without_a_listener_says_no_path_and_names_the_fallback(store, client):
    coordinator = _coordinator(store)
    capabilities = {"codex": False, "ring": True} if client == "codex" else {"ring": True}
    b = store.register("alice", project="proj", capabilities=capabilities)
    store.attach(*creds(b), attachment_id=b["agent_id"][:8], ring=True)
    store.update(*creds(b), park_reason="done", park_clear_by="maintainer")
    _idle(store, b)
    wake = _wake(store, coordinator, b, urgent=True)
    assert (wake["decision"], wake["reason"], wake["queued"], wake["reopened"]) == (
        "no_path", "listener_unknown", True, True)
    expected = (["codex_doorbell", "maintainer_types"] if client == "codex"
                else ["claude_desktop_send_message", "maintainer_types"])
    assert wake["fallback_paths"] == expected
    assert ("doorbell" in wake["fallback"]) is (client == "codex")
    assert ("send_message" in wake["fallback"]) is (client == "claude")


def test_withheld_mail_says_what_would_ring_the_park(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="waiting_peer", park_needs="the verdict on #548",
                 park_clear_by="c" * 32)
    _idle(store, b)
    wake = _wake(store, a, b)
    assert wake["decision"] == "withheld"
    assert "urgent" in wake["retry"] and "clears" in wake["retry"]


def test_codex_serves_a_reopen_of_its_current_done_park_and_nothing_older(store):
    coordinator = _coordinator(store)
    b = store.register("alice", project="proj", capabilities={"codex": False, "ring": True})
    attached = store.attach(*creds(b), attachment_id="codex", ring=True, ring_armed_until=1060.0)
    store.update(*creds(b), park_reason="blocked", park_needs="the review",
                 park_clear_by="anyone")
    _idle(store, b)
    older = _wake(store, store.register("alice"), b)
    assert older["reason"] == "anyone"
    store.test_time[0] = 1001.0
    store.update(*creds(b), park_reason="done", park_clear_by="anyone")
    _idle(store, b, 120)
    answer = store.heartbeat(*creds(b), attachment_id="codex",
                             generation=attached["generation"], ring_armed_until=1061.0)
    assert answer["wake"] is None
    store.test_time[0] = 1002.0
    _idle(store, b, 120)
    reopen = _wake(store, coordinator, b, urgent=True, text="two required changes")
    assert (reopen["decision"], reopen["reason"]) == ("rung", "coordinator")
    answer = store.heartbeat(*creds(b), attachment_id="codex",
                             generation=attached["generation"], ring_armed_until=1062.0)
    assert (answer["wake"]["decision"], answer["wake"]["reason"]) == ("rung", "coordinator")


def test_a_recipient_without_a_wake_path_reports_no_path_and_its_need(store):
    a = store.register("alice")
    b = store.register("alice")   # pull-only
    store.update(*creds(b), park_reason="blocked", park_needs="a review", park_clear_by="anyone")
    _idle(store, b)
    wake = _wake(store, a, b)
    assert {key: wake[key] for key in ("decision", "reason", "park_needs", "park_clear_by")} == {
        "decision": "no_path", "reason": "wake_disabled",
        "park_needs": "a review", "park_clear_by": "anyone"}
    assert wake["queued"] is True


def test_chatter_to_a_parked_session_is_withheld_with_the_need(store):
    a = store.register("alice")
    b = _wake_capable(store)
    c = store.register("alice")
    store.update(*creds(b), park_reason="waiting_peer", park_needs="the merge of #425",
                 park_clear_by=c["agent_id"])
    _idle(store, b)
    receipt = store.send(*creds(a), to=b["agent_id"], text="fyi", request_id="chatter")
    assert receipt["state"] == "queued"
    assert receipt["wake"] == {"decision": "withheld", "reason": "need_not_cleared",
                               "park_needs": "the merge of #425",
                               "park_clear_by": c["agent_id"],
                               "retry": WITHHELD_RETRY}
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (0,)


@pytest.mark.parametrize("how", ["clearer", "anyone", "clears", "urgent"])
def test_mail_that_clears_a_parked_need_rings(store, how):
    a = store.register("alice")
    b = _wake_capable(store)
    clear_by = a["agent_id"] if how == "clearer" else "anyone" if how == "anyone" else "maintainer"
    store.update(*creds(b), park_reason="needs_approval", park_needs="Approve the deploy",
                 park_clear_by=clear_by)
    _idle(store, b)
    extra = ({"clears": "approve the DEPLOY"} if how == "clears"
             else {"urgent": True} if how == "urgent" else {})
    wake = _wake(store, a, b, **extra)
    assert wake == {"decision": "rung", "reason": how, "ring_at": 1000.0}
    rows = store.storage.conn.execute(
        "SELECT recipient_agent_id,sender_agent_id,decision,reason,urgent,ring_at "
        "FROM coordination_wakes").fetchall()
    assert rows == [(b["agent_id"], a["agent_id"], "rung", how, how == "urgent", 1000.0)]


def test_clears_must_match_the_need(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="needs_info", park_needs="which judge family",
                 park_clear_by="maintainer")
    _idle(store, b)
    assert _wake(store, a, b, clears="the GPU")["decision"] == "withheld"
    assert _wake(store, a, b, clears="judge family")["decision"] == "rung"


def test_an_expired_park_is_idle(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="maintainer",
                 park_expires=1500.0)
    _idle(store, b)
    store.test_time[0] = 1600.0
    _renew_wake_path(store, b)
    assert _wake(store, a, b) == {"decision": "not_needed", "reason": "no_park"}


def test_plain_mail_never_rings_an_idle_unparked_recipient(store):
    """Regular mail never wakes (maintainer decision 2026-10-02): a session
    that has not parked is waiting on nobody, so plain mail to it, however
    often, or naming a need it never declared, is ``not_needed`` and waits
    for its next turn, with or without a wake path. Nothing rings, nothing
    is capped, the attach and heartbeat answers carry no ring, and the live
    path skips the mail; an explicit receive still returns it. (``urgent``
    is the exception: test_urgent_mail_rings_an_idle_unparked_recipient.)"""
    a = store.register("alice")
    b = _wake_capable(store)
    pull_only = store.register("alice")
    for at, extra in ((1000.0, {}), (1000.0 + 1800, {}),
                      (1000.0 + 3601, {"clears": "the review"})):
        store.test_time[0] = at
        _renew_wake_path(store, b)
        for agent in (b, pull_only):
            _idle(store, agent)
            assert _wake(store, a, agent, **extra) == {"decision": "not_needed",
                                                       "reason": "no_park"}
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (0,)
    answer = store.attach(*creds(b), attachment_id=b["agent_id"][:8], wake_enabled=True)
    assert answer["wake"] is None
    assert store.heartbeat(*creds(b), attachment_id=b["agent_id"][:8],
                           generation=answer["generation"])["wake"] is None
    assert store.receive(*creds(b), for_delivery=True)["messages"] == []
    assert len(store.receive(*creds(b))["messages"]) == 3


def test_urgent_mail_rings_an_idle_unparked_recipient(store):
    """``urgent`` rings an idle session that has not parked (maintainer
    decision 2026-10-02) as a ring like any other: reason ``urgent``, an
    urgent coordination_wakes row, staggered within one sender's burst,
    served once through the recipient's attach and carried by the live
    path. Plain mail and ``clears`` beside it stay ``not_needed``."""
    a = store.register("alice")
    b, b2 = _wake_capable(store), _wake_capable(store)
    for agent in (b, b2):
        _idle(store, agent)
    assert _wake(store, a, b, clears="the review") == {"decision": "not_needed",
                                                       "reason": "no_park"}
    assert _wake(store, a, b, urgent=True) == {"decision": "rung", "reason": "urgent",
                                               "ring_at": 1000.0}
    assert _wake(store, a, b2, urgent=True) == {"decision": "rung", "reason": "urgent",
                                                "ring_at": 1030.0}
    assert _wake(store, a, b) == {"decision": "not_needed", "reason": "no_park"}
    rows = store.storage.conn.execute(
        "SELECT recipient_agent_id,decision,reason,urgent FROM coordination_wakes "
        "ORDER BY ring_at").fetchall()
    assert rows == [(b["agent_id"], "rung", "urgent", True),
                    (b2["agent_id"], "rung", "urgent", True)]
    answer = store.attach(*creds(b), attachment_id=b["agent_id"][:8], wake_enabled=True)
    assert answer["wake"] == {"decision": "rung", "reason": "urgent", "ring_at": 1000.0}
    store.test_time[0] += 30   # past the one-heartbeat repeat window
    assert store.heartbeat(*creds(b), attachment_id=b["agent_id"][:8],
                           generation=answer["generation"])["wake"] is None
    assert len(store.receive(*creds(b), for_delivery=True)["messages"]) == 1
    assert len(store.receive(*creds(b))["messages"]) == 3


def test_urgent_to_an_unparked_recipient_keeps_every_cap(store):
    """The urgent ring to an unparked session spends the sender's urgent
    allowance (6 an hour, shared with urgent rings to parked sessions) and
    counts against the recipient's hourly cap like any ring. An active
    recipient is hinted, urgent or not."""
    from pseudolife_memory.storage.coordination import WakePolicy
    a = store.register("alice")
    b = _wake_capable(store)
    for i in range(6):
        store.test_time[0] = 1000.0 + i * 120
        _renew_wake_path(store, b)
        _idle(store, b)
        assert _wake(store, a, b, urgent=True)["decision"] == "rung"
    store.test_time[0] = 1000.0 + 6 * 120
    _renew_wake_path(store, b)
    _idle(store, b)
    assert _wake(store, a, b, urgent=True) == {"decision": "capped",
                                               "reason": "urgent_sender_hour"}
    assert _wake(store, a, b) == {"decision": "not_needed", "reason": "no_park"}
    c, d = store.register("alice"), store.register("alice")
    assert _wake(store, c, b, urgent=True)["decision"] == "rung"
    store.wake = WakePolicy(per_recipient_per_hour=7)
    assert _wake(store, d, b, urgent=True) == {"decision": "capped", "reason": "recipient_hour"}
    store.update(*creds(b), status="implementing")   # active again
    assert _wake(store, d, b, urgent=True) == {"decision": "hinted", "reason": "active"}


@pytest.mark.parametrize("served_at", [None, 990.0])
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("decision", ["rung", "nudged"])
def test_a_nudge_decided_before_the_change_never_rings(store, decision, queued, served_at):
    """A ``nudged`` ring still in coordination_wakes when the daemon is
    updated (decided before 2026-10-02, immediate or queued behind a
    ``no_path`` receipt, served already or not) is history, not a ring:
    the recipient's attach never serves or re-offers it and the live path
    skips its mail, which an explicit receive still returns. The same row
    decided ``rung`` is served."""
    a = store.register("alice")
    b = _wake_capable(store)
    _idle(store, b)
    sent = store.send(*creds(a), to=b["agent_id"], text="note", request_id="before")
    reason = "anyone" if decision == "rung" else "no_park"
    wake = ({"decision": "no_path", "reason": "listener_expired", "queued": True} if queued
            else {"decision": decision, "reason": reason, "ring_at": 1000.0})
    store.storage.conn.execute("UPDATE coordination_messages SET wake=%s::jsonb "
                               "WHERE message_id=%s", (json.dumps(wake), sent["message_id"]))
    store.storage.conn.execute(
        "INSERT INTO coordination_wakes (recipient_agent_id,sender_agent_id,message_id,"
        "decision,reason,ring_at,created_at,served_at) VALUES (%s,%s,%s,%s,%s,1000.0,1000.0,%s)",
        (b["agent_id"], a["agent_id"], sent["message_id"], decision, reason, served_at))
    answer = store.attach(*creds(b), attachment_id=b["agent_id"][:8], wake_enabled=True)
    delivered = [m["message_id"] for m in store.receive(*creds(b), for_delivery=True)["messages"]]
    served = store.storage.conn.execute("SELECT served_at FROM coordination_wakes").fetchone()[0]
    if decision == "rung":
        assert answer["wake"] == {"decision": "rung", "reason": "anyone", "ring_at": 1000.0}
        assert (delivered, served) == ([sent["message_id"]], served_at or 1000.0)
    else:
        assert answer["wake"] is None
        assert (delivered, served) == ([], served_at)
    assert [m["message_id"] for m in store.receive(*creds(b))["messages"]] == [sent["message_id"]]


def test_rings_to_one_recipient_are_capped_per_hour(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="anyone")
    for i in range(20):
        store.test_time[0] = 1000.0 + i * 60
        _renew_wake_path(store, b)
        _idle(store, b)
        assert _wake(store, a, b)["decision"] == "rung"
    store.test_time[0] = 1000.0 + 20 * 60
    _renew_wake_path(store, b)
    _idle(store, b)
    assert _wake(store, a, b) == {"decision": "capped", "reason": "recipient_hour",
                                  "park_needs": "x", "park_clear_by": "anyone"}
    store.test_time[0] = 1000.0 + 3601
    _renew_wake_path(store, b)
    _idle(store, b)
    assert _wake(store, a, b)["decision"] == "rung"


def test_urgent_is_capped_per_sender(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="maintainer")
    for i in range(6):
        store.test_time[0] = 1000.0 + i * 120
        _renew_wake_path(store, b)
        _idle(store, b)
        assert _wake(store, a, b, urgent=True)["decision"] == "rung"
    store.test_time[0] = 1000.0 + 6 * 120
    _renew_wake_path(store, b)
    _idle(store, b)
    assert _wake(store, a, b, urgent=True) == {"decision": "capped", "reason": "urgent_sender_hour",
                                               "park_needs": "x", "park_clear_by": "maintainer"}
    # Another sender's urgency still counts.
    c = store.register("alice")
    _idle(store, b)
    assert _wake(store, c, b, urgent=True)["decision"] == "rung"


def test_rings_have_a_nightly_total(store):
    from pseudolife_memory.storage.coordination import WakePolicy
    store.wake = WakePolicy(nightly_total=2)
    a = store.register("alice")
    b, c, d = (_wake_capable(store) for _ in range(3))
    for agent in (b, c, d):
        # Past the default expiry: the test runs a day on.
        store.update(*creds(agent), park_reason="blocked", park_needs="x", park_clear_by="anyone",
                     park_expires=1000.0 + 3 * 86400)
        _idle(store, agent)
    assert _wake(store, a, b)["decision"] == "rung"
    assert _wake(store, a, c)["decision"] == "rung"
    assert _wake(store, a, d) == {"decision": "capped", "reason": "nightly",
                                  "park_needs": "x", "park_clear_by": "anyone"}
    store.test_time[0] = 1000.0 + 86401
    _renew_wake_path(store, d)
    _idle(store, d)
    assert _wake(store, a, d)["decision"] == "rung"


def test_a_fan_out_burst_staggers_its_rings(store):
    a = store.register("alice")
    peers = [_wake_capable(store) for _ in range(3)]
    for agent in peers:
        store.update(*creds(agent), park_reason="blocked", park_needs="x", park_clear_by="anyone")
        _idle(store, agent)
    assert [_wake(store, a, agent)["ring_at"] for agent in peers] == [1000.0, 1030.0, 1060.0]
    # A ring a minute after the burst's last is not part of it.
    store.test_time[0] = 1000.0 + 120
    _renew_wake_path(store, peers[0])
    for agent in peers:
        _idle(store, agent)
    assert _wake(store, a, peers[0])["ring_at"] == 1120.0


def test_the_ring_reaches_the_recipient_once_through_its_heartbeat(store):
    """The daemon decides, the shim rings: the next attach or heartbeat of
    the recipient's adapter carries the decided ring once."""
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="anyone")
    generation = store.attach(*creds(b), attachment_id=b["agent_id"][:8],
                              wake_enabled=True)["generation"]
    beat = dict(attachment_id=b["agent_id"][:8], generation=generation)
    assert store.heartbeat(*creds(b), **beat)["wake"] is None
    _idle(store, b)
    _wake(store, a, b)
    assert store.heartbeat(*creds(b), **beat)["wake"] == {
        "decision": "rung", "reason": "anyone", "ring_at": 1000.0}
    store.test_time[0] += 30   # past the one-heartbeat repeat window
    assert store.heartbeat(*creds(b), **beat)["wake"] is None


def test_send_validates_clears_and_urgent(store):
    a = store.register("alice")
    b = _wake_capable(store)
    for bad in ({"clears": ""}, {"clears": "x" * 121}, {"clears": 4}):
        with pytest.raises(CoordinationError, match="invalid_clears"):
            store.send(*creds(a), to=b["agent_id"], text="t", request_id="c", **bad)
    with pytest.raises(CoordinationError, match="invalid_urgent"):
        store.send(*creds(a), to=b["agent_id"], text="t", request_id="u", urgent="yes")
    shaped = "gh" + "p_" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1Wx2"
    with pytest.raises(CoordinationError, match="secret_like_body"):
        store.send(*creds(a), to=b["agent_id"], text="t", request_id="s", clears=shaped)


def test_a_retried_send_repeats_its_wake_decision(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="anyone")
    _idle(store, b)
    first = store.send(*creds(a), to=b["agent_id"], text="t", request_id="again")
    again = store.send(*creds(a), to=b["agent_id"], text="t", request_id="again")
    assert first["wake"]["decision"] == "rung"
    assert again["wake"] == first["wake"]
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (1,)


# --- the Stop-hook park gate -------------------------------------------------

def test_park_gate_asks_an_unparked_session_once(store):
    """What the Stop hook asks at turn end: block when the session's row has
    no live park record and its status is not done-shaped, or when an
    unparked session did not update its status during the turn. A live
    standing park allows when no rung delivery invalidated it."""
    a = store.register("alice")

    def gate(since=None):
        return store.park_gate(a["agent_id"], "alice", since=since)

    assert gate() == {"gate": "block", "reason": "no_park"}
    store.update(*creds(a), status="implementing the gate")
    assert gate() == {"gate": "block", "reason": "no_park"}
    store.update(*creds(a), status="DONE: PR #440 open, maintainer merges")
    assert gate() == {"gate": "allow", "reason": "done"}
    store.update(*creds(a), status="working", park_reason="blocked", park_needs="x",
                 park_clear_by="anyone")
    assert gate() == {"gate": "allow", "reason": "parked"}
    # A live standing park needs no repeat when this turn received no ring.
    store.test_time[0] = 2000.0
    assert gate(since=1500.0) == {"gate": "allow", "reason": "parked"}
    store.update(*creds(a), park_reason="blocked")
    assert gate(since=1500.0) == {"gate": "allow", "reason": "parked"}
    # An expired park is no park.
    store.update(*creds(a), park_reason="blocked", park_expires=2500.0)
    store.test_time[0] = 3000.0
    assert gate() == {"gate": "block", "reason": "no_park"}
    # Unknown or foreign addresses are allowed: the hook has nothing to ask.
    assert store.park_gate("0" * 32, "alice") == {"gate": "allow", "reason": "unknown_agent"}
    assert store.park_gate(a["agent_id"], "bob") == {"gate": "allow", "reason": "unknown_agent"}


@pytest.mark.parametrize("reset_at", [None, 1600.0, 1700.0])
def test_park_gate_requires_a_park_strictly_after_a_rung_delivery(store, reset_at):
    sender = store.register("alice")
    recipient = _wake_capable(store)
    store.update(*creds(recipient), park_reason="blocked", park_needs="a review",
                 park_clear_by="anyone")
    # An update earlier in the turn must not excuse a later cleared need.
    store.test_time[0] = 1510.0
    _renew_wake_path(store, recipient)
    store.update(*creds(recipient), park_reason="blocked")
    store.test_time[0] = 1600.0
    _renew_wake_path(store, recipient)
    assert _wake(store, sender, recipient)["decision"] == "rung"
    if reset_at is not None:
        store.test_time[0] = reset_at
        _renew_wake_path(store, recipient)
        store.update(*creds(recipient), park_reason="blocked")
    expected = ({"gate": "allow", "reason": "parked"} if reset_at == 1700.0
                else {"gate": "block", "reason": "not_updated_this_turn"})
    assert store.park_gate(recipient["agent_id"], "alice", since=1500.0) == expected


@pytest.mark.parametrize("delivery_at", [1499.0, 1500.0])
def test_park_gate_ignores_rung_deliveries_before_or_at_the_turn_start(store, delivery_at):
    sender = store.register("alice")
    recipient = _wake_capable(store)
    store.update(*creds(recipient), park_reason="blocked", park_needs="a review",
                 park_clear_by="anyone")
    store.test_time[0] = delivery_at
    _renew_wake_path(store, recipient)
    assert _wake(store, sender, recipient)["decision"] == "rung"
    store.test_time[0] = 2000.0
    _renew_wake_path(store, recipient)
    assert store.park_gate(recipient["agent_id"], "alice", since=1500.0) == {
        "gate": "allow", "reason": "parked"}


def test_park_gate_blocks_a_null_park_timestamp_after_a_rung_delivery(store):
    sender, recipient = pair(store)
    store.update(*creds(recipient), park_reason="blocked", park_needs="a review",
                 park_clear_by="anyone")
    store.storage.conn.execute(
        "UPDATE coordination_agents SET park_set_at=NULL WHERE agent_id=%s",
        (recipient["agent_id"],))
    store.test_time[0] = 1600.0
    store.storage.conn.execute(
        "INSERT INTO coordination_wakes (recipient_agent_id,sender_agent_id,message_id,"
        "decision,reason,ring_at,created_at) VALUES (%s,%s,%s,'rung','anyone',%s,%s)",
        (recipient["agent_id"], sender["agent_id"], "fixture-message", 1600.0, 1600.0))
    assert store.park_gate(recipient["agent_id"], "alice", since=1500.0) == {
        "gate": "block", "reason": "not_updated_this_turn"}


def test_park_gate_ignores_rung_deliveries_to_another_agent(store):
    sender, recipient = (_wake_capable(store) for _ in range(2))
    for agent in (sender, recipient):
        store.update(*creds(agent), park_reason="blocked", park_needs="a review",
                     park_clear_by="anyone")
    store.test_time[0] = 1600.0
    _renew_wake_path(store, recipient)
    assert _wake(store, sender, recipient)["decision"] == "rung"
    assert store.park_gate(sender["agent_id"], "alice", since=1500.0) == {
        "gate": "allow", "reason": "parked"}


def test_park_gate_ignores_a_historical_nudge_when_the_recipient_then_parks(store):
    """Only a rung delivery makes a standing park stale. A ``nudged`` row
    decided before 2026-10-02 may still sit in coordination_wakes after the
    update; it never counts."""
    sender = store.register("alice")
    recipient = _wake_capable(store)
    store.test_time[0] = 1600.0
    _renew_wake_path(store, recipient)
    store.storage.conn.execute(
        "INSERT INTO coordination_wakes (recipient_agent_id,sender_agent_id,message_id,"
        "decision,reason,ring_at,created_at) VALUES (%s,%s,%s,'nudged','no_park',%s,%s)",
        (recipient["agent_id"], sender["agent_id"], "fixture-message", 1600.0, 1600.0))
    store.update(*creds(recipient), park_reason="blocked", park_needs="a review",
                 park_clear_by="anyone")
    assert store.park_gate(recipient["agent_id"], "alice", since=1500.0) == {
        "gate": "allow", "reason": "parked"}


def test_park_gate_compares_the_park_with_the_newest_rung_delivery(store):
    sender = store.register("alice")
    recipient = _wake_capable(store)
    store.update(*creds(recipient), park_reason="blocked", park_needs="a review",
                 park_clear_by="anyone")
    store.test_time[0] = 1550.0
    _renew_wake_path(store, recipient)
    assert _wake(store, sender, recipient)["decision"] == "rung"
    store.test_time[0] = 1600.0
    _renew_wake_path(store, recipient)
    store.update(*creds(recipient), park_reason="blocked")
    store.test_time[0] = 1700.0
    _renew_wake_path(store, recipient)
    assert _wake(store, sender, recipient)["decision"] == "rung"
    assert store.park_gate(recipient["agent_id"], "alice", since=1500.0) == {
        "gate": "block", "reason": "not_updated_this_turn"}


def test_park_gate_uses_delivery_time_not_the_staggered_or_served_ring(store):
    sender = store.register("alice")
    other, recipient = (_wake_capable(store) for _ in range(2))
    for agent in (other, recipient):
        store.update(*creds(agent), park_reason="blocked", park_needs="a review",
                     park_clear_by="anyone")
    store.test_time[0] = 1550.0
    for agent in (other, recipient):
        _renew_wake_path(store, agent)
    assert _wake(store, sender, other)["decision"] == "rung"
    store.test_time[0] = 1560.0
    for agent in (other, recipient):
        _renew_wake_path(store, agent)
    assert _wake(store, sender, recipient)["ring_at"] == 1580.0
    store.test_time[0] = 1570.0
    store.update(*creds(recipient), park_reason="blocked")
    store.test_time[0] = 1700.0
    assert store.attach(*creds(recipient), attachment_id=recipient["agent_id"][:8],
                        wake_enabled=True)["wake"]["decision"] == "rung"
    assert store.park_gate(recipient["agent_id"], "alice", since=1500.0) == {
        "gate": "allow", "reason": "parked"}


@pytest.mark.parametrize("updated", [False, True])
def test_park_gate_still_checks_this_turn_when_the_park_lapsed(store, updated):
    recipient = store.register("alice")
    store.update(*creds(recipient), park_reason="blocked", park_expires=1600.0)
    if updated:
        store.test_time[0] = 1510.0
        store.update(*creds(recipient), park_reason="blocked")
    store.test_time[0] = 2000.0
    assert store.park_gate(recipient["agent_id"], "alice", since=1500.0) == {
        "gate": "block", "reason": "no_park" if updated else "not_updated_this_turn"}


@pytest.mark.parametrize("status, gate, reason", [
    ("working", "block", "no_park"),
    ("DONE: review ready", "allow", "done"),
])
@pytest.mark.parametrize("updated", [False, True])
def test_park_gate_preserves_the_unparked_status_check(store, status, gate, reason, updated):
    recipient = store.register("alice")
    store.update(*creds(recipient), status=status)
    if updated:
        store.test_time[0] = 1600.0
        store.update(*creds(recipient), status=status)
    store.test_time[0] = 2000.0
    assert store.park_gate(recipient["agent_id"], "alice", since=1500.0) == (
        {"gate": gate, "reason": reason} if updated
        else {"gate": "block", "reason": "not_updated_this_turn"})


def test_a_woke_marker_is_logged_against_the_rings_it_answers(store):
    """When the Stop hook fires it tells the daemon the turn is starting;
    the daemon logs one ``woke`` event for the recipient, counting the rings
    served to it in the last hour and nothing else (no text, no ids beyond
    the row's own), so ``board-audit stats`` measures wake precision from
    the turn actually starting rather than from ``served_at``."""
    from tests.test_coordination_audit import events
    a = store.register("alice")
    b = store.register("alice", capabilities={"ring": True})
    store.update(*creds(b), status="parked", park_reason="blocked", park_needs="the review",
                 park_clear_by="anyone")
    store.test_time[0] = 2000.0
    store.attach(*creds(b), attachment_id="one", ring=True, ring_armed_until=2060.0)
    assert store.send(*creds(a), to=b["agent_id"], text="review is in",
                      request_id="r1")["wake"]["decision"] == "rung"
    # Nothing served yet: the marker still lands (a hook may fire on a ring
    # the heartbeat served before this daemon restarted), counting zero.
    assert store.woke(b["agent_id"], "alice") == {"recorded": True, "rings": 0}
    store.test_time[0] = 2005.0
    store.attach(*creds(b), attachment_id="one", ring=True, ring_armed_until=2060.0)
    store.test_time[0] = 2010.0
    assert store.woke(b["agent_id"], "alice") == {"recorded": True, "rings": 1}
    woke = events(store, "woke")
    assert [(e["actor"], e["principal"], e["agent_id"], e["recipient_agent_id"], e["payload"],
             e["created_at"]) for e in woke] == [
        ("agent", "alice", b["agent_id"], b["agent_id"], {"rings": 0}, 2000.0),
        ("agent", "alice", b["agent_id"], b["agent_id"], {"rings": 1}, 2010.0)]
    assert all(e["body"] is None for e in woke)
    # Unknown or foreign addresses record nothing: the hook has no standing.
    assert store.woke("0" * 32, "alice") == {"recorded": False, "reason": "unknown_agent"}
    assert store.woke(b["agent_id"], "bob") == {"recorded": False, "reason": "unknown_agent"}
    assert len(events(store, "woke")) == 2


# --- review fixes (2026-09-28) ------------------------------------------------

def _ring_capable(store, principal="alice"):
    """A plain-shim session: no live channel (wake_enabled false), but its
    adapter says at attach that a ring reaches it (the Stop hook's .ring
    marker, the Codex doorbell)."""
    agent = store.register(principal)
    store.attach(*creds(agent), attachment_id=agent["agent_id"][:8], ring=True)
    return agent


def test_static_ring_capability_does_not_claim_a_listener(store):
    sender = store.register("alice")
    recipient = _ring_capable(store)
    store.update(*creds(recipient), park_reason="waiting_peer", park_needs="review",
                 park_clear_by="anyone")
    wake = _wake(store, sender, recipient)
    assert wake["decision"] == "no_path"
    assert wake["reason"] == "listener_unknown"
    assert wake["queued"] is True
    assert wake["last_activity"] == store.test_time[0]
    assert "receive" in wake["fallback"]
    attached = store.heartbeat(*creds(recipient), attachment_id=recipient["agent_id"][:8],
                               generation=1, ring_armed_until=10000.0)
    assert attached["ring_armed_until"] == 1060.0
    assert attached["wake"]["decision"] == "rung"
    assert store.receive(*creds(recipient))["messages"]


def test_listener_heartbeat_expiry_and_disarm(store):
    sender = store.register("alice")
    recipient = store.register("alice", capabilities={"ring": True})
    first = store.attach(*creds(recipient), attachment_id="listener", ring=True,
                         ring_armed_until=1040.0)
    store.update(*creds(recipient), park_reason="waiting_peer", park_needs="review",
                 park_clear_by="anyone")
    assert _wake(store, sender, recipient)["decision"] == "rung"
    store.test_time[0] = 1041.0
    assert _wake(store, sender, recipient)["reason"] == "listener_expired"
    renewed = store.heartbeat(*creds(recipient), attachment_id="listener",
                              generation=first["generation"], ring_armed_until=1080.0)
    assert renewed["wake"]["decision"] == "rung"
    store.heartbeat(*creds(recipient), attachment_id="listener", generation=first["generation"])
    assert _wake(store, sender, recipient)["reason"] == "listener_expired"
    assert store.authenticate(*creds(recipient))["last_activity"] == 1000.0


def test_expired_attachment_cannot_claim_a_live_listener(store):
    sender = store.register("alice")
    recipient = store.register("alice")
    first = store.attach(*creds(recipient), attachment_id="listener", ring=True, ring_armed_until=1060.0)
    store.update(*creds(recipient), park_reason="waiting_peer", park_needs="review", park_clear_by="anyone")
    store.test_time[0] = 1060.0
    assert _wake(store, sender, recipient)["reason"] == "listener_expired"
    with pytest.raises(CoordinationError, match="stale_attachment"):
        store.heartbeat(*creds(recipient), attachment_id="listener", generation=first["generation"], ring_armed_until=1100.0)
    second = store.attach(*creds(recipient), attachment_id="replacement", ring=True)
    assert second["wake"] is None
    store.heartbeat(*creds(recipient), attachment_id="replacement", generation=second["generation"], ring_armed_until=1100.0)
    store.detach(*creds(recipient), attachment_id="replacement", generation=second["generation"])
    assert _wake(store, sender, recipient)["reason"] == "listener_expired"


def test_queued_no_path_mail_reaches_a_later_live_channel_and_counts_toward_caps(store):
    from pseudolife_memory.storage.coordination import WakePolicy
    store.wake = WakePolicy(per_recipient_per_hour=1)
    sender = store.register("alice")
    recipient = _ring_capable(store)
    store.update(*creds(recipient), park_reason="waiting_peer", park_needs="review", park_clear_by="anyone")
    first = store.send(*creds(sender), to=recipient["agent_id"], text="review ready", request_id="queued")
    assert first["wake"]["decision"] == "no_path"
    assert _wake(store, sender, recipient)["decision"] == "capped"
    attached = store.attach(*creds(recipient), attachment_id=recipient["agent_id"][:8], wake_enabled=True)
    assert attached["wake"]["decision"] == "rung"
    delivered = store.receive(*creds(recipient), for_delivery=True)["messages"]
    assert [m["message_id"] for m in delivered] == [first["message_id"]]


def test_unauthenticated_listener_heartbeat_cannot_arm_another_address(store):
    sender = store.register("alice")
    recipient = _ring_capable(store)
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.heartbeat("alice", recipient["agent_id"], sender["credential"],
                        attachment_id=recipient["agent_id"][:8], generation=1, ring_armed_until=1060.0)
    assert "ring_armed_until" not in store.authenticate(*creds(recipient))["capabilities"]


def test_queued_wake_does_not_invalidate_a_park_until_it_reaches_a_listener(store):
    sender = store.register("alice")
    recipient = _ring_capable(store)
    store.test_time[0] = 1010.0
    store.update(*creds(recipient), park_reason="waiting_peer", park_needs="review", park_clear_by="anyone")
    store.test_time[0] = 1020.0
    assert _wake(store, sender, recipient)["decision"] == "no_path"
    assert store.park_gate(recipient["agent_id"], "alice", since=1005.0)["gate"] == "allow"
    store.test_time[0] = 1030.0
    store.heartbeat(*creds(recipient), attachment_id=recipient["agent_id"][:8], generation=1,
                    ring_armed_until=1060.0)
    assert store.park_gate(recipient["agent_id"], "alice", since=1005.0)["gate"] == "block"


@pytest.mark.parametrize("arm_at_attach", [True, False])
def test_queued_wake_survives_a_lost_handoff_and_replacement_attachment(store, arm_at_attach):
    sender = store.register("alice")
    recipient = _ring_capable(store)
    store.update(*creds(recipient), park_reason="waiting_peer", park_needs="review",
                 park_clear_by="anyone")
    body = {"to": recipient["agent_id"], "text": "review ready", "request_id": "lost-handoff"}
    sent = store.send(*creds(sender), **body)
    assert sent["wake"]["decision"] == "no_path"
    store.test_time[0] = 1001.0
    lost = store.heartbeat(*creds(recipient), attachment_id=recipient["agent_id"][:8],
                           generation=1, ring_armed_until=1061.0)
    assert lost["wake"] == {"decision": "rung", "reason": "anyone", "ring_at": 1000.0}
    # The answer never reaches the recipient; its process and lease lapse.
    store.test_time[0] = 1062.0
    replacement = store.attach(*creds(recipient), attachment_id="replacement", ring=True,
                               ring_armed_until=1122.0 if arm_at_attach else None)
    assert replacement["generation"] == 2
    if arm_at_attach:
        assert replacement["wake"] == lost["wake"]
    else:
        assert replacement["wake"] is None
    beat = {"attachment_id": "replacement", "generation": replacement["generation"],
            "ring_armed_until": 1122.0}
    assert store.heartbeat(*creds(recipient), **beat)["wake"] == lost["wake"]
    # Replaying an existing authorization creates neither a send nor a cap entry.
    assert store.send(*creds(sender), **body)["message_id"] == sent["message_id"]
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (1,)
    assert [m["message_id"] for m in store.receive(*creds(recipient))["messages"]] == [sent["message_id"]]
    with pytest.raises(CoordinationError, match="stale_attachment"):
        store.heartbeat(*creds(recipient), attachment_id=recipient["agent_id"][:8], generation=1,
                        ring_armed_until=1122.0)


@pytest.mark.parametrize("retired", ["acknowledged", "expired"])
def test_replacement_attachment_does_not_replay_retired_queued_mail(store, retired):
    sender = store.register("alice")
    recipient = _ring_capable(store)
    store.update(*creds(recipient), park_reason="waiting_peer", park_needs="review",
                 park_clear_by="anyone")
    sent = store.send(*creds(sender), to=recipient["agent_id"], text="review ready", request_id="retired")
    store.test_time[0] = 1001.0
    assert store.heartbeat(*creds(recipient), attachment_id=recipient["agent_id"][:8],
                           generation=1, ring_armed_until=1061.0)["wake"] is not None
    if retired == "acknowledged":
        store.ack(*creds(recipient), message_id=sent["message_id"])
        store.test_time[0] = 1062.0
    else:
        store.test_time[0] = 87401.0
    replacement = store.attach(*creds(recipient), attachment_id="replacement", ring=True,
                               ring_armed_until=store.test_time[0] + 60)
    assert replacement["wake"] is None
    assert store.receive(*creds(recipient))["messages"] == []


def test_queued_wake_repeats_stably_after_a_same_generation_handoff_loss(store):
    sender = store.register("alice")
    recipient = _ring_capable(store)
    store.update(*creds(recipient), park_reason="waiting_peer", park_needs="review", park_clear_by="anyone")
    queued = store.send(*creds(sender), to=recipient["agent_id"], text="review ready", request_id="same-generation")
    beat = {"attachment_id": recipient["agent_id"][:8], "generation": 1, "ring_armed_until": 1060.0}
    store.test_time[0] = 1001.0
    lost = store.heartbeat(*creds(recipient), **beat)["wake"]
    store.test_time[0] = 1030.0
    # A restarted daemon's store has no process-local handoff state.
    restarted = CoordinationStore(store.storage, clock=store.clock, wake=store.wake)
    assert restarted.heartbeat(*creds(recipient), **beat)["wake"] == lost
    # A newer live-path wake must remain the stable offer while queued mail remains.
    newer = store.send(*creds(sender), to=recipient["agent_id"], text="another review", request_id="newer-ring")
    offered = store.heartbeat(*creds(recipient), **beat)["wake"]
    assert offered == newer["wake"]
    store.test_time[0] = 1058.0
    assert store.heartbeat(*creds(recipient), **beat)["wake"] == offered
    store.ack(*creds(recipient), message_id=queued["message_id"])
    assert store.heartbeat(*creds(recipient), **beat)["wake"] is None
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (2,)


def test_listener_deadline_caps_an_oversized_json_integer(store):
    agent = store.register("alice")
    attached = store.attach(*creds(agent), attachment_id="listener", ring=True, ring_armed_until=10**400)
    assert attached["ring_armed_until"] == 1060.0


@pytest.mark.parametrize("until", [True, "1001", float("inf"), float("nan"), -1])
def test_listener_deadline_rejects_invalid_values(store, until):
    agent = store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_ring_armed_until"):
        store.attach(*creds(agent), attachment_id="listener", ring=True, ring_armed_until=until)


def test_a_ring_path_declared_at_attach_is_a_wake_path(store):
    """The Stop hook and the Codex doorbell are wake paths although the
    adapter has no live channel: a parked plain-shim session is rung, and
    only an address with neither is no_path."""
    a = store.register("alice")
    b = _ring_capable(store)
    assert store.authenticate(*creds(b))["capabilities"]["ring"] is True
    store.heartbeat(*creds(b), attachment_id=b["agent_id"][:8], generation=1,
                    ring_armed_until=1060.0)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="anyone")
    _idle(store, b)
    assert _wake(store, a, b)["decision"] == "rung"
    # A later attach that declares no ring path takes it back.
    store.attach(*creds(b), attachment_id=b["agent_id"][:8], ring=False)
    _idle(store, b)
    assert _wake(store, a, b)["decision"] == "no_path"


def test_a_session_that_just_parked_is_rung_not_hinted(store):
    """Parking is a board action, so the recipient looks active for a
    minute after it; but a parked session has stopped, and no tool result
    will carry the mail. A reply that clears the need rings at once."""
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="waiting_peer", park_needs="review",
                 park_clear_by=a["agent_id"])
    assert _wake(store, a, b)["decision"] == "rung"
    # Chatter to it is still withheld, not hinted.
    c = store.register("alice")
    assert _wake(store, c, b)["decision"] == "withheld"


def test_clears_matches_whole_words_only(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="needs_info", park_needs="which judge family",
                 park_clear_by="maintainer")
    _idle(store, b)
    for loose in ("e", "ju", "dge fam", "which judges"):
        assert _wake(store, a, b, clears=loose)["decision"] == "withheld", loose
    assert _wake(store, a, b, clears="Judge  family")["decision"] == "rung"


def test_a_served_ring_is_served_again_within_a_heartbeat(store):
    """A heartbeat answer can be lost and retried; the ring rides the next
    answer too, for one heartbeat, and then stops."""
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="anyone")
    generation = store.attach(*creds(b), attachment_id=b["agent_id"][:8],
                              wake_enabled=True)["generation"]
    beat = dict(attachment_id=b["agent_id"][:8], generation=generation)
    _idle(store, b)
    _wake(store, a, b)
    first = store.heartbeat(*creds(b), **beat)["wake"]
    assert first["decision"] == "rung"
    store.test_time[0] += 5
    assert store.heartbeat(*creds(b), **beat)["wake"] == first
    store.test_time[0] += 30
    assert store.heartbeat(*creds(b), **beat)["wake"] is None


# --- park expiry (orchestrator decision 2026-09-28, pending the maintainer) --

def test_a_new_park_expires_by_default_and_is_capped(store):
    """A park whose clearer vanished must not withhold mail forever: a new
    park without ``park_expires`` gets PARK_DEFAULT_TTL, a refinement keeps
    the standing expiry, and one past PARK_MAX_TTL is refused."""
    from pseudolife_memory.storage.coordination import PARK_DEFAULT_TTL, PARK_MAX_TTL
    assert (PARK_DEFAULT_TTL, PARK_MAX_TTL) == (12 * 3600, 7 * 86400)
    a = store.register("alice")
    out = store.update(*creds(a), park_reason="waiting_peer", park_needs="a review")
    assert out["park_expires"] == 1000.0 + PARK_DEFAULT_TTL
    store.test_time[0] = 2000.0
    out = store.update(*creds(a), park_needs="a review of the diff")
    assert out["park_expires"] == 1000.0 + PARK_DEFAULT_TTL
    # A new reason keeps the standing expiry too.
    out = store.update(*creds(a), park_reason="blocked")
    assert out["park_expires"] == 1000.0 + PARK_DEFAULT_TTL
    # Cleared, then parked again: the default runs from now.
    store.update(*creds(a), park_reason=None)
    out = store.update(*creds(a), park_reason="blocked")
    assert out["park_expires"] == 2000.0 + PARK_DEFAULT_TTL
    assert store.update(*creds(a), park_expires=2000.0 + PARK_MAX_TTL)["park_expires"] == \
        2000.0 + PARK_MAX_TTL
    with pytest.raises(CoordinationError, match="invalid_park"):
        store.update(*creds(a), park_expires=2000.0 + PARK_MAX_TTL + 1)


def test_a_park_after_a_lapsed_one_is_a_new_park(store):
    """A lapsed park is no park: parking again without ``park_expires`` gets
    the default from now, not the lapsed expiry, so the new park stands (the
    Stop-hook gate allows it). Found by the review of PR #441 (2026-09-28):
    the re-park kept the lapsed expiry and was dead on arrival."""
    from pseudolife_memory.storage.coordination import PARK_DEFAULT_TTL
    a = store.register("alice")
    store.update(*creds(a), park_reason="waiting_peer", park_needs="a review",
                 park_clear_by="anyone", park_expires=1500.0)
    store.test_time[0] = 2000.0
    out = store.update(*creds(a), park_reason="blocked", park_needs="the GPU",
                       park_clear_by="anyone")
    assert (out["park_expires"], out["park_set_at"]) == (2000.0 + PARK_DEFAULT_TTL, 2000.0)
    assert store.park_gate(a["agent_id"], "alice") == {"gate": "allow", "reason": "parked"}


def test_a_park_over_a_lapsed_one_does_not_revive_its_need(store):
    """A new park over a lapsed one starts from an empty record: the lapsed
    need, clearer and resume note are not carried into it, since they may
    be stale and a fresh expiry would make them live again (a clearer's
    chatter would ring for 12 hours). Over a live park they stay."""
    a = store.register("alice")
    b = store.register("alice")
    store.update(*creds(a), park_reason="waiting_peer", park_needs="a review from b",
                 park_clear_by=b["agent_id"], park_resume="merge it", park_expires=1500.0)
    store.test_time[0] = 2000.0
    out = store.update(*creds(a), park_reason="blocked")
    assert (out["park_reason"], out["park_needs"], out["park_clear_by"],
            out["park_resume"]) == ("blocked", "", "", "")
    # Named fields are taken as given.
    store.test_time[0] = 2000.0 + 13 * 3600
    out = store.update(*creds(a), park_reason="needs_info", park_needs="which judge")
    assert (out["park_needs"], out["park_clear_by"]) == ("which judge", "")


def test_fields_alone_cannot_refine_a_lapsed_park(store):
    """A refinement needs a standing park, and a lapsed one no longer stands:
    its need may be stale, so the session restates it with a reason. Before,
    the refinement was accepted and kept the lapsed expiry, so it changed
    the text of a park nothing honoured."""
    a = store.register("alice")
    store.update(*creds(a), park_reason="waiting_peer", park_needs="a review",
                 park_expires=1500.0)
    store.test_time[0] = 2000.0
    for fields in ({"park_needs": "a review of the diff"}, {"park_expires": 9000.0}):
        with pytest.raises(CoordinationError, match="invalid_park"):
            store.update(*creds(a), **fields)
    assert store.authenticate(*creds(a))["park_needs"] == "a review"
    # A status update still clears the lapsed record, as it clears a live one.
    out = store.update(*creds(a), status="working again")
    assert (out["park_reason"], out["park_expires"]) == (None, None)


def test_clears_needs_a_distinctive_word(store):
    """A whole-word match of a filler word ("the", "of") is no match: the
    run must hold a word of four letters or more."""
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="needs_info", park_needs="review of the storage diff",
                 park_clear_by="maintainer")
    _idle(store, b)
    for filler in ("the", "of the", "diff"[:3]):
        assert _wake(store, a, b, clears=filler)["decision"] == "withheld", filler
    assert _wake(store, a, b, clears="the storage")["decision"] == "rung"


def test_a_burst_to_parked_peers_decides_and_staggers_each_ring(store):
    """A ``to: "all"`` burst (#430) gets one wake decision per recipient, and
    the rings of that one send are spaced by the fan-out stagger; a retry of
    the burst repeats every decision and writes no new ring."""
    a = store.register("alice")
    peers = [_wake_capable(store) for _ in range(3)]
    for agent in peers:
        store.update(*creds(agent), park_reason="blocked", park_needs="x", park_clear_by="anyone")
        _idle(store, agent)
    out = store.send(*creds(a), to="all", text="the GPU is free", request_id="burst")
    assert {r["wake"]["decision"] for r in out["receipts"]} == {"rung"}
    assert sorted(r["wake"]["ring_at"] for r in out["receipts"]) == [1000.0, 1030.0, 1060.0]
    again = store.send(*creds(a), to="all", text="the GPU is free", request_id="burst")
    assert [r["wake"] for r in again["receipts"]] == [r["wake"] for r in out["receipts"]]
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (3,)


def test_live_delivery_carries_only_mail_the_daemon_rang_or_hinted(store):
    """A recipient with a live channel is woken by every message its
    adapter's delivery receive yields, so that receive yields only mail the
    daemon decided to ring (or hinted to an active session, and mail from
    before v49): chatter withheld from a parked session waits for an
    explicit receive, which still returns it."""
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="waiting_peer", park_needs="the review",
                 park_clear_by="maintainer")
    _idle(store, b)
    withheld = store.send(*creds(a), to=b["agent_id"], text="fyi", request_id="chatter")
    rung = store.send(*creds(a), to=b["agent_id"], text="review done", request_id="clears",
                      clears="the review")
    assert (withheld["wake"]["decision"], rung["wake"]["decision"]) == ("withheld", "rung")
    live = store.receive(*creds(b), for_delivery=True)["messages"]
    assert [m["message_id"] for m in live] == [rung["message_id"]]
    pulled = store.receive(*creds(b))["messages"]
    assert [m["message_id"] for m in pulled] == [withheld["message_id"], rung["message_id"]]
    # A message from before v49 carries no decision and keeps live delivery.
    store.storage.conn.execute("UPDATE coordination_messages SET wake=NULL WHERE message_id=%s",
                               (withheld["message_id"],))
    live = store.receive(*creds(b), for_delivery=True)["messages"]
    assert {m["message_id"] for m in live} == {withheld["message_id"], rung["message_id"]}


def test_a_null_park_expiry_is_refused(store):
    """REST passes a JSON null through; a park stored without an expiry
    would never lapse, past the default and the cap."""
    a = store.register("alice")
    with pytest.raises(CoordinationError, match="invalid_park"):
        store.update(*creds(a), park_reason="blocked", park_needs="GPU", park_expires=None)
    assert store.authenticate(*creds(a))["park_reason"] is None

@pytest.mark.parametrize("recent", [True, False])
def test_codex_urgent_unknown_turn_reports_one_capped_queue_permission(store, recent):
    """Board-call age cannot prove that a desktop task is idle."""
    sender = store.register("alice")
    recipient = store.register("alice", capabilities={"codex": False, "ring": True})
    attached = store.attach(*creds(recipient), attachment_id="codex", ring=True,
                            ring_armed_until=1060.0)
    if not recent:
        _idle(store, recipient)
    receipt = store.send(*creds(sender), to=recipient["agent_id"], text="urgent dependency",
                         request_id="urgent-codex", urgent=True)
    assert receipt["state"] == "queued"  # mailbox queued, not a host notice
    assert receipt["wake"]["decision"] == "no_path"
    assert receipt["wake"]["reason"] == "no_steer_path"
    assert receipt["wake"]["attention"] is True
    assert receipt["wake"]["delivery"] == "queue_pending"
    assert receipt["wake"]["recipient_state"] == "unknown"
    answer = store.heartbeat(*creds(recipient), attachment_id="codex",
                             generation=attached["generation"], ring_armed_until=1060.0)
    assert answer["wake"] == {"decision": "attention", "reason": "urgent", "ring_at": 1000.0,
                              "message_expires_at": 87400.0, "queue_allowed": True, "recipient_state": "unknown"}
    assert store.receive(*creds(recipient), for_delivery=True)["messages"] == []
    assert len(store.receive(*creds(recipient))["messages"]) == 1


def test_codex_attention_spends_caps_and_preserves_done_and_ordinary_mail(store):
    from pseudolife_memory.storage.coordination import WakePolicy

    sender = store.register("alice")
    recipient = store.register("alice", capabilities={"codex": False})
    assert _wake(store, sender, recipient) == {"decision": "hinted", "reason": "active"}
    store.wake = WakePolicy(urgent_per_sender_per_hour=1)
    assert _wake(store, sender, recipient, urgent=True)["reason"] == "no_steer_path"
    assert _wake(store, sender, recipient, urgent=True) == {
        "decision": "capped", "reason": "urgent_sender_hour"}
    store.update(*creds(recipient), park_reason="done")
    assert _wake(store, sender, recipient, urgent=True) == {
        "decision": "not_needed", "reason": "parked_done", "reopen_by": DONE_REOPEN_BY}


@pytest.mark.parametrize("limit, reason", [("per_recipient_per_hour", "recipient_hour"),
                                           ("nightly_total", "nightly")])
def test_codex_attention_preserves_recipient_and_nightly_caps(store, limit, reason):
    from pseudolife_memory.storage.coordination import WakePolicy

    store.wake = WakePolicy(**{limit: 1})
    sender = store.register("alice")
    recipient = store.register("alice", capabilities={"codex": False})
    assert _wake(store, sender, recipient, urgent=True)["reason"] == "no_steer_path"
    assert _wake(store, sender, recipient, urgent=True) == {"decision": "capped", "reason": reason}


def test_codex_attention_staggers_while_eligible_parked_wake_keeps_queue_path(store):
    sender = store.register("alice")
    peers = [store.register("alice", capabilities={"codex": False, "ring": True})
             for _ in range(3)]
    for peer in peers:
        store.attach(*creds(peer), attachment_id=peer["agent_id"][:8], ring=True,
                     ring_armed_until=1060.0)
    first = _wake(store, sender, peers[0], urgent=True)
    second = _wake(store, sender, peers[1], urgent=True)
    assert (first["reason"], second["reason"]) == ("no_steer_path", "no_steer_path")
    assert (first["ring_at"], second["ring_at"]) == (1000.0, 1030.0)
    store.update(*creds(peers[2]), park_reason="blocked", park_needs="the review",
                 park_clear_by="maintainer")
    assert _wake(store, sender, peers[2])["decision"] == "withheld"
    parked = _wake(store, sender, peers[2], clears="the review")
    assert parked["decision"] == "rung" and parked["reason"] == "clears"
    assert parked["ring_at"] == 1060.0



def _legacy_codex_urgent_ring(store, *, queued=True, served_at=None, parked_before=False, parked_at=1999.0):
    """Seed the persisted v49 authorization an older daemon could produce."""
    sender = store.register("alice")
    recipient = store.register("alice", capabilities={"codex": False, "ring": True})
    if parked_before:
        store.test_time[0] = parked_at
        store.update(*creds(recipient), park_reason="blocked", park_needs="the review",
                     park_clear_by="maintainer")
    store.test_time[0] = 2000.0
    sent = store.send(*creds(sender), to=recipient["agent_id"], text="urgent dependency",
                      request_id="before-upgrade")
    wake = ({"decision": "no_path", "reason": "listener_unknown", "queued": True}
            if queued else {"decision": "rung", "reason": "urgent", "ring_at": 2000.0})
    store.storage.conn.execute("UPDATE coordination_messages SET wake=%s::jsonb WHERE message_id=%s",
                               (json.dumps(wake), sent["message_id"]))
    store.storage.conn.execute(
        "INSERT INTO coordination_wakes (recipient_agent_id,sender_agent_id,message_id,"
        "decision,reason,urgent,ring_at,created_at,served_at) "
        "VALUES (%s,%s,%s,'rung','urgent',true,2000.0,2000.0,%s)",
        (recipient["agent_id"], sender["agent_id"], sent["message_id"], served_at))
    return sender, recipient


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("served_at", [None, 2000.0])
@pytest.mark.parametrize("park", ["unparked", "expired", "done", "blocked", "changed"])
def test_codex_legacy_rung_offer_requires_current_live_park(store, queued, served_at, park):
    sender, recipient = _legacy_codex_urgent_ring(
        store, queued=queued, served_at=served_at, parked_before=park in {"blocked", "changed"})
    store.test_time[0] = 2001.0
    if park not in {"unparked", "blocked"}:
        if park == "done":
            store.update(*creds(recipient), park_reason="done")
        else:
            store.update(*creds(recipient), park_reason="blocked", park_needs="a different review",
                         park_clear_by="maintainer", park_expires=2002.0 if park == "expired" else 3000.0)
    store.test_time[0] = 2003.0
    answer = store.attach(*creds(recipient), attachment_id="upgraded-shim", ring=True,
                          ring_armed_until=2063.0)
    if park == "blocked":
        assert answer["wake"] == {"decision": "rung", "reason": "urgent", "ring_at": 2000.0, "message_expires_at": 88400.0}
    elif park == "done":
        assert answer["wake"] is None
    else:
        assert answer["wake"] == {"decision": "attention", "reason": "no_steer_path", "ring_at": 2000.0}
    # Serving does not rewrite the historical authorization or spend caps again.
    row = store.storage.conn.execute("SELECT decision,reason,urgent FROM coordination_wakes").fetchone()
    assert row == ("rung", "urgent", True)
    if park != "blocked":
        assert store.storage.conn.execute("SELECT served_at FROM coordination_wakes").fetchone()[0] == served_at


def test_upgrade_legacy_unparked_ring_never_queues_after_unshown_hint_and_plain_mail(store, tmp_path, monkeypatch):
    import asyncio
    from pseudolife_memory.codex_doorbell import CodexDoorbell
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    from tests.test_codex_doorbell import THREAD, _stub, _calls, _settle

    monkeypatch.setenv("PSEUDOLIFE_DIGEST_DIR", str(tmp_path))
    sender, recipient = _legacy_codex_urgent_ring(store)
    store.test_time[0] = 2001.0
    attached = store.attach(*creds(recipient), attachment_id="upgraded-shim", ring=True,
                            ring_armed_until=2061.0)
    command, log = _stub(tmp_path)

    async def drive():
        now = [0.0]
        adapter = CoordinationAdapter("http://127.0.0.1:1", "fixture-token",
            state_path=tmp_path / "unused-state.json", delivery_transport="codex",
            digest_path=tmp_path / "digest.txt")
        adapter._update_pending_count(attached)
        bell = CodexDoorbell(command, clock=lambda: now[0])
        bell.watch(THREAD, adapter)
        try:
            # A cancelled calling turn need not show its initial tool-result hint.
            store.test_time[0] = 2002.0
            plain = store.send(*creds(sender), to=recipient["agent_id"], text="ordinary follow-up",
                               request_id="after-upgrade")
            assert plain["wake"]["decision"] == "hinted"
            store.test_time[0] = 2042.0
            answer = store.heartbeat(*creds(recipient), attachment_id="upgraded-shim",
                generation=attached["generation"], ring_armed_until=2102.0)
            now[0] = 60.0
            adapter._update_pending_count(answer)
            await _settle(bell)
            assert not _calls(log), "legacy unparked authorization must not create a fresh native queue"
            assert adapter.ring_due() is None
            assert "no_steer_path" in adapter.unread_hint
            assert "recipient turn state unknown" in adapter.unread_hint
            assert not (tmp_path / "digest.ring").exists()
        finally:
            await bell.aclose()
            if adapter._ring_timer is not None:
                adapter._ring_timer.cancel()
    asyncio.run(drive())


@pytest.mark.parametrize("refined_at", [2000.0, 1998.0])
def test_codex_legacy_grant_cannot_follow_equal_or_backwards_clock_refinement(store, refined_at):
    _, recipient = _legacy_codex_urgent_ring(store, parked_before=True)
    store.test_time[0] = refined_at
    store.update(*creds(recipient), park_needs="a different review")
    store.test_time[0] = 2003.0
    answer = store.attach(*creds(recipient), attachment_id="upgraded-shim", ring=True,
                          ring_armed_until=2063.0)
    assert answer["wake"]["decision"] == "attention"
    assert answer["wake"]["reason"] == "no_steer_path"
    assert store.storage.conn.execute("SELECT served_at FROM coordination_wakes").fetchone()[0] is None


def test_codex_legacy_grant_preserves_unchanged_park_with_same_clock_stamp(store):
    _, recipient = _legacy_codex_urgent_ring(store, parked_before=True, parked_at=2000.0)
    store.test_time[0] = 2003.0
    answer = store.attach(*creds(recipient), attachment_id="upgraded-shim", ring=True,
                          ring_armed_until=2063.0)
    assert answer["wake"] == {"decision": "rung", "reason": "urgent", "ring_at": 2000.0, "message_expires_at": 88400.0}


@pytest.mark.parametrize("missing", ["send", "park", "park_snapshot"])
def test_codex_legacy_grant_requires_retained_current_park_audit_evidence(store, missing):
    _, recipient = _legacy_codex_urgent_ring(store, parked_before=True)
    if missing == "park_snapshot":
        store.storage.conn.execute("UPDATE coordination_agents SET park_set_at=1997.0 WHERE agent_id=%s",
                                   (recipient["agent_id"],))
    else:
        event = "send" if missing == "send" else "update"
        store.storage.conn.execute("DELETE FROM coordination_events WHERE event=%s", (event,))
    store.test_time[0] = 2003.0
    answer = store.attach(*creds(recipient), attachment_id="upgraded-shim", ring=True,
                          ring_armed_until=2063.0)
    assert answer["wake"]["decision"] == "attention"
    assert answer["wake"]["reason"] == "no_steer_path"
    assert store.storage.conn.execute("SELECT served_at FROM coordination_wakes").fetchone()[0] is None


@pytest.mark.parametrize("transition", ["done", "unparked", "refined", "acked", "expired"])
def test_retained_codex_offer_cannot_queue_after_authoritative_withdrawal(store, tmp_path, monkeypatch, transition):
    import asyncio
    from pseudolife_memory.codex_doorbell import CodexDoorbell
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    from tests.test_codex_doorbell import THREAD, _stub, _calls, _settle
    monkeypatch.setenv('PSEUDOLIFE_DIGEST_DIR', str(tmp_path))
    monkeypatch.setattr(time, 'time', lambda: store.test_time[0])
    sender = store.register('alice')
    recipient = store.register('alice', capabilities={'codex': False, 'ring': True})
    store.test_time[0] = 1999.0
    store.update(*creds(recipient), park_reason='blocked', park_needs='the review',
                 park_clear_by='maintainer')
    store.test_time[0] = 2000.0
    sent = store.send(*creds(sender), to=recipient['agent_id'], text='urgent dependency',
                      request_id='authorized-park', urgent=True,
                      expires_at=2003.0 if transition == 'expired' else None)
    store.test_time[0] = 2001.0
    attached = store.attach(*creds(recipient), attachment_id='current-shim', ring=True,
                            ring_armed_until=2061.0)
    assert attached['wake']['decision'] == 'rung'
    command, log = _stub(tmp_path)

    async def drive():
        now = [0.0]
        adapter = CoordinationAdapter('http://127.0.0.1:1', 'fixture-token',
            state_path=tmp_path / 'unused-state.json', delivery_transport='codex',
            digest_path=tmp_path / 'digest.txt')
        adapter._update_pending_count(attached)
        bell = CodexDoorbell(command, clock=lambda: now[0])
        bell.watch(THREAD, adapter)
        try:
            # Cancellation can prevent the attaching call's hint from being shown.
            store.test_time[0] = 2002.0
            if transition == 'done':
                store.update(*creds(recipient), park_reason='done')
            elif transition == 'unparked':
                store.update(*creds(recipient), status='working again')
            elif transition == 'refined':
                store.update(*creds(recipient), park_needs='a different review')
            elif transition == 'acked':
                store.ack(*creds(recipient), message_id=sent['message_id'])
            plain = store.send(*creds(sender), to=recipient['agent_id'], text='ordinary follow-up',
                               request_id='after-transition')
            assert plain['wake']['decision'] in {'not_needed', 'hinted', 'withheld'}
            store.test_time[0] = 2042.0
            answer = store.heartbeat(*creds(recipient), attachment_id='current-shim',
                generation=attached['generation'], ring_armed_until=2102.0)
            now[0] = 60.0
            adapter._update_pending_count(answer)
            await _settle(bell)
            assert answer['wake'] is None or answer['wake']['decision'] == 'attention'
            assert not _calls(log), 'withdrawn current-park authorization must not create a fresh native queue'
            assert adapter.ring_due() is None
            assert not (tmp_path / 'digest.ring').exists()
            if transition == 'refined':
                # A new grant for the current need may still legitimately ring.
                store.test_time[0] = 2043.0
                fresh = store.send(*creds(sender), to=recipient['agent_id'], text='new urgent dependency',
                                   request_id='current-park', urgent=True)
                assert fresh['wake']['decision'] == 'rung'
                store.test_time[0] = 2044.0
                current = store.heartbeat(*creds(recipient), attachment_id='current-shim',
                    generation=attached['generation'], ring_armed_until=2104.0)
                now[0] = 120.0
                adapter._update_pending_count(current)
                await _settle(bell)
                assert len(_calls(log)) == 1
        finally:
            await bell.aclose()
            if adapter._ring_timer is not None:
                adapter._ring_timer.cancel()
    asyncio.run(drive())


@pytest.mark.parametrize("earlier", ["clearer_ring", "attention"])
def test_codex_done_park_never_serves_a_grant_from_before_it(store, earlier):
    """A reopen reason alone is not enough: the grant must follow the
    current done park. A clearer's ring under an earlier park, or attention
    decided while unparked, is neither served nor stamped served."""
    clearer = store.register("alice")
    b = store.register("alice", project="proj", capabilities={"codex": False, "ring": True})
    attached = store.attach(*creds(b), attachment_id="codex", ring=True, ring_armed_until=1060.0)
    _idle(store, b)
    if earlier == "clearer_ring":
        store.update(*creds(b), park_reason="blocked", park_needs="the review",
                     park_clear_by=clearer["agent_id"])
        _idle(store, b)
        assert _wake(store, clearer, b)["reason"] == "clearer"
    else:
        store.maintainer_principals = frozenset({"maintainer-cli"})
        maintainer = store.register("maintainer-cli")
        wake = _wake(store, maintainer, b, urgent=True)
        assert (wake["reason"], wake["attention"]) == ("no_steer_path", True)
        assert store.storage.conn.execute(
            "SELECT decision,reason FROM coordination_wakes").fetchone() == ("attention", "maintainer")
    store.test_time[0] = 1001.0
    store.update(*creds(b), park_reason="done", park_clear_by=clearer["agent_id"])
    answer = store.heartbeat(*creds(b), attachment_id="codex",
                             generation=attached["generation"], ring_armed_until=1061.0)
    assert answer["wake"] is None
    assert store.storage.conn.execute(
        "SELECT served_at FROM coordination_wakes").fetchone() == (None,)


def test_a_replayed_reopen_repeats_its_receipt_and_rings_once(store):
    coordinator = _coordinator(store)
    b = _done(store)
    first = store.send(*creds(coordinator), to=b["agent_id"], text="reopen", request_id="r1",
                       urgent=True)
    again = store.send(*creds(coordinator), to=b["agent_id"], text="reopen", request_id="r1",
                       urgent=True)
    assert first["wake"] == again["wake"] and first["wake"]["reason"] == "coordinator"
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (1,)


def test_a_reopened_session_is_asked_to_park_again(store):
    coordinator = _coordinator(store)
    b = _done(store)
    since = store.test_time[0]
    store.test_time[0] += 1
    assert _wake(store, coordinator, b, urgent=True)["reason"] == "coordinator"
    assert store.park_gate(b["agent_id"], "alice", since=since) == {
        "gate": "block", "reason": "not_updated_this_turn"}


def test_a_coordinators_project_burst_reopens_only_its_own_project(store):
    coordinator = _coordinator(store)
    ours = _done(store, project="proj")
    theirs = _done(store, project="other")
    burst = store.send(*creds(coordinator), to="all", text="incident: stop deploys",
                       request_id="burst", urgent=True)
    reasons = {r["recipient_agent_id"]: r["wake"]["reason"] for r in burst["receipts"]}
    assert reasons[ours["agent_id"]] == "coordinator"
    assert reasons[theirs["agent_id"]] == "parked_done"


def test_releasing_the_lease_a_done_park_names_reopens_it_within_the_urgent_cap(store):
    """A done park whose ``park_clear_by`` names a lease is reopened by the
    agent that just released it (``_lease_clearer``), with ``urgent`` only."""
    holder = store.register("alice")
    store.acquire_lease(*creds(holder), name="gpu", ttl=3600)
    b = _done(store, clear_by="gpu")
    store.release_lease(*creds(holder), name="gpu")
    assert _wake(store, holder, b)["reason"] == "parked_done"
    assert _wake(store, holder, b, urgent=True) == {
        "decision": "rung", "reason": "clearer", "ring_at": 1000.0, "reopened": True}
    # Another agent's urgency names no lease it gave up.
    assert _wake(store, store.register("alice"), b, urgent=True)["reason"] == "parked_done"
