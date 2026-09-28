"""One send reaches a whole project, or the whole board.

On 2026-09-27 a session that had fixed a host-wide fault sent the same
HOST FIX text to six agents one at a time, finding them by reading the peer
list and pattern-matching status strings; one of the sends failed
transiently and had to be repeated. ``to: "project:<name>"`` now reaches
every attached, non-idle agent in that project except the sender, and
``to: "all"`` every attached, non-idle agent on the board: exactly the peers
the list shows with ``adapter_available``. One request id covers the burst,
so a retry returns the same receipts; the burst is atomic, so a retry never
sees half of one. The audit log keeps one ``send`` event per recipient, each
with its own body and salt, so ``verify``, ``redact`` and ``export --agent``
need no new event kind; schema v48 lets the mailbox rows share the request
id.
"""
import pytest

from pseudolife_memory.coordination import dispatch
from pseudolife_memory.storage import coordination as store_module
from pseudolife_memory.storage.coordination import (
    ATTACHED_IDLE_WINDOW, COORDINATION_SCHEMA_SQL, FANOUT_MAX, CoordinationError,
)
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_audit import events, verify
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_coordination_storage import creds, store  # noqa: F401

DAY = 86400


def attached(store, project, *, principal="alice", wake=False):
    """A registered peer whose adapter is up."""
    agent = store.register(principal, project=project, wake_enabled=wake)
    store.attach(*creds(agent, principal), attachment_id="live-" + agent["agent_id"][:8],
                 wake_enabled=wake)
    return agent


def idle(store, agent):
    """Still attached (its shim heartbeats), but no action of its own for
    longer than the peer list shows an attached peer."""
    store.storage.conn.execute(
        "UPDATE coordination_agents SET last_activity=%s,lease_until=%s WHERE agent_id=%s",
        (store.test_time[0] - ATTACHED_IDLE_WINDOW - 1, store.test_time[0] + 60,
         agent["agent_id"]))


def recipients(out):
    return [r["recipient_agent_id"] for r in out["receipts"]]


def pending(store, agent, principal="alice"):
    return [m["text"] for m in store.receive(*creds(agent, principal))["messages"]]


def test_a_project_broadcast_reaches_its_attached_working_peers_and_nobody_else(store):
    sender = attached(store, "p")
    a, b = attached(store, "p"), attached(store, "p")
    elsewhere = attached(store, "q")
    detached = store.register("alice", project="p")
    parked = attached(store, "p")
    idle(store, parked)
    out = store.send(*creds(sender), to="project:p", text="HOST FIX: swap done", request_id="r")
    assert out["to"] == "project:p" and out["recipients"] == 2
    assert recipients(out) == sorted([a["agent_id"], b["agent_id"]])
    assert len({r["message_id"] for r in out["receipts"]}) == 2
    assert all(r["state"] == "queued" for r in out["receipts"])
    assert pending(store, a) == pending(store, b) == ["HOST FIX: swap done"]
    for left_out in (sender, elsewhere, detached, parked):
        assert pending(store, left_out) == []


def test_all_reaches_every_attached_working_peer_across_projects_and_principals(store):
    sender = attached(store, "p")
    mine, theirs = attached(store, "p"), attached(store, "q")
    other = attached(store, "", principal="bob")
    store.register("alice", project="q")
    out = store.send(*creds(sender), to="all", text="host-wide: GPU freed", request_id="r")
    assert out["to"] == "all"
    assert recipients(out) == sorted([mine["agent_id"], theirs["agent_id"], other["agent_id"]])
    assert pending(store, other, "bob") == ["host-wide: GPU freed"]


def test_one_request_id_covers_the_burst_so_a_retry_returns_the_same_receipts(store, monkeypatch):
    sender = attached(store, "p")
    a, b = attached(store, "p"), attached(store, "p")
    first = store.send(*creds(sender), to="project:p", text="fix", request_id="r")
    store.test_time[0] += 2
    assert store.send(*creds(sender), to="project:p", text="fix", request_id="r") == first
    with pytest.raises(CoordinationError, match="request_conflict"):
        store.send(*creds(sender), to="project:p", text="changed", request_id="r")
    with pytest.raises(CoordinationError, match="request_conflict"):
        store.send(*creds(sender), to=a["agent_id"], text="fix", request_id="r")
    # The rows share the request id (schema v48) and the recipients each got
    # the message once.
    assert store.storage.conn.execute(
        "SELECT count(*) FROM coordination_messages WHERE request_id='r'").fetchone()[0] == 2
    assert pending(store, a) == pending(store, b) == ["fix"]
    # The burst counts once against the sender's rate, like the one request
    # it is.
    monkeypatch.setattr(store_module, "SEND_RATE", 2)
    store.send(*creds(sender), to=a["agent_id"], text="one more", request_id="r2")
    with pytest.raises(CoordinationError, match="rate_limited"):
        store.send(*creds(sender), to=b["agent_id"], text="too many", request_id="r3")


