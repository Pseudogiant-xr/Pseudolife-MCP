"""An agent or message id may be given by a unique prefix of eight or more
hex characters.

Every surface shows an 8-12 character id prefix (the peer list, the per-turn
digest, the Console), and on 2026-09-26 seven sends bounced with
``recipient_not_found`` because a prefix was pasted as the address
(Coordination v2 design, Addressing E8). A prefix that matches one id
resolves to it; one that matches several is refused with the candidates'
distinguishing prefixes in the error's detail; one that matches none fails
the way the full id would. A value that is not eight to thirty-one lowercase
hex characters is looked up exactly, as before.
"""
import json

import pytest

from pseudolife_memory.coordination import PUBLIC_ERROR_CODES, dispatch, public_error
from pseudolife_memory.storage.coordination import (
    CoordinationError, distinguishing_prefixes, is_id_prefix,
)
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401
from tests.test_board_audit_cli import cli, lines  # noqa: F401
from tests.test_coordination_dispatch_isolation import PRINCIPAL, coordinating  # noqa: F401
from tests.test_coordination_storage import creds, pair, store  # noqa: F401

DAY = 86400
A_ID, B_ID = "deadbeefaa" + "0" * 22, "deadbeefab" + "0" * 22


def rename_agent(conn, agent, new_id):
    """Give a registered agent an id that shares a prefix with another;
    uuid4 never produces two that do."""
    conn.execute("UPDATE coordination_agents SET agent_id=%s WHERE agent_id=%s",
                 (new_id, agent["agent_id"]))
    return {**agent, "agent_id": new_id}


def rename_message(conn, message_id, new_id):
    conn.execute("UPDATE coordination_messages SET message_id=%s WHERE message_id=%s",
                 (new_id, message_id))
    conn.execute("UPDATE coordination_events SET message_id=%s WHERE message_id=%s",
                 (new_id, message_id))


def test_a_prefix_is_eight_to_thirty_one_lowercase_hex_characters():
    assert is_id_prefix("deadbeef") and is_id_prefix("0123456789abcdef0123456789abcde")
    for other in ("deadbee", "DEADBEEF", "deadbeefg", "a" * 32, "", None, 12345678,
                  "dead-beef", "deadbeef "):
        assert not is_id_prefix(other)
    assert distinguishing_prefixes([A_ID, B_ID]) == ["deadbeefaa", "deadbeefab"]
    assert distinguishing_prefixes(["deadbeefac" + "0" * 22, A_ID, B_ID]) == [
        "deadbeefaa", "deadbeefab", "deadbeefac"]
    # Never shorter than a prefix a caller may give.
    assert distinguishing_prefixes(["a" + "0" * 31, "b" + "0" * 31]) == ["a0000000", "b0000000"]


def test_a_unique_prefix_addresses_the_agent_and_the_receipt_names_it(store):
    a, b = pair(store)
    sent = store.send(*creds(a), to=b["agent_id"][:8], text="by prefix", request_id="r1")
    assert sent["recipient_agent_id"] == b["agent_id"] and sent["state"] == "queued"
    longer = store.send(*creds(a), to=b["agent_id"][:12], text="longer", request_id="r2")
    assert longer["recipient_agent_id"] == b["agent_id"]
    assert [m["text"] for m in store.receive(*creds(b))["messages"]] == ["by prefix", "longer"]
    # A retry by prefix is the same request as one by full id.
    assert store.send(*creds(a), to=b["agent_id"], text="by prefix", request_id="r1") == sent
    for short in (b["agent_id"][:7], b["agent_id"][:8].upper(), "missing-agent"):
        with pytest.raises(CoordinationError, match="recipient_not_found"):
            store.send(*creds(a), to=short, text="x", request_id="r3")


def test_an_ambiguous_prefix_is_refused_naming_the_candidates(store):
    sender, first, second = store.register("alice"), store.register("alice"), store.register("alice")
    first = rename_agent(store.storage.conn, first, A_ID)
    second = rename_agent(store.storage.conn, second, B_ID)
    with pytest.raises(CoordinationError, match="ambiguous_recipient") as refused:
        store.send(*creds(sender), to="deadbeef", text="x", request_id="r")
    assert refused.value.detail == "deadbeefaa, deadbeefab"
    assert store.send(*creds(sender), to="deadbeefab", text="x",
                      request_id="r")["recipient_agent_id"] == B_ID
    # An address whose credentials a restore revoked is not a candidate.
    store.storage.conn.execute("UPDATE coordination_agents SET credential_hash=NULL "
                               "WHERE agent_id=%s", (B_ID,))
    assert store.send(*creds(sender), to="deadbeef", text="y",
                      request_id="r2")["recipient_agent_id"] == A_ID


