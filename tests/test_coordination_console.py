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


@pytest.mark.parametrize("principal,scope", [
    ("alice", None), ("absent-principal", None),
    ("alice", ["absent-principal", "alice", "bob"]),
], ids=["alice", "absent", "owner-scope"])
def test_console_timeline_indexes_sparse_principal_and_caps_execution(store, monkeypatch, principal,
                                                                      scope):
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
    result = store.console_snapshot(principal, scope=scope)
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


# -- v59: the owner's Console sees every owner principal's board metadata -----

def owner_app(monkeypatch, store, *, not_owner=("guest",)):
    """Three environment principals; ``guest`` is listed as someone else's."""
    from pseudolife_memory import principals
    tokens = {"fixture-alice": "alice", "fixture-bob": "bob", "fixture-guest": "guest"}
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", ",".join(f"{t}:{p}" for t, p in tokens.items()))
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(principals, "_STORE", None)
    from pseudolife_memory import coordination
    service = FixtureService()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["alice", "bob", "guest"]
    service.config.coordination.not_owner_principals = list(not_owner)
    service._storage = object()
    service._coordination_lock = threading.Lock()
    monkeypatch.setattr(coordination, "_store", lambda _: store)
    return build_console_app(stub_mcp, None, lambda: {}, service, token_map=tokens)


def test_an_owner_console_sees_every_owner_principals_mail_and_counts(monkeypatch, store):
    a = store.register("alice", label="desk")
    b = store.register("bob", label="laptop")
    g1, g2 = store.register("guest"), store.register("guest")
    to_bob = store.send(*creds(a), to=b["agent_id"], text="owner body", request_id="one")
    to_alice = store.send(*creds(b, "bob"), to=a["agent_id"], text="owner body", request_id="two")
    guest_only = store.send(*creds(g1, "guest"), to=g2["agent_id"], text="guest body",
                            request_id="three")
    store.receive(*creds(b, "bob"))
    store.ack(*creds(b, "bob"), message_id=to_bob["message_id"])
    app = owner_app(monkeypatch, store)
    for token in ("fixture-alice", "fixture-bob"):
        status, out = snapshot(app, token)
        assert status == 200 and out["available"]
        counts = {row["agent_id"]: row["pending_count"] for row in out["agents"]}
        assert counts == {a["agent_id"]: 1, b["agent_id"]: 0,
                          g1["agent_id"]: None, g2["agent_id"]: None}, token
        seen = {e["message_id"] for e in out["events"]}
        assert {to_bob["message_id"], to_alice["message_id"]} <= seen
        assert guest_only["message_id"] not in seen
        assert {"send", "read", "ack"} <= {e["event"] for e in out["events"]}
        assert "owner body" not in json.dumps(out) and "guest body" not in json.dumps(out)
    status, out = snapshot(app, "fixture-guest")
    assert status == 200
    counts = {row["agent_id"]: row["pending_count"] for row in out["agents"]}
    assert counts == {a["agent_id"]: None, b["agent_id"]: None,
                      g1["agent_id"]: 0, g2["agent_id"]: 1}
    assert {e["message_id"] for e in out["events"]} == {guest_only["message_id"]}


def test_without_ownership_the_console_keeps_one_principal(monkeypatch, store):
    """Listing ``bob`` as someone else's puts him outside alice's scope."""
    a, b = store.register("alice"), store.register("bob")
    b2 = store.register("bob")
    to_alice = store.send(*creds(b, "bob"), to=a["agent_id"], text="x", request_id="one")
    between_bobs = store.send(*creds(b, "bob"), to=b2["agent_id"], text="x", request_id="two")
    app = owner_app(monkeypatch, store, not_owner=("bob", "guest"))
    _, out = snapshot(app, "fixture-alice")
    assert {e["message_id"] for e in out["events"]} == {to_alice["message_id"]}
    assert {r["agent_id"]: r["pending_count"] for r in out["agents"]}[b2["agent_id"]] is None
    _, out = snapshot(app, "fixture-bob")
    assert {e["message_id"] for e in out["events"]} == {to_alice["message_id"],
                                                        between_bobs["message_id"]}


