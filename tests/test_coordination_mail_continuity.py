"""Mailbox gaps and retained history preserve delivery and access boundaries."""
import pytest

from pseudolife_memory.storage.coordination import CoordinationError, CoordinationStore
from tests.test_coordination_storage import creds, pair, store  # noqa: F401
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

DAY = 86400


@pytest.mark.parametrize("peer", [None, "absent-peer"])
def test_history_uses_participant_and_lifecycle_indexes_with_unrelated_audit(store, monkeypatch, peer):
    import json
    a, b = pair(store)
    sent = store.send(*creds(a), to=b["agent_id"], text="own mail", request_id="own")
    store.receive(*creds(b))
    store.ack(*creds(b), message_id=sent["message_id"])
    store.storage.conn.execute(
        "INSERT INTO coordination_events "
        "(seq,event,actor,principal,agent_id,recipient_agent_id,message_id,payload,created_at,prev_hash,hash) "
        "SELECT 1000+n,'send','agent','unrelated','other-sender','other-recipient',"
        "'other-'||n,'{}',%s,'fixture','fixture' FROM generate_series(1,3000) n", (store.clock(),))
    store.storage.conn.execute(
        "INSERT INTO coordination_events "
        "(seq,event,actor,principal,agent_id,recipient_agent_id,message_id,payload,created_at,prev_hash,hash) "
        "SELECT 5000+n,'send','agent','alice',%s,'other-peer','own-'||n,'{}',%s,"
        "'fixture','fixture' FROM generate_series(1,3000) n", (b['agent_id'], store.clock()))
    store.storage.conn.execute("ANALYZE coordination_events")
    plans = []
    for name in ("_all", "_one"):
        original = getattr(store, name)

        def instrument(query, params=(), original=original):
            if "coordination_events" in query:
                with store.storage.conn.transaction():
                    plan = store.storage.conn.execute(
                        "EXPLAIN (ANALYZE, FORMAT JSON) " + query, params).fetchone()[0]
                    plans.append(json.dumps(plan))
            return original(query, params)

        monkeypatch.setattr(store, name, instrument)
    result = store.history(*creds(b), after=f"{b['agent_id']}:history:0",
                           peer=peer, limit=1, audit_retention_days=0)
    assert [m["message_id"] for m in result["messages"]] == ([sent["message_id"]] if peer is None else [])
    rendered = " ".join(plans)
    print(rendered)
    assert "coordination_events_send_recipient_idx" in rendered
    assert "coordination_events_send_sender_idx" in rendered
    if peer is None:
        assert "coordination_events_message_idx" in rendered
    else:
        assert "coordination_events_send_pair_idx" in rendered
    # The global unrelated rows must not be filtered after an audit-wide scan.
    def removed(node):
        if isinstance(node, dict):
            return [node.get("Rows Removed by Filter", 0),
                    *[n for value in node.values() for n in removed(value)]]
        if isinstance(node, list):
            return [n for value in node for n in removed(value)]
        return []
    assert max(removed( [json.loads(plan) for plan in plans]), default=0) < 100


def test_receive_distinguishes_empty_expired_and_acknowledged(store):
    a, b = pair(store)
    assert store.receive(*creds(b))["continuity"]["status"] == "empty"
    sent = store.send(*creds(a), to=b["agent_id"], text="review request", request_id="one")
    assert store.receive(*creds(b))["continuity"]["status"] == "messages"
    store.test_time[0] += DAY
    for prune in (False, True):
        if prune:
            store.prune()
        got = store.receive(*creds(b))
        assert got["messages"] == []
        assert got["continuity"]["status"] == "expired_unacknowledged"
        assert got["continuity"]["expired_unacknowledged"] == 1
        assert "review request" not in str(got)
    assert store.ack(*creds(b), message_id=sent["message_id"])["state"] == "acknowledged"
    assert store.receive(*creds(b))["continuity"]["status"] == "empty"


