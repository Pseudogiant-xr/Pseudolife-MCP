"""Read-only Console coordination metadata, using disposable board state."""
import json
import threading
import pytest

from tests.asgi_helpers import call, stub_mcp
from tests.test_coordination_storage import store, creds  # noqa: F401
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from pseudolife_memory.web.api import build_console_app
from pseudolife_memory.web.fixtures import FixtureService


def test_snapshot_disconnect_is_whole_failure_and_next_snapshot_recovers(pg_url, store, monkeypatch):
    from pseudolife_memory.storage.coordination import CoordinationConnection, CoordinationStore
    store.register("alice", label="Before disconnect")
    connection = CoordinationConnection(pg_url)
    board = CoordinationStore(connection, clock=store.clock)
    original = board._roster
    def disconnect(*args, **kwargs):
        result = original(*args, **kwargs)
        connection._conn.close()
        return result
    monkeypatch.setattr(board, "_roster", disconnect)
    try:
        with pytest.raises(Exception):
            board.console_snapshot("alice")
        monkeypatch.setattr(board, "_roster", original)
        result = board.console_snapshot("alice")
        assert result["available"] and result["agents"][0]["label"] == "Before disconnect"
    finally:
        connection.close()


@pytest.mark.parametrize("principal", ["alice", "absent-principal"])
def test_console_timeline_indexes_sparse_principal_and_caps_execution(store, monkeypatch, principal):
    a = store.register("alice")
    b = store.register("bob")
    sent = store.send(*creds(a), to=b["agent_id"], text="fixture", request_id="one")
    store.receive(*creds(b, "bob"))
    store.ack(*creds(b, "bob"), message_id=sent["message_id"])
    store.storage.conn.execute("INSERT INTO coordination_events "
        "(seq,event,actor,created_at,agent_id,principal,recipient_agent_id,message_id,payload,prev_hash,hash) "
        "SELECT n,'send','fixture',1000,'unrelated','bob','unrelated-peer','other-'||n,'{}','','' "
        "FROM generate_series(1001,7000) AS n")
    store.storage.conn.execute("ANALYZE coordination_events")
    original = store._all
    plans = []
    def explain(query, params=()):
        if "timeline" in query:
            plans.extend(store.storage.conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query, params).fetchone()[0])
            print(json.dumps(plans))
            assert store.storage.conn.execute("SHOW statement_timeout").fetchone()[0] != "0"
        return original(query, params)
    monkeypatch.setattr(store, "_all", explain)
    result = store.console_snapshot(principal)
    assert bool(result["events"]) == (principal == "alice")
    text = json.dumps(plans)
    print(text)
    assert "coordination_events_principal_idx" in text
    assert "coordination_events_message_idx" in text
    def removed(node):
        if isinstance(node, dict):
            return [node.get("Rows Removed by Filter", 0),
                    *[n for value in node.values() for n in removed(value)]]
        if isinstance(node, list):
            return [n for value in node for n in removed(value)]
        return []
    assert max(removed(plans), default=0) < 100


def console_app(monkeypatch, store):
    from pseudolife_memory import coordination
    service = FixtureService()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["alice", "bob"]
    service._storage = object()
    service._coordination_lock = threading.Lock()
    monkeypatch.setattr(coordination, "_store", lambda _: store)
    return build_console_app(stub_mcp, None, lambda: {}, service,
                             token_map={"fixture-alice": "alice", "fixture-bob": "bob",
                                        "fixture-denied": "denied"})


def snapshot(app, token="fixture-alice", query="view=coordination&limit=50"):
    status, body = call(app, "GET", "/api/agents", query=query,
                        headers=[(b"authorization", ("Bearer " + token).encode())])
    return status, json.loads(body)


def test_snapshot_shows_roster_pending_park_children_and_fifo_without_writes(monkeypatch, store):
    a = store.register("alice", label="Worker", project="example", status="reviewing")
    b = store.register("alice", label="Helper")
    c = store.register("bob", label="Other principal")
    store.update(*creds(a), children=["helper"], park_reason="needs_info",
                 park_needs="specification", park_resume="continue review")
    store.send(*creds(b), to=a["agent_id"], text="private body", request_id="one")
    store.send(*creds(a), to=c["agent_id"], text="other body", request_id="two")
    store.acquire_lease(*creds(a), name="fixture-resource", ttl=60)
    store.acquire_lease(*creds(b), name="fixture-resource", ttl=60)
    store.test_time[0] += 61  # ordinary lease listing would now settle the hold
    before = store.storage.conn.execute("SELECT count(*) FROM coordination_events").fetchone()
    app = console_app(monkeypatch, store)
    status, out = snapshot(app)
    assert status == 200 and out["available"]
    agents = {row["agent_id"]: row for row in out["agents"]}
    assert agents[a["agent_id"]]["pending_count"] == 1
    assert agents[c["agent_id"]]["pending_count"] is None
    assert agents[a["agent_id"]]["park_needs"] == "specification"
    assert agents[a["agent_id"]]["children"][0]["label"] == "helper"
    assert agents[a["agent_id"]]["status_age"]
    lease = out["leases"][0]
    assert lease["expired"] and lease["holder"]["agent_id"] == a["agent_id"]
    assert lease["queue"][0]["agent_id"] == b["agent_id"]
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_events").fetchone() == before
    assert store.storage.conn.execute("SELECT first_read_at FROM coordination_messages WHERE recipient_agent_id=%s",
                                      (a["agent_id"],)).fetchone() == (None,)
    encoded = json.dumps(out)
    assert "private body" not in encoded and "other body" not in encoded
    assert "credential" not in encoded and "body_salt" not in encoded