# The pre-v59 timeline and count queries, verbatim, as the reference a
# one-principal scope must still match.
_REFERENCE_TIMELINE = (
    "WITH visible_mail AS MATERIALIZED ("
    "SELECT message_id FROM coordination_events WHERE event='send' "
    "AND principal>=%s AND principal<=%s UNION "
    "SELECT e.message_id FROM coordination_agents a CROSS JOIN LATERAL "
    "(SELECT message_id FROM coordination_events WHERE event='send' "
    "AND recipient_agent_id>=a.agent_id AND recipient_agent_id<=a.agent_id "
    "ORDER BY recipient_agent_id,seq) e WHERE a.principal=%s), "
    "candidates AS ("
    "(SELECT e.* FROM coordination_events e WHERE principal>=%s AND principal<=%s "
    "AND event IN ('send','read','ack','attempt','served','woke','lease_expire') "
    "ORDER BY principal DESC,seq DESC LIMIT 101) UNION "
    "(SELECT e.* FROM visible_mail v CROSS JOIN LATERAL "
    "(SELECT * FROM coordination_events WHERE message_id>=v.message_id AND message_id<=v.message_id "
    "AND event IN ('send','read','ack','attempt','served','woke','lease_expire') "
    "ORDER BY message_id,event,created_at OFFSET 0) e "
    "ORDER BY e.seq DESC LIMIT 101) UNION "
    "(SELECT e.* FROM coordination_events e WHERE e.event='expire' AND EXISTS "
    "(SELECT 1 FROM jsonb_array_elements_text(e.payload::jsonb->'message_ids') AS ids(id) "
    "JOIN visible_mail v ON v.message_id=ids.id) ORDER BY e.seq DESC LIMIT 101)), "
    "timeline AS (SELECT e.seq,e.event,e.created_at,e.agent_id,e.recipient_agent_id,"
    "e.message_id,e.project,e.task, "
    "CASE e.event WHEN 'send' THEN e.payload::jsonb->>'wake' "
    "WHEN 'read' THEN e.payload::jsonb->>'path' END AS detail, "
    "CASE WHEN e.event='expire' THEN (SELECT count(*) FROM "
    "jsonb_array_elements_text(e.payload::jsonb->'message_ids') AS ids(id) "
    "JOIN visible_mail v ON v.message_id=ids.id) ELSE 0 END AS expired_count "
    "FROM candidates e) SELECT * FROM timeline ORDER BY seq DESC LIMIT 101")
_REFERENCE_COUNTS = (
    "SELECT m.recipient_agent_id,count(*) AS n FROM coordination_messages m "
    "JOIN coordination_agents a ON a.agent_id=m.recipient_agent_id "
    "WHERE a.principal=%s AND m.acknowledged_at IS NULL AND m.expires_at>%s "
    "AND m.recipient_agent_id=ANY(%s) GROUP BY m.recipient_agent_id")
_DETAILS = {"hinted", "not_needed", "rung", "withheld", "nudged", "no_path", "capped", "pull",
            "delivery"}


def _reference(store, principal, agents):
    rows = store._all(_REFERENCE_TIMELINE, (principal,) * 5)
    events = [{**dict(r), "detail": r["detail"] if r["detail"] in _DETAILS else None}
              for r in rows[:100]]
    counts = {r["recipient_agent_id"]: r["n"] for r in store._all(
        _REFERENCE_COUNTS, (principal, store.clock(), [a["agent_id"] for a in agents]))}
    pending = {a["agent_id"]: counts.get(a["agent_id"], 0) if a["principal"] == principal else None
               for a in agents}
    return events, len(rows) > 100, pending


