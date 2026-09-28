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
    from pseudolife_memory.storage.schema import assert_disposable_database
    assert_disposable_database(pg_conn)
    pg_conn.execute("TRUNCATE coordination_messages, coordination_agents, coordination_events, "
                    "coordination_wakes")
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
    return store.send(*creds(sender), to=recipient["agent_id"], text=text,
                      request_id=request_id or f"w{_wake.n}", **kw)["wake"]


def _idle(store, agent, seconds=3600):
    """Make ``agent`` idle: no activity for ``seconds``."""
    store.storage.conn.execute("UPDATE coordination_agents SET last_activity=%s WHERE agent_id=%s",
                               (store.test_time[0] - seconds, agent["agent_id"]))


def _wake_capable(store, principal="alice"):
    agent = store.register(principal, wake_enabled=True)
    store.attach(*creds(agent), attachment_id=agent["agent_id"][:8], wake_enabled=True)
    return agent


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
    assert _wake(store, a, b) == {"decision": "not_needed", "reason": "parked_done"}


def test_a_recipient_without_a_wake_path_reports_no_path_and_its_need(store):
    a = store.register("alice")
    b = store.register("alice")   # pull-only
    store.update(*creds(b), park_reason="blocked", park_needs="a review", park_clear_by="anyone")
    _idle(store, b)
    assert _wake(store, a, b) == {"decision": "no_path", "reason": "wake_disabled",
                                  "park_needs": "a review", "park_clear_by": "anyone"}


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
                               "park_clear_by": c["agent_id"]}
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
    assert _wake(store, a, b)["decision"] == "nudged"


def test_an_idle_unparked_recipient_is_nudged_once_an_hour(store):
    a = store.register("alice")
    b = _wake_capable(store)
    _idle(store, b)
    assert _wake(store, a, b) == {"decision": "nudged", "reason": "no_park", "ring_at": 1000.0}
    store.test_time[0] = 1000.0 + 1800
    _idle(store, b)
    assert _wake(store, a, b) == {"decision": "capped", "reason": "nudge_hour"}
    store.test_time[0] = 1000.0 + 3601
    _idle(store, b)
    assert _wake(store, a, b)["decision"] == "nudged"


def test_rings_to_one_recipient_are_capped_per_hour(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="anyone")
    for i in range(20):
        store.test_time[0] = 1000.0 + i * 60
        _idle(store, b)
        assert _wake(store, a, b)["decision"] == "rung"
    store.test_time[0] = 1000.0 + 20 * 60
    _idle(store, b)
    assert _wake(store, a, b) == {"decision": "capped", "reason": "recipient_hour",
                                  "park_needs": "x", "park_clear_by": "anyone"}
    store.test_time[0] = 1000.0 + 3601
    _idle(store, b)
    assert _wake(store, a, b)["decision"] == "rung"


def test_urgent_is_capped_per_sender(store):
    a = store.register("alice")
    b = _wake_capable(store)
    store.update(*creds(b), park_reason="blocked", park_needs="x", park_clear_by="maintainer")
    for i in range(6):
        store.test_time[0] = 1000.0 + i * 120
        _idle(store, b)
        assert _wake(store, a, b, urgent=True)["decision"] == "rung"
    store.test_time[0] = 1000.0 + 6 * 120
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
    # A nudge says so, so the shim can ask for a park record.
    store.update(*creds(b), park_reason=None)
    store.test_time[0] = 5000.0
    _idle(store, b)
    _wake(store, a, b)
    assert store.attach(*creds(b), attachment_id=b["agent_id"][:8], wake_enabled=True)["wake"] == {
        "decision": "nudged", "reason": "no_park", "ring_at": 5000.0}


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
    _idle(store, b)
    first = store.send(*creds(a), to=b["agent_id"], text="t", request_id="again")
    again = store.send(*creds(a), to=b["agent_id"], text="t", request_id="again")
    assert first["wake"]["decision"] == "nudged"
    assert again["wake"] == first["wake"]
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_wakes").fetchone() == (1,)


# --- the Stop-hook park gate -------------------------------------------------

def test_park_gate_asks_an_unparked_session_once(store):
    """What the Stop hook asks at turn end: block when the session's row has
    no live park record and its status is not done-shaped, or when it did
    not update its status during the turn; allow otherwise."""
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
    # Parked last turn, but this turn ended without a word: ask again.
    store.test_time[0] = 2000.0
    assert gate(since=1500.0) == {"gate": "block", "reason": "not_updated_this_turn"}
    store.update(*creds(a), park_reason="blocked")
    assert gate(since=1500.0) == {"gate": "allow", "reason": "parked"}
    # An expired park is no park.
    store.update(*creds(a), park_reason="blocked", park_expires=2500.0)
    store.test_time[0] = 3000.0
    assert gate() == {"gate": "block", "reason": "no_park"}
    # Unknown or foreign addresses are allowed: the hook has nothing to ask.
    assert store.park_gate("0" * 32, "alice") == {"gate": "allow", "reason": "unknown_agent"}
    assert store.park_gate(a["agent_id"], "bob") == {"gate": "allow", "reason": "unknown_agent"}


# --- review fixes (2026-09-28) ------------------------------------------------

def _ring_capable(store, principal="alice"):
    """A plain-shim session: no live channel (wake_enabled false), but its
    adapter says at attach that a ring reaches it (the Stop hook's .ring
    marker, the Codex doorbell)."""
    agent = store.register(principal)
    store.attach(*creds(agent), attachment_id=agent["agent_id"][:8], ring=True)
    return agent


def test_a_ring_path_declared_at_attach_is_a_wake_path(store):
    """The Stop hook and the Codex doorbell are wake paths although the
    adapter has no live channel: a parked plain-shim session is rung, and
    only an address with neither is no_path."""
    a = store.register("alice")
    b = _ring_capable(store)
    assert store.authenticate(*creds(b))["capabilities"]["ring"] is True
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
