"""Two real adapters exchange durable mail through the authenticated ASGI API."""
import asyncio
from contextlib import aclosing
import threading

import httpx

from tests.pg_fixtures import pg_url  # noqa: F401
from tests.asgi_helpers import stub_mcp
from pseudolife_memory.memory.hlc import HybridLogicalClock
from pseudolife_memory.storage.postgres import PostgresStorage
from pseudolife_memory.web.fixtures import FixtureService
from pseudolife_memory.web.api import build_console_app


def test_two_adapters_mail_reply_ack_and_resume(pg_url, tmp_path):
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