def _seed_board(store):
    a, a2 = store.register("alice"), store.register("alice")
    b, c = store.register("bob"), store.register("carol")
    early = store.send(*creds(a), to=c["agent_id"], text="x", request_id="early")
    store.send(*creds(b, "bob"), to=c["agent_id"], text="x", request_id="unrelated")
    for n in range(30):
        sender, to = [(a, b), (a2, c), (b, a), (c, a2)][n % 4]
        principal = {a["agent_id"]: "alice", a2["agent_id"]: "alice", b["agent_id"]: "bob",
                     c["agent_id"]: "carol"}[sender["agent_id"]]
        sent = store.send(*creds(sender, principal), to=to["agent_id"], text="x",
                          request_id=f"r{n}")
        store.test_time[0] += 1
        if n % 3 == 0:
            owner = {a["agent_id"]: "alice", a2["agent_id"]: "alice", b["agent_id"]: "bob",
                     c["agent_id"]: "carol"}[to["agent_id"]]
            store.receive(*creds(to, owner))
            store.ack(*creds(to, owner), message_id=sent["message_id"])
    store.receive(*creds(c, "carol"))
    store.ack(*creds(c, "carol"), message_id=early["message_id"])   # early mail, acked late
    store.test_time[0] += 86401
    store.prune()                                          # expiries, visible and not
    names = {a["agent_id"]: "alice", a2["agent_id"]: "alice", b["agent_id"]: "bob",
             c["agent_id"]: "carol"}
    for agent in (a, a2, b, c):                            # back in the active roster
        store.update(*creds(agent, names[agent["agent_id"]]), status="back")
    store.send(*creds(b, "bob"), to=a["agent_id"], text="x", request_id="pending-1")
    store.send(*creds(c, "carol"), to=a2["agent_id"], text="x", request_id="pending-2")
    store.send(*creds(a, "alice"), to=c["agent_id"], text="x", request_id="pending-3")
    with store.storage._txn():
        for _ in range(60):
            store._append([store._event("woke", {"rings": 1}, principal="alice",
                                        agent_id=a["agent_id"])], store.clock())
    return early


@pytest.mark.parametrize("principal", ["alice", "bob", "carol", "absent-principal"])
def test_a_one_principal_scope_matches_the_pre_v59_snapshot(store, principal):
    _seed_board(store)
    result = store.console_snapshot(principal)
    events, truncated, pending = _reference(store, principal, result["agents"])
    assert result["events"] == events
    assert result["events_truncated"] == truncated
    assert {a["agent_id"]: a["pending_count"] for a in result["agents"]} == pending
    assert len(result["agents"]) == 4
    if principal == "alice":
        assert truncated and {"send", "read", "ack", "expire", "woke"} <= {
            e["event"] for e in events}
    assert store.console_snapshot(principal, scope=[principal]) == result


def test_an_owner_scope_takes_the_newest_events_across_its_principals(store):
    """The cap is the globally newest by seq, not one principal's newest: an
    ``ORDER BY principal DESC, seq DESC LIMIT 101`` over the set would
    return only bob's."""
    a, b = store.register("alice"), store.register("bob")
    with store.storage._txn():
        for n in range(240):
            agent, principal = (a, "alice") if n % 2 else (b, "bob")
            store._append([store._event("woke", {"rings": 1}, principal=principal,
                                        agent_id=agent["agent_id"])], store.clock())
    expected = [r["seq"] for r in store._all(
        "SELECT seq FROM coordination_events WHERE event='woke' ORDER BY seq DESC LIMIT 100")]
    result = store.console_snapshot("alice", scope=["alice", "bob"])
    assert [e["seq"] for e in result["events"]] == expected
    assert result["events_truncated"]
    assert {e["agent_id"] for e in result["events"]} == {a["agent_id"], b["agent_id"]}


def _insert_events(store, rows):
    """Append synthetic audit rows after the current head (the snapshot
    reads them; the hash chain is not under test here)."""
    base = store.storage.conn.execute(
        "SELECT coalesce(max(seq),0) FROM coordination_events").fetchone()[0]
    for n, (event, agent, principal, recipient, message) in enumerate(rows, start=1):
        store.storage.conn.execute(
            "INSERT INTO coordination_events (seq,event,actor,principal,agent_id,"
            "recipient_agent_id,message_id,payload,created_at,prev_hash,hash) "
            "VALUES (%s,%s,'agent',%s,%s,%s,%s,'{}',%s,'','')",
            (base + n, event, principal, agent, recipient, message, 2000.0 + n))


