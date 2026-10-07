"""Optional Claude channel transport; no mailbox, host launch or implicit receipt.

The caller supplies an authenticated, opted-in inbox. This module only serializes
its events onto a handshake-era MCP connection and owns that worker's lifetime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
import re
import sys
from types import MappingProxyType

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.connection import Connection
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.runner import ServerRunner, aclose_shielded
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.message import SessionMessage


@dataclass(frozen=True)
class ChannelEvent:
    """Transport payload with adapter-supplied, immutable routing metadata."""

    content: str
    meta: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise ValueError("channel content must be text")
        for key, value in self.meta.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]+", key)
                    or key == "source" or not isinstance(value, str)):
                raise ValueError("invalid channel metadata")
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))


class _SerializedWriter:
    def __init__(self, stream):
        self.stream = stream
        self.lock = anyio.Lock()

    async def send(self, item):
        async with self.lock:
            await self.stream.send(item)

    async def aclose(self):
        await self.stream.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


async def idle_inbox() -> AsyncIterator[ChannelEvent]:
    """Keep the transport alive without generating events before mail is wired."""
    await anyio.sleep_forever()
    if False:  # Async generator interface; cancellation is the only exit.
        yield ChannelEvent("", {})


async def serve_channel(
    server: Server,
    read_stream,
    write_stream,
    inbox_factory: Callable[[], AsyncIterator[ChannelEvent]],
    *,
    notification_options: NotificationOptions | None = None,
) -> None:
    """Serve one legacy MCP connection and its cancellable event source.

    Uses public SDK runner/dispatcher interfaces to retain initialization state
    without reaching into a request's private session. Ordinary Server.run and
    upstream clients are unaffected. Stream submission does not prove host receipt.
    """
    writer = _SerializedWriter(write_stream)
    dispatcher = JSONRPCDispatcher(
        read_stream, writer, inline_methods=frozenset({"initialize"}))
    connection = Connection.for_loop(dispatcher)
    options = server.create_initialization_options(
        notification_options, experimental_capabilities={"claude/channel": {}})
    ready = anyio.Event()

    async def emit_events():
        await ready.wait()
        async with aclosing(inbox_factory()) as inbox:
            async for event in inbox:
                await writer.send(SessionMessage(types.JSONRPCNotification(
                    jsonrpc="2.0", method="notifications/claude/channel",
                    params={"content": event.content, "meta": dict(event.meta)},
                )))

    try:
        async with server.lifespan(server) as lifespan:
            runner = ServerRunner(server, connection, lifespan, init_options=options)

            async def on_notify(ctx, method, params):
                # The SDK accepts the notification without a prior handshake;
                # do not let an out-of-order notification enable event delivery.
                if method == "notifications/initialized" and connection.client_params is None:
                    return
                await runner.on_notify(ctx, method, params)
                if connection.initialized.is_set() and not ready.is_set():
                    print(f"pseudolife-mcp: channel protocol={connection.protocol_version} "
                          "initialized; live delivery unconfirmed.", file=sys.stderr)
                    ready.set()

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(emit_events)
                try:
                    await dispatcher.run(runner.on_request, on_notify)
                finally:
                    tasks.cancel_scope.cancel()
    finally:
        await aclose_shielded(connection)
        await writer.aclose()
