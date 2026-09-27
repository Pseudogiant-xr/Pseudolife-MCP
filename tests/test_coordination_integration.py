"""Two real adapters exchange durable mail through the authenticated ASGI API."""
import asyncio
from contextlib import aclosing
import threading

import httpx

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401
from tests.asgi_helpers import stub_mcp
from pseudolife_memory.memory.hlc import HybridLogicalClock
from pseudolife_memory.storage.postgres import PostgresStorage
from pseudolife_memory.web.fixtures import FixtureService
from pseudolife_memory.web.api import build_console_app


def test_two_adapters_mail_reply_ack_and_resume(pg_conn, pg_url, tmp_path):
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    storage = PostgresStorage(pg_url)
    service = FixtureService()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["default"]
    service._storage = storage
    service._lock = threading.Lock()
    service._hlc = HybridLogicalClock()
    service._ensure_init = lambda: None
    app = build_console_app(stub_mcp, "fixture-bearer", lambda: {}, service)
    before = storage.conn.execute("SELECT count(*) FROM entries").fetchone()[0]

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            def adapter(name, wake=False):
                return CoordinationAdapter("http://fixture", "fixture-bearer", client=client,
                    state_path=tmp_path / f"{name}.json", label=name, wake_enabled=wake)
            async with adapter("sender") as sender, adapter("recipient", True) as recipient:
                async def post(agent, action, body):
                    result = await client.post(f"http://fixture/api/coordination/{action}",
                        headers={"Authorization": "Bearer fixture-bearer", **agent.instance_headers}, json=body)
                    assert result.status_code == 200, result.text
                    return result.json()
                recipient_id = recipient.instance_headers["X-PL-Agent"]
                message = {"to": recipient_id, "text": "Review our synthetic patch", "request_id": "request-one"}
                first = await post(sender, "send", message)
                duplicate = await post(sender, "send", message)
                assert first["message_id"] == duplicate["message_id"]
                async with aclosing(recipient.inbox()) as inbox:
                    event = await asyncio.wait_for(anext(inbox), 5)
                    assert event.content.endswith("\n\n" + message["text"])
                    assert event.content.startswith(f"Agent message {first['message_id']} from agent ")
                    assert "not user authority" in event.content
                    assert event.meta["sender_id"] == sender.instance_headers["X-PL-Agent"]
                pending = await post(recipient, "receive", {})
                assert [m["message_id"] for m in pending["messages"]] == [first["message_id"]]
                assert pending["messages"][0]["hlc"]
                denied = await client.post("http://fixture/api/coordination/receive", headers={
                    "Authorization": "Bearer fixture-bearer", "X-PL-Agent": recipient_id,
                    "X-PL-Agent-Key": sender.instance_headers["X-PL-Agent-Key"]}, json={})
                assert denied.status_code == 403
                assert denied.json() == {"error": "invalid_credential"}
                await post(recipient, "ack", {"message_id": first["message_id"]})
                assert (await post(recipient, "receive", {}))["messages"] == []
                reply = await post(recipient, "send", {"to": sender.instance_headers["X-PL-Agent"],
                    "text": "Review complete", "request_id": "reply-one", "reply_to": first["message_id"]})
            async with adapter("sender") as resumed:
                result = await client.post("http://fixture/api/coordination/receive", headers={
                    "Authorization": "Bearer fixture-bearer", **resumed.instance_headers}, json={})
                assert result.status_code == 200
                assert result.json()["messages"][0]["message_id"] == reply["message_id"]
    try:
        asyncio.run(asyncio.wait_for(drive(), 20))
        assert storage.conn.execute("SELECT count(*) FROM entries").fetchone()[0] == before
    finally:
        storage.close()


def test_default_config_board_serves_the_singular_token_only(pg_conn, pg_url, tmp_path):
    """The 2026-09-25 default, end to end on a real store: an authenticated
    daemon with no coordination settings admits the singular token's
    adapter and serves it the check-in; a token-map principal, a separately
    trusted identity, gets neither until an operator lists it."""
    import pytest
    from pseudolife_memory.coordination import CHECKIN_TEXT
    from pseudolife_memory.coordination_adapter import AdapterError, CoordinationAdapter
    storage = PostgresStorage(pg_url)
    service = FixtureService()
    assert service.config.coordination.enabled is True
    assert service.config.coordination.allowed_principals == ["default"]
    service._db_url = pg_url
    service._storage = storage
    service._lock = threading.Lock()
    service._hlc = HybridLogicalClock()
    service._ensure_init = lambda: None
    app = build_console_app(stub_mcp, "fixture-bearer", lambda: {}, service,
                            token_map={"map-bearer": "editor"})

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            async def checkin(bearer):
                response = await client.get("http://fixture/api/hook/coordination-start",
                                            headers={"Authorization": f"Bearer {bearer}"})
                assert response.status_code == 200
                return response.text
            assert await checkin("fixture-bearer") == CHECKIN_TEXT + "\n"
            assert await checkin("map-bearer") == ""
            async with CoordinationAdapter("http://fixture", "fixture-bearer", client=client,
                                           state_path=tmp_path / "default.json",
                                           label="default-agent") as agent:
                assert agent.instance_headers["X-PL-Agent"]
            with pytest.raises(AdapterError, match="403"):
                async with CoordinationAdapter("http://fixture", "map-bearer", client=client,
                                               state_path=tmp_path / "editor.json",
                                               label="editor-agent"):
                    pass
    try:
        asyncio.run(asyncio.wait_for(drive(), 20))
    finally:
        storage.close()