def test_reply_to_and_ack_take_a_prefix_scoped_to_the_callers_own_mail(store):
    a, b, c = store.register("alice"), store.register("alice"), store.register("alice")
    asked = store.send(*creds(a), to=b["agent_id"], text="q", request_id="q")
    reply = store.send(*creds(b), to=a["agent_id"][:8], text="a", request_id="a",
                       reply_to=asked["message_id"][:8])
    assert store.receive(*creds(a))["messages"][0]["reply_to"] == asked["message_id"]
    with pytest.raises(CoordinationError, match="invalid_reply"):
        store.send(*creds(c), to=a["agent_id"], text="spoof", request_id="s",
                   reply_to=asked["message_id"][:8])
    receipt = store.ack(*creds(a), message_id=reply["message_id"][:10])
    assert receipt["message_id"] == reply["message_id"] and receipt["state"] == "acknowledged"
    more = [store.send(*creds(a), to=b["agent_id"], text=f"n{i}", request_id=f"n{i}")["message_id"]
            for i in range(2)]
    batch = store.ack(*creds(b), message_id=f"{more[0][:8]},{more[1][:9]},{asked['message_id']},"
                                              "0123456789ab")
    assert [r["message_id"] for r in batch["receipts"]] == [more[0], more[1], asked["message_id"]]
    assert batch["missing"] == ["0123456789ab"]
    with pytest.raises(CoordinationError, match="message_not_found"):
        store.ack(*creds(b), message_id="0123456789ab")
    # A prefix of another mailbox's message is not this mailbox's to acknowledge.
    with pytest.raises(CoordinationError, match="message_not_found"):
        store.ack(*creds(c), message_id=more[0][:8])


def test_an_ambiguous_message_prefix_is_refused_naming_the_candidates(store):
    a, b = pair(store)
    ids = [store.send(*creds(a), to=b["agent_id"], text=f"m{i}", request_id=f"m{i}")["message_id"]
           for i in range(2)]
    rename_message(store.storage.conn, ids[0], "feedface01" + "0" * 22)
    rename_message(store.storage.conn, ids[1], "feedface02" + "0" * 22)
    with pytest.raises(CoordinationError, match="ambiguous_message_id") as refused:
        store.ack(*creds(b), message_id="feedface")
    assert refused.value.detail == "feedface01, feedface02"
    with pytest.raises(CoordinationError, match="ambiguous_message_id"):
        store.ack(*creds(b), message_id="feedface0,feedface02")
    with pytest.raises(CoordinationError, match="ambiguous_reply"):
        store.send(*creds(b), to=a["agent_id"], text="which?", request_id="w", reply_to="feedface")
    assert store.ack(*creds(b), message_id="feedface02")["message_id"] == "feedface02" + "0" * 22


def test_redact_takes_a_message_id_prefix(store):
    a, b = pair(store)
    sent = store.send(*creds(a), to=b["agent_id"], text="token pasted", request_id="r")
    out = store.redact(sent["message_id"][:8], "pasted a token")
    assert out["message_id"] == sent["message_id"] and out["audit_copy"] == "removed"
    with pytest.raises(CoordinationError, match="message_not_found"):
        store.redact("0123456789ab", "nothing there")
    ids = [store.send(*creds(a), to=b["agent_id"], text=f"m{i}", request_id=f"m{i}")["message_id"]
           for i in range(2)]
    rename_message(store.storage.conn, ids[0], "feedface01" + "0" * 22)
    rename_message(store.storage.conn, ids[1], "feedface02" + "0" * 22)
    with pytest.raises(CoordinationError, match="ambiguous_message_id") as refused:
        store.redact("feedface", "which?")
    assert refused.value.detail == "feedface01, feedface02"


