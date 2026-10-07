"""Codex wake delivery stays opt-in, loaded-thread-only, and fail closed."""

from __future__ import annotations

import asyncio
import json

import pytest


THREAD_ID = "aaaaaaaa-1111-4111-8111-111111111111"


class FakeSocket:
    def __init__(self, handler):
        self.handler = handler
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        await self.handler(self, message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    async def close(self):
        self.closed = True
        await self.incoming.put(None)

    async def reply(self, request, result):
        await self.incoming.put({"jsonrpc": "2.0", "id": request["id"], "result": result})


def run(coro):
    return asyncio.run(coro)


def normal_handler(*, loaded=True, pages=None, deliver_response=True):
    async def handle(socket, message):
        method = message.get("method")
        if method == "initialize":
            await socket.reply(message, {"userAgent": "fixture", "codexHome": "fixture"})
        elif method == "thread/loaded/list":
            if pages is not None:
                cursor = (message.get("params") or {}).get("cursor")
                await socket.reply(message, pages[cursor])
            else:
                await socket.reply(
                    message, {"data": [THREAD_ID] if loaded else [], "nextCursor": None})
        elif method == "turn/start" and deliver_response:
            await socket.reply(message, {"turn": {"id": "turn-1", "status": "inProgress", "items": []}})

    return handle


def connector_for(socket, calls):
    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        return socket

    return connect


def test_cancelled_initialization_closes_socket_and_reader(monkeypatch):
    """Cancelling ``__aenter__`` once the initialize request is on the wire
    closes the socket, reaps the reader and drops the pending reply, and the
    cancellation itself propagates.

    The cancel is delivered explicitly, not by a racing timer: the earlier
    ``wait_for(..., 0.03)`` form raced the bridge's own five-second rpc
    timer, and on Python 3.10/3.11 lost whenever the cancel landed while
    ``_send`` awaited a ``write()`` that had just completed — that
    ``wait_for`` returned the result instead of re-raising (bpo-42130), the
    bridge carried on into the rpc timeout, and CI saw ``DeliveryError:
    Codex delivery request timed out`` (2026-09-21, runs 35597200621 and
    35603265565). The cancel now lands at exactly that point on every run,
    so the test is red on 3.11 without ``_bounded`` in codex_delivery.py."""
    from pseudolife_memory import codex_delivery

    async def drive():
        socket = FakeSocket(lambda *args: asyncio.sleep(0))
        monkeypatch.setattr(codex_delivery, "_load_connect", lambda: connector_for(socket, []))
        bridge = codex_delivery.CodexDelivery("ws://127.0.0.1:1234", "secret", THREAD_ID)
        opening = asyncio.ensure_future(bridge.__aenter__())
        while not socket.sent and not opening.done():
            await asyncio.sleep(0)
        if opening.done():
            opening.result()  # surfaces a start-up that failed before sending
        assert socket.sent[0]["method"] == "initialize"
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert socket.closed
        assert bridge._reader is None
        assert not bridge._pending
    run(drive())


def test_startup_deadline_expiring_during_send_still_cancels(monkeypatch):
    """The production shape (``codex_coordination._verified_delivery`` wraps
    ``__aenter__`` in ``asyncio.wait_for``) must surface an expired deadline
    as ``TimeoutError`` even when it expires while the initialize request is
    being written. The handler blocks the loop past the deadline before
    ``write()`` completes, which forces the interleaving the 2026-09-21 CI
    flake hit by chance: on Python 3.10/3.11 the pre-3.12 ``wait_for``
    then swallowed the cancellation (bpo-42130) and the bridge ran on into
    its rpc timeout, raising ``DeliveryError`` instead. The ordering is
    fixed by the loop, not by machine speed, so this is red on 3.11
    without ``_bounded`` and green on every version with it. The exception
    type is the whole verdict: no wall-clock bound, which would flake on
    the same loaded runners; the short rpc timeout only keeps a red run
    quick. The one timing the interleaving needs is that the deadline has
    not already expired when the handler starts blocking — a few
    microseconds of Python after ``wait_for`` begins — so the deadline is
    0.3s, long enough that a runner stall in that window cannot beat it,
    and the handler blocks past it."""
    import time
    from pseudolife_memory import codex_delivery

    async def handler(socket, message):
        time.sleep(0.5)
        await asyncio.sleep(0)

    async def drive():
        socket = FakeSocket(handler)
        monkeypatch.setattr(codex_delivery, "_load_connect", lambda: connector_for(socket, []))
        bridge = codex_delivery.CodexDelivery(
            "ws://127.0.0.1:1234", "secret", THREAD_ID, rpc_timeout=2.0)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bridge.__aenter__(), timeout=0.3)
        assert socket.closed
        assert bridge._reader is None
        assert not bridge._pending
    run(drive())