def test_receive_explains_ahead_cursor_without_changing_error_code(store):
    _, b = pair(store)
    with pytest.raises(CoordinationError) as raised:
        store.receive(*creds(b), after=f"{b['agent_id']}:1")
    assert raised.value.code == "invalid_cursor"
    assert raised.value.detail == "cursor_ahead"
    with pytest.raises(CoordinationError) as raised:
        store.receive(*creds(b), after="another-mailbox:1")
    assert raised.value.code == "invalid_cursor"
    assert raised.value.detail != "cursor_ahead"


def test_receive_retention_gap_and_cursor_scope_survive_restart(store):
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="old request", request_id="old")
    store.test_time[0] += 8 * DAY
    # Keep the mailbox identity alive, as an attached adapter would.
    store.update(*creds(b), status="returned")
    store.prune()
    restarted = CoordinationStore(store.storage, clock=store.clock)
    got = restarted.receive(*creds(b))
    assert got["messages"] == []
    assert got["continuity"]["status"] == "retention_gap"
    assert got["continuity"]["metadata_gap"] is True
    assert got["continuity"]["expired_unacknowledged"] == 0
    assert restarted.receive(*creds(b), after=f"{b['agent_id']}:1")["continuity"]["status"] == "empty"


def test_receive_expiry_counts_are_after_cursor_and_do_not_hide_live_mail(store):
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="old", request_id="old")
    store.test_time[0] += DAY
    store.send(*creds(a), to=b["agent_id"], text="new", request_id="new")
    got = store.receive(*creds(b), limit=1)
    assert [m["text"] for m in got["messages"]] == ["new"]
    assert got["continuity"]["expired_unacknowledged"] == 1
    assert store.receive(*creds(b), after=got["after"])["continuity"]["expired_unacknowledged"] == 0


def test_history_is_visible_only_to_authenticated_participants(store):
    a, b = pair(store)
    other = store.register("bob")
    sibling = store.register("alice")
    sent = store.send(*creds(a), to=b["agent_id"], text="review request", request_id="one")
    for participant in (a, b):
        [item] = store.history(*creds(participant))["messages"]
        assert item["message_id"] == sent["message_id"]
        assert item["text"] == "review request"
        assert item["participants"] == sorted([a["agent_id"], b["agent_id"]])
        assert "body_salt" not in item and "text_commitment" not in item
    assert store.history(*creds(other, "bob"))["messages"] == []
    assert store.history(*creds(sibling))["messages"] == []
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.history(*creds(b, "bob"))
    with pytest.raises(CoordinationError, match="invalid_credential"):
        store.history("alice", b["agent_id"], a["credential"])


def test_history_cross_principal_addressed_mail_is_visible_to_recipient(store):
    a = store.register("alice")
    b = store.register("bob")
    store.send(*creds(a), to=b["agent_id"], text="addressed request", request_id="one")
    assert store.history(*creds(b, "bob"))["messages"][0]["text"] == "addressed request"


def test_history_does_not_read_ack_wake_or_requeue_mail(store):
    a, b = pair(store)
    sent = store.send(*creds(a), to=b["agent_id"], text="review request", request_id="one")
    before = store.storage.conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0]
    assert store.history(*creds(b))["messages"][0]["state"] == "pending"
    assert store.storage.conn.execute("SELECT first_read_at,acknowledged_at,attempts FROM coordination_messages").fetchone() == (None, None, 0)
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_events").fetchone()[0] == before
    store.ack(*creds(b), message_id=sent["message_id"])
    store.test_time[0] += 8 * DAY
    store.update(*creds(b), status="returned")
    store.prune()
    restarted = CoordinationStore(store.storage, clock=store.clock)
    [item] = restarted.history(*creds(b))["messages"]
    assert item["state"] == "acknowledged"
    assert item["text"] == "review request"
    assert restarted.receive(*creds(b))["messages"] == []