def test_a_burst_over_the_cap_or_with_nobody_to_reach_is_refused_whole(store, monkeypatch):
    sender = attached(store, "p")
    peers = [attached(store, "p") for _ in range(3)]
    monkeypatch.setattr(store_module, "FANOUT_MAX", 1)
    with pytest.raises(CoordinationError, match="fanout_too_large") as refused:
        store.send(*creds(sender), to="project:p", text="fix", request_id="r")
    # The real count, not the one-over-the-cap the set is read with.
    assert refused.value.detail == "3 recipients, limit 1"
    with pytest.raises(CoordinationError, match="no_recipients"):
        store.send(*creds(sender), to="project:empty", text="fix", request_id="r")
    for bad in ("project:", "project:" + "x" * 121, "PROJECT:p", "all ", "project"):
        with pytest.raises(CoordinationError, match="invalid_recipient|recipient_not_found"):
            store.send(*creds(sender), to=bad, text="fix", request_id="r")
    assert all(pending(store, peer) == [] for peer in peers)
    assert events(store, "send") == []
    assert FANOUT_MAX == 50


def test_a_full_mailbox_refuses_the_whole_burst_and_writes_nothing(store, monkeypatch):
    sender = attached(store, "p")
    full, free = attached(store, "p"), attached(store, "p")
    monkeypatch.setattr(store_module, "MAX_PENDING", 1)
    store.send(*creds(sender), to=full["agent_id"], text="first", request_id="r0")
    with pytest.raises(CoordinationError, match="queue_full") as refused:
        store.send(*creds(sender), to="project:p", text="fix", request_id="r")
    assert refused.value.detail == full["agent_id"][:12]
    assert pending(store, free) == []
    assert len(events(store, "send")) == 1


def test_the_audit_log_keeps_one_send_event_per_recipient_and_still_verifies(store):
    sender = attached(store, "p")
    peers = [attached(store, "p") for _ in range(3)]
    out = store.send(*creds(sender), to="project:p", text="HOST FIX", request_id="r")
    sent = events(store, "send")
    assert [e["message_id"] for e in sent] == [r["message_id"] for r in out["receipts"]]
    assert [e["recipient_agent_id"] for e in sent] == sorted(p["agent_id"] for p in peers)
    assert all(e["body"] == "HOST FIX" and e["agent_id"] == sender["agent_id"] for e in sent)
    assert len({e["body_salt"] for e in sent}) == 3
    assert all(e["payload"]["fanout"] == {"to": "project:p", "recipients": 3}
               and e["payload"]["request_id"] == "r" for e in sent)
    assert verify(store)["ok"]
    # A direct send carries no fan-out marker.
    store.send(*creds(sender), to=peers[0]["agent_id"], text="just you", request_id="r2")
    assert "fanout" not in events(store, "send")[-1]["payload"]
    # One recipient's copy can be redacted on its own; the rest keep theirs.
    store.redact(out["receipts"][1]["message_id"], "pasted a token")
    bodies = [e["body"] for e in events(store, "send")]
    assert bodies == ["HOST FIX", None, "HOST FIX", "just you"]
    # The operator is told which other copies still hold the body.
    ids = [r["message_id"] for r in out["receipts"]]
    assert store.redact(ids[0], "pasted a token")["other_copies"] == [ids[1], ids[2]]
    assert store.redact(ids[2], "pasted a token")["other_copies"] == [ids[0], ids[1]]
    direct = events(store, "send")[-1]["message_id"]
    assert store.redact(direct, "pasted a token")["other_copies"] == []
    assert verify(store)["ok"]
    store.test_time[0] += DAY + 1
    store.prune()
    assert verify(store)["ok"]


def test_the_wake_decision_names_live_delivery_only_for_a_wake_enabled_peer(store):
    """``path`` says whether the recipient's adapter holds a live channel;
    since v49 ``wake`` is the daemon's ring decision (every peer here acted
    just now, so each is hinted)."""
    sender = attached(store, "p")
    live, pull = attached(store, "p", wake=True), attached(store, "p")
    out = store.send(*creds(sender), to="project:p", text="fix", request_id="r")
    assert {r["recipient_agent_id"]: r["path"] for r in out["receipts"]} == {
        live["agent_id"]: "live", pull["agent_id"]: "pull"}
    assert {r["wake"]["decision"] for r in out["receipts"]} == {"hinted"}
    direct = store.send(*creds(sender), to=live["agent_id"], text="you", request_id="r2")
    assert direct["recipient_agent_id"] == live["agent_id"] and direct["path"] == "live"