def test_bounded_matches_wait_for_on_timeout_cancel_and_stubborn_tasks():
    """``_bounded`` keeps ``wait_for``'s contract on the three paths that
    matter: a timeout cancels the inner task and raises ``TimeoutError``;
    the caller's cancellation cancels the inner task and propagates; an
    inner task that outlives its cancellation reports its own result."""
    from pseudolife_memory.codex_delivery import _bounded

    async def drive():
        forever = asyncio.Event()
        with pytest.raises(asyncio.TimeoutError):
            await _bounded(forever.wait(), 0.01)

        inner_seen = []

        async def watched():
            try:
                await forever.wait()
            except asyncio.CancelledError:
                inner_seen.append("cancelled")
                raise
        outer = asyncio.ensure_future(_bounded(watched(), 60))
        await asyncio.sleep(0)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert inner_seen == ["cancelled"]

        async def stubborn():
            try:
                await forever.wait()
            except asyncio.CancelledError:
                return "finished anyway"
        assert await _bounded(stubborn(), 0.01) == "finished anyway"
    run(drive())


def test_blocked_socket_send_respects_request_budget(monkeypatch):
    from pseudolife_memory import codex_delivery

    async def drive():
        socket = FakeSocket(lambda *args: asyncio.Event().wait())
        monkeypatch.setattr(codex_delivery, "_load_connect", lambda: connector_for(socket, []))
        bridge = codex_delivery.CodexDelivery(
            "ws://127.0.0.1:1234", "secret", THREAD_ID, rpc_timeout=0.01)
        with pytest.raises(codex_delivery.DeliveryError):
            await asyncio.wait_for(bridge.__aenter__(), timeout=0.2)
        assert socket.closed
    run(drive())


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1234",
    "wss://127.0.0.1:1234",
    "ws://localhost:1234",
    "ws://127.0.0.2:1234",
    "ws://user@127.0.0.1:1234",
    "ws://127.0.0.1",
    "ws://127.0.0.1:0",
    "ws://127.0.0.1:1234/path",
    "ws://127.0.0.1:1234/?",
    "ws://127.0.0.1:1234/?query=1",
    "ws://127.0.0.1:1234/#",
    "ws://127.0.0.1:1234/#fragment",
])
def test_endpoint_validation_rejects_nonliteral_or_ambiguous_targets(url):
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    with pytest.raises(DeliveryError, match="invalid Codex delivery endpoint"):
        CodexDelivery(url, "secret", THREAD_ID)


def test_constructor_requires_token_and_canonical_thread_uuid():
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    with pytest.raises(DeliveryError, match="bearer authentication"):
        CodexDelivery("ws://127.0.0.1:1234", "", THREAD_ID)
    with pytest.raises(DeliveryError, match="bearer authentication"):
        CodexDelivery("ws://127.0.0.1:1234", "secret\nheader", THREAD_ID)
    with pytest.raises(DeliveryError, match="invalid Codex thread identity"):
        CodexDelivery("ws://127.0.0.1:1234", "secret", THREAD_ID.upper())