def test_board_audit_export_takes_an_agent_prefix_even_after_the_address_is_pruned(store, cli):
    a, b = pair(store)
    store.send(*creds(a), to=b["agent_id"], text="hello", request_id="r")
    code, output = cli("export", "--agent", b["agent_id"][:8])
    assert code == 0 and [e["event"] for e in lines(output)] == ["register", "send"]
    store.test_time[0] += 8 * DAY
    store.prune()
    assert store.storage.conn.execute("SELECT count(*) FROM coordination_agents").fetchone()[0] == 0
    code, output = cli("export", "--agent", b["agent_id"][:8])
    assert code == 0 and [e["event"] for e in lines(output)] == ["register", "send"]
    code, output = cli("export", "--agent", "0123456789ab")
    assert code != 0 and "no agent id starts with 0123456789ab" in output.err


def test_board_audit_export_refuses_an_ambiguous_agent_prefix_naming_the_candidates(store, cli):
    a, b = pair(store)
    rename_agent(store.storage.conn, a, A_ID)
    rename_agent(store.storage.conn, b, B_ID)
    code, output = cli("export", "--agent", "deadbeef")
    assert code != 0 and output.out == ""
    assert "deadbeefaa, deadbeefab" in output.err


def test_recovery_rebind_takes_an_agent_prefix(store, pg_url, tmp_path, monkeypatch, capsys):
    from tests.test_coordination_recovery import invoke, settings
    a, b = pair(store)
    a = rename_agent(store.storage.conn, a, A_ID)
    b = rename_agent(store.storage.conn, b, B_ID)
    config = settings(tmp_path)
    assert invoke(monkeypatch, pg_url, config, "recover", "--confirm-restore") == 0
    options = ["--principal", "alice", "--bank-url", "http://127.0.0.1:8099"]
    assert invoke(monkeypatch, pg_url, config, "rebind", "--agent", "deadbeef", *options,
                  "--state", str(tmp_path / "ambiguous.json")) == 1
    assert "deadbeefaa, deadbeefab" in capsys.readouterr().err
    path = tmp_path / "rebound.json"
    assert invoke(monkeypatch, pg_url, config, "rebind", "--agent", "deadbeefab", *options,
                  "--state", str(path)) == 0
    assert json.loads(path.read_text())["agent_id"] == B_ID


def test_the_refusal_and_its_detail_reach_dispatch_callers(coordinating):
    """The MCP tool raises ``dispatch``'s error as its text, so the detail
    rides after the code; the code alone stays the stable public one."""
    sender = dispatch(coordinating, "register", {}, headers={}, principal=PRINCIPAL)
    mine = {"x-pl-agent": sender["agent_id"], "x-pl-agent-key": sender["credential"]}
    conn = coordinating._coordination_storage.conn
    for new_id in (A_ID, B_ID):
        rename_agent(conn, dispatch(coordinating, "register", {}, headers={}, principal=PRINCIPAL),
                     new_id)
    with pytest.raises(ValueError) as refused:
        dispatch(coordinating, "send", {"to": "deadbeef", "text": "x", "request_id": "r"},
                 headers=mine, principal=PRINCIPAL)
    assert str(refused.value) == "ambiguous_recipient: deadbeefaa, deadbeefab"
    assert public_error(refused.value) == "ambiguous_recipient"
    assert {"ambiguous_recipient", "ambiguous_message_id", "ambiguous_reply",
            "fanout_too_large", "no_recipients"} <= PUBLIC_ERROR_CODES
    sent = dispatch(coordinating, "send", {"to": "deadbeefaa", "text": "x", "request_id": "r"},
                    headers=mine, principal=PRINCIPAL)
    assert sent["recipient_agent_id"] == A_ID


def test_the_refusal_and_its_detail_reach_rest_callers(pg_conn, pg_url):
    import httpx
    from tests.test_coordination_leases_api import _app, _post, _register, _run

    storage, app = _app(pg_url)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            sender = await _register(client, "sender")
            for label, new_id in (("a", A_ID), ("b", B_ID)):
                peer = await _register(client, label)
                rename_agent(storage.conn, {"agent_id": peer["X-PL-Agent"]}, new_id)
            refused = await _post(client, sender, "send",
                                  {"to": "deadbeef", "text": "x", "request_id": "r"})
            assert refused.status_code == 400
            assert refused.json() == {"error": "ambiguous_recipient",
                                      "detail": "deadbeefaa, deadbeefab"}
            sent = await _post(client, sender, "send",
                               {"to": "deadbeefab", "text": "x", "request_id": "r"})
            assert sent.status_code == 200 and sent.json()["recipient_agent_id"] == B_ID
    _run(storage, drive)