def test_timeline_is_bounded_principal_scoped_and_redacts_legacy_payload(monkeypatch, store):
    a, b = store.register("alice"), store.register("alice")
    x, y = store.register("bob"), store.register("bob")
    msg = store.send(*creds(a), to=b["agent_id"], text="body sentinel", request_id="one")
    secret = store.send(*creds(x, "bob"), to=y["agent_id"], text="hidden", request_id="two")
    store.receive(*creds(b))
    store.ack(*creds(b), message_id=msg["message_id"])
    # Old send payloads carried text; a projection must never return payload wholesale.
    store.storage.conn.execute("UPDATE coordination_events SET payload=%s WHERE message_id=%s AND event='send'",
                               (json.dumps({"text": "legacy sentinel", "wake": "hinted"}), msg["message_id"]))
    # Retained audit after expiry also includes unacknowledged mail.
    expiring = store.send(*creds(a), to=b["agent_id"], text="expiring", request_id="three")
    store.test_time[0] += 86401
    store.prune()
    app = console_app(monkeypatch, store)
    status, out = snapshot(app)
    assert status == 200
    events = out["events"]
    assert {"send", "read", "ack", "expire"} <= {e["event"] for e in events}
    assert events == sorted(events, key=lambda e: e["seq"], reverse=True)
    assert next(e for e in events if e["event"] == "expire")["expired_count"] == 2
    encoded = json.dumps(events)
    assert secret["message_id"] not in encoded
    assert "legacy sentinel" not in encoded and "body sentinel" not in encoded
    assert all("payload" not in e and "body" not in e for e in events)
    assert expiring["message_id"] in encoded
    # Repeated read/ack rows are enough to exercise the bounded SQL page.
    with store.storage._txn():
        for _ in range(101):
            store._append([store._event("woke", {"rings": 1}, principal="alice", agent_id=a["agent_id"])],
                          store.clock())
    _, out = snapshot(app)
    assert len(out["events"]) == 100 and out["events_truncated"]


def test_snapshot_gate_request_context_errors_and_methods(monkeypatch, store):
    app = console_app(monkeypatch, store)
    assert snapshot(app, "fixture-denied")[0] == 403
    assert snapshot(app, "wrong")[0] == 401
    assert call(app, "GET", "/api/agents", query="view=coordination")[0] == 401
    assert call(app, "POST", "/api/agents", query="view=coordination",
                headers=[(b"authorization", b"Bearer fixture-alice")])[0] == 405
    assert snapshot(app, query="view=coordination&limit=0")[0] == 400
    assert snapshot(app, query="view=coordination&limit=51")[0] == 400
    assert snapshot(app, "fixture-bob")[0] == 200
    open_app = build_console_app(stub_mcp, None, lambda: {}, FixtureService())
    assert call(open_app, "GET", "/api/agents", query="view=coordination")[0] == 401
    from pseudolife_memory.writer_context import _http_request_headers
    assert _http_request_headers() is None
    from pseudolife_memory import coordination
    monkeypatch.setattr(coordination, "_store", lambda _: (_ for _ in ()).throw(RuntimeError("private failure body")))
    status, out = snapshot(app)
    assert status == 500 and out == {"error": "coordination_unavailable"}
    assert _http_request_headers() is None


def test_snapshot_unavailable_and_existing_awareness_contract(monkeypatch):
    service = FixtureService()
    service.coordination_awareness = lambda **_: {"peers": [], "available": True}
    app = build_console_app(stub_mcp, "fixture-alice", lambda: {}, service)
    assert snapshot(app, query="")[1] == {"peers": [], "available": True}
    service.config.coordination.enabled = False
    assert snapshot(app)[1]["reason"] == "disabled"
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["default"]
    service._storage = None
    assert snapshot(app)[1]["reason"] == "not_initialized"