def _busy_board(store):
    """alice's agent sends 150 messages to carol's and receives 150 from
    it, past the per-agent cap on both sides; then carol acks alice's
    oldest message and alice acks carol's oldest, both after everything."""
    a, c = store.register("alice"), store.register("carol")
    rows = [("send", a["agent_id"], "alice", c["agent_id"], f"out-{n}") for n in range(150)]
    rows += [("send", c["agent_id"], "carol", a["agent_id"], f"in-{n}") for n in range(150)]
    rows += [("ack", c["agent_id"], "carol", c["agent_id"], "out-0"),
             ("ack", a["agent_id"], "alice", a["agent_id"], "in-0")]
    _insert_events(store, rows)
    return a, c


def test_past_the_cap_only_a_late_outside_follow_up_of_old_mail_drops_out(store):
    """The documented exception, pinned so it stays deliberate: carol's
    late ack of a message older than the 101 newest sends of alice's agent
    drops out of alice's page, which the unbounded pre-v59 query showed;
    alice's own late ack (her principal's events) and everything else
    stays."""
    _busy_board(store)
    result = store.console_snapshot("alice")
    reference, _truncated, _pending = _reference(store, "alice", result["agents"])
    late = ("ack", "out-0")
    assert late in {(e["event"], e["message_id"]) for e in reference}
    assert late not in {(e["event"], e["message_id"]) for e in result["events"]}
    assert ("ack", "in-0") in {(e["event"], e["message_id"]) for e in result["events"]}
    assert [e for e in reference if (e["event"], e["message_id"]) != late] == \
        result["events"][:len(reference) - 1]


def test_visible_mail_is_capped_per_agent(store):
    """300 sends from and to alice's one agent: the visible-mail set holds
    at most the cap on each side. Without either LIMIT it would hold more."""
    from pseudolife_memory.storage.coordination import CONSOLE_EVENTS_CAP, CONSOLE_TIMELINE_SQL
    _busy_board(store)
    plan = store.storage.conn.execute(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + CONSOLE_TIMELINE_SQL,
        {"scope": ["alice"], "cap": CONSOLE_EVENTS_CAP}).fetchone()[0]

    def nodes(node):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from nodes(value)
        elif isinstance(node, list):
            for value in node:
                yield from nodes(value)
    visible = [n for n in nodes(plan) if n.get("Subplan Name") == "CTE visible_mail"]
    assert len(visible) == 1
    held = visible[0]["Actual Rows"] * visible[0]["Actual Loops"]
    assert 0 < held <= 2 * CONSOLE_EVENTS_CAP, held


def test_the_rust_daemon_runs_the_same_timeline_sql():
    from pathlib import Path
    from pseudolife_memory.storage.coordination import CONSOLE_TIMELINE_SQL
    shared = Path(__file__).resolve().parents[1] / "rust/daemon/src/board/console_timeline.sql"
    assert shared.read_text(encoding="utf-8").strip() == (
        CONSOLE_TIMELINE_SQL.replace("%(scope)s", "$1").replace("%(cap)s", "$2"))


def test_the_cap_is_per_agent_so_a_principal_with_two_busy_agents_matches_pre_v59(store):
    """alice's two agents send 80 each (160 by the principal, under the
    cap per agent): carol's late ack of the oldest still shows, as before
    v59. A cap per sending principal would have dropped it."""
    a1, a2, c = store.register("alice"), store.register("alice"), store.register("carol")
    rows = []
    for n in range(80):
        rows += [("send", a1["agent_id"], "alice", c["agent_id"], f"one-{n}"),
                 ("send", a2["agent_id"], "alice", c["agent_id"], f"two-{n}")]
    rows.append(("ack", c["agent_id"], "carol", c["agent_id"], "one-0"))
    _insert_events(store, rows)
    result = store.console_snapshot("alice")
    reference, truncated, _pending = _reference(store, "alice", result["agents"])
    assert ("ack", "one-0") in {(e["event"], e["message_id"]) for e in result["events"]}
    assert result["events"] == reference and result["events_truncated"] == truncated