def test_history_redaction_and_expiry_have_separate_body_states(store):
    a, b = pair(store)
    sent = store.send(*creds(a), to=b["agent_id"], text="review request", request_id="one")
    store.test_time[0] += DAY
    store.prune()
    [item] = store.history(*creds(b))["messages"]
    assert (item["state"], item["body_state"], item["text"]) == ("expired", "retained", "review request")
    store.redact(sent["message_id"], "operator request")
    for participant in (a, b):
        [item] = store.history(*creds(participant))["messages"]
        assert (item["body_state"], item["text"]) == ("redacted", None)


@pytest.mark.parametrize("legacy", [False, True])
def test_history_redaction_matches_live_expiry_even_for_legacy_bodies(store, monkeypatch, legacy):
    from tests.test_coordination_audit import _sent_by_v45
    a, b = pair(store)
    sent = (_sent_by_v45(monkeypatch, store, a, b, "review request") if legacy else
            store.send(*creds(a), to=b["agent_id"], text="review request", request_id="one"))
    store.test_time[0] += 1
    store.redact(sent["message_id"], "operator request")
    [item] = store.history(*creds(b))["messages"]
    assert item["text"] is None
    assert item["body_state"] == "redacted"
    assert item["state"] == "expired"
    assert item["expires_at"] == store.test_time[0]
    assert store.receive(*creds(b))["messages"] == []
    store.test_time[0] += 8 * DAY
    store.update(*creds(b), status="returned")
    store.prune()
    [item] = store.history(*creds(b))["messages"]
    assert item["text"] is None and item["state"] == "expired"


def test_history_fresh_connection_retains_mail_and_recovery_revokes_access(store, pg_url):
    import psycopg
    from tests.test_coordination_storage import Storage
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="retained request", request_id="one")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        resumed = CoordinationStore(Storage(conn), clock=store.clock)
        assert resumed.history(*creds(b))["messages"][0]["text"] == "retained request"
        resumed.recover()
        with pytest.raises(CoordinationError, match="invalid_credential"):
            resumed.history(*creds(b))


def test_history_ahead_cursor_is_explained(store):
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="request", request_id="one")
    with pytest.raises(CoordinationError) as raised:
        store.history(*creds(b), after=f"{b['agent_id']}:history:999999")
    assert raised.value.code == "invalid_cursor"
    assert raised.value.detail == "cursor_ahead"


def test_history_cursor_validation_does_not_use_other_principals_events(store):
    a, b = pair(store)
    c, d = store.register("bob"), store.register("bob")
    store.send(*creds(a), to=b["agent_id"], text="request", request_id="one")
    page = store.history(*creds(b))
    seq = int(page["after"].rsplit(":", 1)[1])
    store.send(*creds(c, "bob"), to=d["agent_id"], text="unrelated request", request_id="two")
    with pytest.raises(CoordinationError) as raised:
        store.history(*creds(b), after=f"{b['agent_id']}:history:{seq + 1}")
    assert raised.value.detail == "cursor_ahead"
    # An authentic saved cursor remains usable when its whole slice is cut.
    store.test_time[0] += 3 * DAY
    store.prune(audit_retention_days=1)
    assert store.history(*creds(b), after=page["after"])["messages"] == []


def test_history_pagination_pair_filter_and_cursor_scope(store):
    a, b = pair(store)
    c = store.register("alice")
    first = store.send(*creds(a), to=b["agent_id"], text="first", request_id="one")
    reply = store.send(*creds(b), to=a["agent_id"], text="reply", request_id="two", reply_to=first["message_id"])
    store.send(*creds(c), to=b["agent_id"], text="other pair", request_id="three")
    page = store.history(*creds(b), peer=a["agent_id"], limit=1)
    assert [m["message_id"] for m in page["messages"]] == [first["message_id"]]
    assert page["has_more"] is True
    restarted = CoordinationStore(store.storage, clock=store.clock)
    page2 = restarted.history(*creds(b), peer=a["agent_id"], limit=1, after=page["after"])
    assert [m["message_id"] for m in page2["messages"]] == [reply["message_id"]]
    assert page2["messages"][0]["reply_to"] == first["message_id"]
    assert page2["has_more"] is False
    assert restarted.history(*creds(b), peer=a["agent_id"], after=page2["after"])["messages"] == []
    with pytest.raises(CoordinationError, match="invalid_cursor"):
        restarted.history(*creds(a), after=page["after"])
    with pytest.raises(CoordinationError, match="invalid_cursor"):
        restarted.receive(*creds(b), after=page["after"])