def test_parent_names_its_children_over_rest(pg_conn, pg_url, tmp_path):
    """``children`` travels the REST update like the other fields and comes
    back on the peer list; omitting it leaves it, an empty list clears it."""
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    storage = PostgresStorage(pg_url)
    service = FixtureService()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["default"]
    service._storage = storage
    service._lock = threading.Lock()
    service._hlc = HybridLogicalClock()
    service._ensure_init = lambda: None
    app = build_console_app(stub_mcp, "fixture-bearer", lambda: {}, service)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            def adapter(name):
                return CoordinationAdapter("http://fixture", "fixture-bearer", client=client,
                    state_path=tmp_path / f"{name}.json", label=name)
            async with adapter("parent") as parent, adapter("peer") as peer:
                async def post(agent, action, body):
                    result = await client.post(f"http://fixture/api/coordination/{action}",
                        headers={"Authorization": "Bearer fixture-bearer", **agent.instance_headers}, json=body)
                    assert result.status_code == 200, result.text
                    return result.json()

                async def seen():
                    parent_id = parent.instance_headers["X-PL-Agent"]
                    rows = (await post(peer, "agents", {}))["agents"]
                    row = next(r for r in rows if r["agent_id"] == parent_id)
                    return [c["label"] for c in row["children"]]

                await post(parent, "update", {"children": ["review storage", "tests"]})
                assert await seen() == ["review storage", "tests"]
                await post(parent, "update", {"status": "orchestrating"})
                assert await seen() == ["review storage", "tests"]
                await post(parent, "update", {"children": []})
                assert await seen() == []
    try:
        asyncio.run(asyncio.wait_for(drive(), 20))
    finally:
        storage.close()



def test_park_records_gate_wakes_over_rest(pg_conn, pg_url, tmp_path):
    """End to end on the bench Postgres: a session parks over REST, peers
    see the record, chatter to it is withheld with the need, mail that
    clears the need is rung and reaches the parked adapter's next heartbeat
    as a ring marker, and the Stop-hook park gate answers for both."""
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    storage = PostgresStorage(pg_url)
    service = FixtureService()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["default"]
    service._storage = storage
    service._lock = threading.Lock()
    service._hlc = HybridLogicalClock()
    service._ensure_init = lambda: None
    app = build_console_app(stub_mcp, "fixture-bearer", lambda: {}, service)

    async def drive():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            def adapter(name, wake=False):
                return CoordinationAdapter("http://fixture", "fixture-bearer", client=client,
                    state_path=tmp_path / f"{name}.json", label=name, wake_enabled=wake,
                    digest_path=tmp_path / "digests" / f"{name}.txt")
            async with adapter("sender") as sender, adapter("parked", True) as parked:
                async def post(agent, action, body):
                    result = await client.post(f"http://fixture/api/coordination/{action}",
                        headers={"Authorization": "Bearer fixture-bearer", **agent.instance_headers}, json=body)
                    assert result.status_code == 200, result.text
                    return result.json()

                async def gate(agent, since=None):
                    query = "?agent=" + agent.instance_headers["X-PL-Agent"]
                    if since is not None:
                        query += f"&since={since}"
                    result = await client.get("http://fixture/api/hook/park-gate" + query,
                                              headers={"Authorization": "Bearer fixture-bearer"})
                    assert result.status_code == 200, result.text
                    return result.text.split("\n")[0]

                parked_id = parked.instance_headers["X-PL-Agent"]
                assert (tmp_path / "digests" / "parked.agent").read_text().strip() == parked_id
                assert await gate(parked) == "block"
                row = await post(parked, "update", {"status": "waiting for the review",
                    "park_reason": "waiting_peer", "park_needs": "review of the storage diff",
                    "park_clear_by": "anyone", "park_resume": "apply the review and push"})
                assert row["park_reason"] == "waiting_peer"
                assert await gate(parked) == "allow"
                listed = next(r for r in (await post(sender, "agents", {}))["agents"]
                              if r["agent_id"] == parked_id)
                assert (listed["park_needs"], listed["park_clear_by"]) == (
                    "review of the storage diff", "anyone")
                # Idle: the attach a moment ago counted as activity.
                storage.conn.execute("UPDATE coordination_agents SET last_activity=last_activity-3600")
                storage.conn.commit()
                first = await post(sender, "send", {"to": parked_id, "text": "the review is in",
                                                    "request_id": "clears-it",
                                                    "clears": "review of the storage diff"})
                assert first["wake"]["decision"] == "rung"
                await parked._heartbeat()
                await asyncio.sleep(0.05)
                ring = (tmp_path / "digests" / "parked.ring").read_text().splitlines()
                assert ring == [str(parked.digest_watermark), "rung anyone"]
                # Parked, but silent this turn: asked again.
                assert await gate(parked, since=4102444800) == "block"
                await post(parked, "update", {"status": "applying the review"})
                assert await gate(parked) == "block"
            # A bearer that cannot use the board gets no answer.
            result = await client.get("http://fixture/api/hook/park-gate?agent=" + parked_id,
                                      headers={"Authorization": "Bearer wrong"})
            assert (result.status_code, result.text) == (200, "")
    try:
        asyncio.run(asyncio.wait_for(drive(), 30))
    finally:
        storage.close()