def test_a_reply_cannot_ride_a_broadcast(store):
    sender, peer = attached(store, "p"), attached(store, "p")
    parent = store.send(*creds(peer), to=sender["agent_id"], text="q", request_id="q")
    with pytest.raises(CoordinationError, match="invalid_reply"):
        store.send(*creds(sender), to="project:p", text="a", request_id="r",
                   reply_to=parent["message_id"])
    assert events(store, "send")[-1]["message_id"] == parent["message_id"]


def _register(service, **fields):
    agent = dispatch(service, "register", fields, headers={}, principal=PRINCIPAL)
    headers = {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}
    return agent, headers


def test_over_dispatch_every_recipient_of_a_burst_is_notified(coordinating):
    """The MCP tool and the REST route share ``dispatch``; the live-wake
    notifier must learn each recipient, not the address the burst was sent
    to."""
    sender, mine = _register(coordinating, project="p")
    peers = []
    for _ in range(2):
        agent, headers = _register(coordinating, project="p")
        dispatch(coordinating, "attach", {"attachment_id": "live-" + agent["agent_id"][:8]},
                 headers=headers, principal=PRINCIPAL)
        peers.append(agent["agent_id"])
    woken = []
    coordinating._coordination_notifier = woken.append
    out = dispatch(coordinating, "send", {"to": "all", "text": "fix", "request_id": "r"},
                   headers=mine, principal=PRINCIPAL)
    assert recipients(out) == sorted(peers) and sorted(woken) == sorted(peers)
    direct = dispatch(coordinating, "send", {"to": peers[0][:8], "text": "you", "request_id": "r2"},
                      headers=mine, principal=PRINCIPAL)
    assert direct["recipient_agent_id"] == peers[0] and woken[-1] == peers[0]


def test_over_rest_a_burst_returns_per_recipient_receipts(pg_conn, pg_url):
    import httpx
    from tests.test_coordination_leases_api import _app, _post, _register as register, _run

    storage, app = _app(pg_url)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            sender = await register(client, "sender")
            peers = []
            for label in ("a", "b"):
                peer = await register(client, label)
                attach = await _post(client, peer, "attach", {"attachment_id": "live-" + label})
                assert attach.status_code == 200, attach.text
                peers.append(peer)
            sent = await _post(client, sender, "send",
                               {"to": "all", "text": "fix", "request_id": "r"})
            assert sent.status_code == 200, sent.text
            out = sent.json()
            assert out["recipients"] == 2
            assert recipients(out) == sorted(p["X-PL-Agent"] for p in peers)
            for peer in peers:
                got = await _post(client, peer, "receive", {})
                assert [m["text"] for m in got.json()["messages"]] == ["fix"]
            refused = await _post(client, sender, "send",
                                  {"to": "project:nobody", "text": "fix", "request_id": "r2"})
            assert refused.status_code == 400 and refused.json() == {"error": "no_recipients"}
    _run(storage, drive)


def test_v48_replaces_the_sender_request_key_with_one_that_admits_a_recipient(pg_conn):
    """A bank created before v48 carries the two-column unique constraint;
    the schema pass must drop it (guarded, so an open export never blocks
    the pass) and the three-column unique index must be there either way."""
    pg_conn.autocommit = True
    pg_conn.execute(COORDINATION_SCHEMA_SQL)
    from pseudolife_memory.storage.schema import assert_disposable_database
    assert_disposable_database(pg_conn)
    pg_conn.execute("TRUNCATE coordination_messages")
    pg_conn.execute("ALTER TABLE coordination_messages ADD CONSTRAINT "
                    "coordination_messages_sender_agent_id_request_id_key "
                    "UNIQUE (sender_agent_id, request_id)")
    pg_conn.execute(COORDINATION_SCHEMA_SQL)
    constraints = {r[0] for r in pg_conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid='coordination_messages'::regclass")}
    assert "coordination_messages_sender_agent_id_request_id_key" not in constraints
    index = pg_conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename='coordination_messages' "
        "AND indexname='coordination_messages_request_idx'").fetchone()
    assert index and index[0].startswith("CREATE UNIQUE INDEX")
    assert "(sender_agent_id, request_id, recipient_agent_id)" in index[0]
    # Found by its columns, whatever name it was given; a unique key over
    # other columns is left alone.
    pg_conn.execute("ALTER TABLE coordination_messages ADD CONSTRAINT some_other_name "
                    "UNIQUE (request_id, sender_agent_id)")
    pg_conn.execute("ALTER TABLE coordination_messages ADD CONSTRAINT keep_this_one "
                    "UNIQUE (sender_agent_id, request_id, recipient_sequence)")
    pg_conn.execute(COORDINATION_SCHEMA_SQL)
    constraints = {r[0] for r in pg_conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid='coordination_messages'::regclass")}
    assert "some_other_name" not in constraints and "keep_this_one" in constraints
    pg_conn.execute("ALTER TABLE coordination_messages DROP CONSTRAINT keep_this_one")
