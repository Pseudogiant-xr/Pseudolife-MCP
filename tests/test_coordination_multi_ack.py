"""Acknowledging several messages in one call.

``message_id`` stays a string because Claude Code stringifies list
parameters (2026-07-11 finding): several ids are comma-separated. A single id
keeps the receipt it always returned; several return the receipts in the
order given plus the ids that were not this mailbox's to acknowledge. A
cursor is still never an acknowledgement.
"""
import json

import pytest

from pseudolife_memory.coordination import dispatch
from pseudolife_memory.storage.coordination import MAX_PAGE, CoordinationError
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_coordination_storage import creds, store  # noqa: F401


def _mail(store, sender, recipient, n):
    return [store.send(*creds(sender), to=recipient["agent_id"], text=f"note {i}",
                       request_id=f"r{i}")["message_id"] for i in range(n)]


def test_several_ids_are_acknowledged_in_order_and_unknown_ones_reported(store):
    me, peer, other = store.register("alice"), store.register("alice"), store.register("alice")
    mine = _mail(store, peer, me, 3)
    theirs = store.send(*creds(peer), to=other["agent_id"], text="not yours", request_id="o1")["message_id"]
    store.test_time[0] += 5
    out = store.ack(*creds(me), message_id=f"{mine[2]}, {mine[0]},{theirs},{mine[0]},missing-id")
    assert [r["message_id"] for r in out["receipts"]] == [mine[2], mine[0]]
    assert all(r["state"] == "acknowledged" and r["acknowledged_at"] == 1005.0 for r in out["receipts"])
    assert out["missing"] == [theirs, "missing-id"]
    # Acknowledging is activity for retention, as for a single id (read
    # before anything else touches the row).
    assert _last_activity(store, me) == 1005.0
    # The other mailbox's message is untouched; the middle one is still pending.
    assert _pending(store, other) == 1 and _pending(store, me) == 1


def test_a_list_a_host_stringified_is_read_as_that_list(store):
    """Claude Code stringifies list parameters, so a model that passes a
    list arrives as '["a", "b"]'; that form must acknowledge, not land two
    bracketed non-ids in `missing`."""
    me, peer = store.register("alice"), store.register("alice")
    mine = _mail(store, peer, me, 2)
    out = store.ack(*creds(me), message_id=json.dumps(mine))
    assert [r["message_id"] for r in out["receipts"]] == mine and out["missing"] == []
    out = store.ack(*creds(me), message_id=json.dumps([mine[0]]))
    assert [r["message_id"] for r in out["receipts"]] == [mine[0]]
    for bad in ('["a", 1]', '[not json', '{"message_id": "a"}', '[]', '["a b"]'):
        with pytest.raises(CoordinationError, match="invalid_message_id"):
            store.ack(*creds(me), message_id=bad)


def _last_activity(store, agent):
    return store.storage.conn.execute("SELECT last_activity FROM coordination_agents WHERE agent_id=%s",
                                      (agent["agent_id"],)).fetchone()[0]


def _pending(store, agent):
    return store.storage.conn.execute(
        "SELECT count(*) FROM coordination_messages WHERE recipient_agent_id=%s AND acknowledged_at IS NULL",
        (agent["agent_id"],)).fetchone()[0]


def test_a_single_id_keeps_its_receipt_shape_and_errors(store):
    me, peer = store.register("alice"), store.register("alice")
    [only] = _mail(store, peer, me, 1)
    receipt = store.ack(*creds(me), message_id=only)
    assert set(receipt) == {"message_id", "state", "created_at", "expires_at", "acknowledged_at"}
    assert receipt["state"] == "acknowledged"
    store.test_time[0] += 5
    with pytest.raises(CoordinationError, match="message_not_found"):
        store.ack(*creds(me), message_id="missing-id")
    # A failed single ack rolls back with its activity bump, as before.
    assert _last_activity(store, me) == 1000.0


@pytest.mark.parametrize("bad", ["", " ", "a,,b", ",a", "a,", ",", "a\nb,c", "a;b", "a b,c", "a/b"])
def test_malformed_lists_are_rejected_whole(store, bad):
    me, peer = store.register("alice"), store.register("alice")
    [only] = _mail(store, peer, me, 1)
    with pytest.raises(CoordinationError, match="invalid_message_id"):
        store.ack(*creds(me), message_id=bad.replace("a", only))
    assert _pending(store, me) == 1


def test_the_batch_is_bounded_like_a_receive_page(store):
    me, peer = store.register("alice"), store.register("alice")
    ids = [f"id{i}" for i in range(MAX_PAGE + 1)]
    with pytest.raises(CoordinationError, match="invalid_message_id"):
        store.ack(*creds(me), message_id=",".join(ids))
    out = store.ack(*creds(me), message_id=",".join(ids[:MAX_PAGE]))
    assert out == {"receipts": [], "missing": ids[:MAX_PAGE]}


def test_dispatch_and_the_tool_accept_a_comma_separated_list(coordinating):
    def headers(agent):
        return {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}
    me = dispatch(coordinating, "register", {}, headers={}, principal=PRINCIPAL)
    peer = dispatch(coordinating, "register", {}, headers={}, principal=PRINCIPAL)
    ids = [dispatch(coordinating, "send", {"to": me["agent_id"], "text": f"n{i}", "request_id": f"d{i}"},
                    headers=headers(peer), principal=PRINCIPAL)["message_id"] for i in range(2)]
    out = dispatch(coordinating, "ack", {"message_id": ",".join(ids)}, headers=headers(me), principal=PRINCIPAL)
    assert [r["state"] for r in out["receipts"]] == ["acknowledged", "acknowledged"]
    assert out["missing"] == []


def test_tool_docstring_names_the_list_form(tmp_path, monkeypatch):
    """The tier budget itself is enforced by test_tool_consolidation."""
    from tests.test_tool_consolidation import _reload
    import asyncio
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    assert "comma-separated" in tools["memory_message"].description
