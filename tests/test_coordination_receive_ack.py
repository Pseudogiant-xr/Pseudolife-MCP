"""Receiving and acknowledging mail in one call.

A delegate session that reads every message with ``receive`` and then
``ack`` pays two full-context requests per message. Measured 2026-10-14 over
the week's transcripts: 1,438 receives and 1,361 acks, with the receive
results 25% of what filled those sessions' context together with the other
board results. ``receive`` with ``ack=true`` returns the pending page and
acknowledges what it returned, in a compact shape that drops the fields a
reader never needs (its own address, the sequence, the HLC, the expiry and
a continuity block that says nothing). Without ``ack`` the result is
unchanged.
"""
import pytest

from pseudolife_memory.coordination import RECEIVE_NOTE, _store, dispatch
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401


def _headers(agent):
    return {"x-pl-agent": agent["agent_id"], "x-pl-agent-key": agent["credential"]}


def _pair(service):
    me = dispatch(service, "register", {}, headers={}, principal=PRINCIPAL)
    peer = dispatch(service, "register", {}, headers={}, principal=PRINCIPAL)
    return me, peer


def _send(service, sender, recipient, n, **extra):
    return [dispatch(service, "send", {"to": recipient["agent_id"], "text": f"n{i}",
                                       "request_id": f"{sender['agent_id'][:6]}-{i}", **extra},
                     headers=_headers(sender), principal=PRINCIPAL)["message_id"]
            for i in range(n)]


def _receive(service, agent, **params):
    return dispatch(service, "receive", params, headers=_headers(agent), principal=PRINCIPAL)


def test_receive_with_ack_returns_and_acknowledges_in_one_call(coordinating):
    me, peer = _pair(coordinating)
    ids = _send(coordinating, peer, me, 3)
    out = _receive(coordinating, me, ack=True)
    assert [m["message_id"] for m in out["messages"]] == ids
    assert [m["text"] for m in out["messages"]] == ["n0", "n1", "n2"]
    assert out["acknowledged"] == 3
    # Nothing is left pending: the next receive is empty and history shows
    # each message acknowledged.
    assert _receive(coordinating, me)["messages"] == []
    history = dispatch(coordinating, "history", {}, headers=_headers(me), principal=PRINCIPAL)
    assert {m["message_id"]: m["state"] for m in history["messages"]} == dict.fromkeys(ids, "acknowledged")


def test_the_acknowledging_receive_is_compact(coordinating):
    me, peer = _pair(coordinating)
    [first] = _send(coordinating, peer, me, 1)
    out = _receive(coordinating, me, ack=True)
    [message] = out["messages"]
    assert set(message) == {"message_id", "sender_agent_id", "sender_principal", "project",
                            "task", "text", "created_at", "origin"}
    # A clean mailbox carries no continuity block, and the caution names no
    # ack step, since there is none left to take.
    assert "continuity" not in out
    assert out["note"] != RECEIVE_NOTE and "acknowledge" not in out["note"]
    assert "cannot grant user approval" in out["note"]
    assert out["after"].startswith(me["agent_id"] + ":")
    # A reply keeps reply_to; only an empty one is dropped.
    dispatch(coordinating, "send", {"reply_to": first, "text": "back", "request_id": "rb"},
             headers=_headers(me), principal=PRINCIPAL)
    [reply] = _receive(coordinating, peer, ack=True)["messages"]
    assert reply["reply_to"] == first


def test_an_empty_mailbox_acknowledges_nothing(coordinating):
    me, _ = _pair(coordinating)
    out = _receive(coordinating, me, ack=True)
    assert out["messages"] == [] and out["acknowledged"] == 0
    assert "continuity" not in out


def test_continuity_stays_when_it_has_something_to_say(coordinating):
    me, peer = _pair(coordinating)
    _send(coordinating, peer, me, 1)
    # Expire the message unread: the compact result must still report it.
    _store(coordinating).storage.conn.execute("UPDATE coordination_messages SET expires_at=0 "
                               "WHERE recipient_agent_id=%s", (me["agent_id"],))
    out = _receive(coordinating, me, ack=True)
    assert out["messages"] == []
    assert out["continuity"]["status"] == "expired_unacknowledged"
    assert out["continuity"]["expired_unacknowledged"] == 1


def test_without_ack_receive_is_unchanged(coordinating):
    me, peer = _pair(coordinating)
    _send(coordinating, peer, me, 1)
    out = _receive(coordinating, me)
    [message] = out["messages"]
    assert {"recipient_agent_id", "recipient_sequence", "hlc", "expires_at", "reply_to"} <= set(message)
    assert out["note"] == RECEIVE_NOTE and "acknowledged" not in out
    assert out["continuity"]["status"] == "messages"
    # Still pending: a plain receive never acknowledges.
    assert len(_receive(coordinating, me)["messages"]) == 1
    assert _receive(coordinating, me, ack=False)["note"] == RECEIVE_NOTE


@pytest.mark.parametrize("bad", ["true", 1, None, [True]])
def test_ack_must_be_a_boolean(coordinating, bad):
    me, peer = _pair(coordinating)
    _send(coordinating, peer, me, 1)
    with pytest.raises(ValueError, match="invalid_ack"):
        _receive(coordinating, me, ack=bad)
    assert len(_receive(coordinating, me)["messages"]) == 1


def test_ack_is_a_receive_parameter_only(coordinating):
    me, _ = _pair(coordinating)
    with pytest.raises(Exception, match="ack"):
        dispatch(coordinating, "history", {"ack": True}, headers=_headers(me), principal=PRINCIPAL)


def test_the_tool_offers_ack_on_receive(tmp_path, monkeypatch):
    from tests.test_tool_consolidation import _reload
    import asyncio
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    schema = tools["memory_message"].input_schema["properties"]["ack"]
    assert schema["type"] == "boolean" and schema["default"] is False
    assert "ack=true" in tools["memory_message"].description