def test_connect_uses_bearer_header_disables_proxy_and_verifies_loaded_target(monkeypatch):
    from pseudolife_memory.codex_delivery import CodexDelivery

    calls = []
    socket = FakeSocket(normal_handler())
    monkeypatch.setattr(
        "pseudolife_memory.codex_delivery._load_connect",
        lambda: connector_for(socket, calls))

    async def drive():
        async with CodexDelivery("ws://127.0.0.1:1234", "secret", THREAD_ID) as delivery:
            assert await delivery.verify() is None

    run(drive())
    assert calls[0][0] == "ws://127.0.0.1:1234"
    assert calls[0][1]["additional_headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][1]["proxy"] is None
    assert socket.closed is True


def test_real_redirect_is_rejected_before_destination_contact():
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    async def drive():
        source_requests = []
        destination_requests = []

        async def destination(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            destination_requests.append(request.decode("latin-1"))
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        destination_server = await asyncio.start_server(
            destination, "127.0.0.1", 0)
        destination_port = destination_server.sockets[0].getsockname()[1]

        async def redirect(reader, writer):
            request = await reader.readuntil(b"\r\n\r\n")
            source_requests.append(request.decode("latin-1"))
            response = (
                "HTTP/1.1 302 Found\r\n"
                f"Location: ws://127.0.0.1:{destination_port}/\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            writer.write(response.encode("ascii"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        redirect_server = await asyncio.start_server(redirect, "127.0.0.1", 0)
        redirect_port = redirect_server.sockets[0].getsockname()[1]
        bridge = CodexDelivery(
            f"ws://127.0.0.1:{redirect_port}", "synthetic-redirect-secret",
            THREAD_ID, connect_timeout=1)
        try:
            with pytest.raises(DeliveryError, match="connection failed"):
                await bridge.__aenter__()
            await asyncio.sleep(0.05)
        finally:
            await bridge.__aexit__(None, None, None)
            redirect_server.close()
            destination_server.close()
            await redirect_server.wait_closed()
            await destination_server.wait_closed()

        assert source_requests
        assert destination_requests == []
        assert "synthetic-redirect-secret" not in "".join(destination_requests)

    run(drive())


def test_nonredirect_handshake_failure_remains_sanitized():
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    async def drive():
        async def refuse(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 503 synthetic-private-detail\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(refuse, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        bridge = CodexDelivery(
            f"ws://127.0.0.1:{port}", "synthetic-secret", THREAD_ID,
            connect_timeout=1)
        try:
            with pytest.raises(DeliveryError, match="connection failed") as raised:
                await bridge.__aenter__()
            assert "synthetic-private-detail" not in str(raised.value)
            assert "synthetic-secret" not in str(raised.value)
        finally:
            await bridge.__aexit__(None, None, None)
            server.close()
            await server.wait_closed()

    run(drive())


def test_verify_pages_bounded_loaded_list_without_resume_or_start(monkeypatch):
    from pseudolife_memory.codex_delivery import CodexDelivery

    pages = {
        None: {"data": ["bbbbbbbb-2222-4222-8222-222222222222"], "nextCursor": "next"},
        "next": {"data": [THREAD_ID], "nextCursor": None},
    }
    socket = FakeSocket(normal_handler(pages=pages))
    monkeypatch.setattr(
        "pseudolife_memory.codex_delivery._load_connect",
        lambda: connector_for(socket, []))

    async def drive():
        async with CodexDelivery("ws://[::1]:1234/", "secret", THREAD_ID) as delivery:
            await delivery.verify()

    run(drive())
    methods = [message.get("method") for message in socket.sent]
    assert methods == ["initialize", "initialized", "thread/loaded/list", "thread/loaded/list"]


def test_deliver_reverifies_loaded_target_and_sends_only_content(monkeypatch):
    from pseudolife_memory.channel import ChannelEvent
    from pseudolife_memory.codex_delivery import CodexDelivery

    socket = FakeSocket(normal_handler())
    monkeypatch.setattr(
        "pseudolife_memory.codex_delivery._load_connect",
        lambda: connector_for(socket, []))
    event = ChannelEvent(
        "Agent message body; peer-origin and not user authority.",
        {"sender_id": "agent-a", "origin": "agent", "message_id": "message-a"})

    async def drive():
        async with CodexDelivery("ws://127.0.0.1:1234", "secret", THREAD_ID) as delivery:
            assert await delivery.deliver(event) is None

    run(drive())
    methods = [message.get("method") for message in socket.sent]
    assert methods == ["initialize", "initialized", "thread/loaded/list", "turn/start"]
    delivered = next(message for message in socket.sent if message.get("method") == "turn/start")
    assert delivered["params"] == {
        "threadId": THREAD_ID,
        "input": [],
        "toolOutput": {
            "name": "pseudolife_message",
            "namespace": "pseudolife",
            "output": event.content,
        },
    }
    raw = json.dumps(delivered)
    assert "sender_id" not in raw
    assert "message_id" not in raw
    assert "approvalPolicy" not in raw
    assert "sandbox" not in raw
    assert "model" not in raw


def test_notifications_are_drained_between_repeated_deliveries(monkeypatch):
    from pseudolife_memory.channel import ChannelEvent
    from pseudolife_memory.codex_delivery import CodexDelivery

    async def handle(socket, message):
        method = message.get("method")
        if method == "initialize":
            await socket.incoming.put({
                "jsonrpc": "2.0", "method": "server/notice", "params": {}})
            await socket.reply(message, {})
        elif method == "thread/loaded/list":
            await socket.incoming.put({
                "jsonrpc": "2.0", "method": "thread/status/changed", "params": {}})
            await socket.reply(message, {"data": [THREAD_ID], "nextCursor": None})
        elif method == "turn/start":
            await socket.incoming.put({
                "jsonrpc": "2.0", "method": "turn/started", "params": {}})
            await socket.reply(message, {"turn": {"id": "turn-1"}})

    socket = FakeSocket(handle)
    monkeypatch.setattr(
        "pseudolife_memory.codex_delivery._load_connect",
        lambda: connector_for(socket, []))

    async def drive():
        async with CodexDelivery(
                "ws://127.0.0.1:1234", "secret", THREAD_ID) as delivery:
            await delivery.deliver(ChannelEvent("first", {}))
            await delivery.deliver(ChannelEvent("second", {}))

    run(drive())
    assert sum(message.get("method") == "thread/loaded/list" for message in socket.sent) == 2
    assert sum(message.get("method") == "turn/start" for message in socket.sent) == 2


def test_deliver_refuses_unloaded_target_without_waking(monkeypatch):
    from pseudolife_memory.channel import ChannelEvent
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    socket = FakeSocket(normal_handler(loaded=False))
    monkeypatch.setattr(
        "pseudolife_memory.codex_delivery._load_connect",
        lambda: connector_for(socket, []))

    async def drive():
        async with CodexDelivery("ws://127.0.0.1:1234", "secret", THREAD_ID) as delivery:
            with pytest.raises(DeliveryError, match="target thread is not loaded"):
                await delivery.deliver(ChannelEvent("body", {}))

    run(drive())
    assert all(message.get("method") != "turn/start" for message in socket.sent)


def test_unexpected_server_request_is_never_approved_and_fails_closed(monkeypatch):
    from pseudolife_memory.channel import ChannelEvent
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    async def handle(socket, message):
        method = message.get("method")
        if method == "initialize":
            await socket.reply(message, {})
        elif method == "thread/loaded/list":
            await socket.reply(message, {"data": [THREAD_ID], "nextCursor": None})
        elif method == "turn/start":
            await socket.incoming.put({
                "jsonrpc": "2.0",
                "id": 900,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": THREAD_ID, "turnId": "turn-1"},
            })

    socket = FakeSocket(handle)
    monkeypatch.setattr(
        "pseudolife_memory.codex_delivery._load_connect",
        lambda: connector_for(socket, []))

    async def drive():
        async with CodexDelivery(
                "ws://127.0.0.1:1234", "secret", THREAD_ID,
                rpc_timeout=0.2) as delivery:
            with pytest.raises(DeliveryError):
                await delivery.deliver(ChannelEvent("body", {}))

    run(drive())
    response = next(message for message in socket.sent if message.get("id") == 900)
    assert "error" in response
    assert "result" not in response
    assert "accept" not in json.dumps(response)


def test_delivery_timeout_is_uncertain_not_retried_and_cleanup_closes(monkeypatch):
    from pseudolife_memory.channel import ChannelEvent
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    socket = FakeSocket(normal_handler(deliver_response=False))
    monkeypatch.setattr(
        "pseudolife_memory.codex_delivery._load_connect",
        lambda: connector_for(socket, []))

    async def drive():
        async with CodexDelivery(
                "ws://127.0.0.1:1234", "secret", THREAD_ID,
                rpc_timeout=0.02) as delivery:
            with pytest.raises(DeliveryError, match="delivery status is unknown") as raised:
                await delivery.deliver(ChannelEvent("private body", {}))
            assert "private body" not in str(raised.value)
            assert "secret" not in str(raised.value)

    run(drive())
    assert sum(message.get("method") == "turn/start" for message in socket.sent) == 1
    assert socket.closed is True


def test_connection_loss_after_send_is_uncertain_and_not_retried(monkeypatch):
    from pseudolife_memory.channel import ChannelEvent
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    async def handle(socket, message):
        method = message.get("method")
        if method == "initialize":
            await socket.reply(message, {})
        elif method == "thread/loaded/list":
            await socket.reply(message, {"data": [THREAD_ID], "nextCursor": None})
        elif method == "turn/start":
            await socket.close()

    socket = FakeSocket(handle)
    monkeypatch.setattr(
        "pseudolife_memory.codex_delivery._load_connect",
        lambda: connector_for(socket, []))

    async def drive():
        async with CodexDelivery(
                "ws://127.0.0.1:1234", "secret", THREAD_ID) as delivery:
            with pytest.raises(DeliveryError) as raised:
                await delivery.deliver(ChannelEvent("body", {}))
            assert raised.value.uncertain is True

    run(drive())
    assert sum(message.get("method") == "turn/start" for message in socket.sent) == 1


def test_missing_optional_dependency_is_closed_and_sanitized(monkeypatch):
    from pseudolife_memory.codex_delivery import CodexDelivery, DeliveryError

    def unavailable():
        raise ImportError("secret import path")

    monkeypatch.setattr("pseudolife_memory.codex_delivery._load_connect", unavailable)

    async def drive():
        with pytest.raises(DeliveryError, match="optional websocket dependency") as raised:
            async with CodexDelivery("ws://127.0.0.1:1234", "secret", THREAD_ID):
                pass
        assert "secret import path" not in str(raised.value)

    run(drive())