def test_history_respects_configured_retention_before_prune(store):
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="old", request_id="one")
    store.test_time[0] += 3 * DAY
    assert store.history(*creds(b), audit_retention_days=1)["messages"] == []
    assert store.history(*creds(b), audit_retention_days=0)["messages"][0]["text"] == "old"
    store.prune(audit_retention_days=1)
    assert store.history(*creds(b), audit_retention_days=0)["messages"] == []


@pytest.mark.parametrize("limit", [0, 51, True, "1"])
def test_history_rejects_invalid_page_limits(store, limit):
    _, b = pair(store)
    with pytest.raises(CoordinationError, match="invalid_limit"):
        store.history(*creds(b), limit=limit)


def test_history_dispatch_uses_server_retention_and_has_no_delivery_effects(store, monkeypatch):
    import threading
    from pseudolife_memory import coordination
    from tests.test_coordination_auth import service
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="old", request_id="one")
    store.test_time[0] += 3 * DAY
    svc = service(allowed=["alice"])
    svc.config.coordination.audit_retention_days = 1
    svc._coordination_lock = threading.RLock()
    svc._coordination_notifier = lambda *_: pytest.fail("history must not notify")
    monkeypatch.setattr(coordination, "_store", lambda _: store)
    def durable_only(_, *, full):
        assert full is False, "history must not initialize models or memory extraction"
    monkeypatch.setattr(coordination, "_ensure_tier", durable_only)
    headers = {"x-pl-agent": b["agent_id"], "x-pl-agent-key": b["credential"]}
    out = coordination.dispatch(svc, "history", {}, headers=headers, principal="alice")
    assert out["messages"] == [] and out["retention_days"] == 1
    assert "historical" in out["note"] and "not pending mail or dream input" in out["note"]
    for extra in ({"audit_retention_days": 0}, {"agent_id": a["agent_id"]}, {"for_delivery": True}):
        with pytest.raises(ValueError, match="unexpected_parameter"):
            coordination.dispatch(svc, "history", extra, headers=headers, principal="alice")
    with pytest.raises(ValueError, match="instance_authentication_required"):
        coordination.dispatch(svc, "history", {}, headers={}, principal="alice")


def test_history_rest_authentication_and_ahead_cursor_details(store, monkeypatch):
    import json
    import threading
    from pseudolife_memory import coordination
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.web.fixtures import FixtureService
    from tests.asgi_helpers import call, stub_mcp
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="addressed request", request_id="one")
    svc = FixtureService()
    svc.config.coordination.enabled = True
    svc.config.coordination.allowed_principals = ["alice", "bob"]
    svc._coordination_lock = threading.RLock()
    monkeypatch.setattr(coordination, "_store", lambda _: store)
    monkeypatch.setattr(coordination, "_ensure_tier", lambda _, **__: None)
    app = build_console_app(stub_mcp, None, lambda: {}, svc,
                            token_map={"fixture-alice": "alice", "fixture-bob": "bob"})
    def request(action, token=None, body=b"{}"):
        headers = [(b"content-type", b"application/json"),
                   (b"x-pl-agent", b["agent_id"].encode()),
                   (b"x-pl-agent-key", b["credential"].encode())]
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        status, raw = call(app, "POST", f"/api/coordination/{action}", body=body, headers=headers)
        return status, json.loads(raw)
    status, out = request("history", "fixture-alice")
    assert status == 200 and out["messages"][0]["text"] == "addressed request"
    status, out = request("history", "fixture-bob")
    assert status == 403 and out["error"] == "invalid_credential"
    assert request("history")[0] == 401
    status, out = request("receive", "fixture-alice",
                          json.dumps({"after": f"{b['agent_id']}:999"}).encode())
    assert status == 400 and out == {"error": "invalid_cursor", "detail": "cursor_ahead"}
