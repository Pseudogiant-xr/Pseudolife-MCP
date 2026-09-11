"""Optional delivery of coordination events to already-loaded Codex threads."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
from urllib.parse import urlsplit
import uuid

from .channel import ChannelEvent


class DeliveryError(RuntimeError):
    """Sanitized delivery failure with no endpoint, credential, or event body."""

    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def _load_connect():
    from websockets.asyncio.client import connect

    class _NoRedirectConnect(connect):
        def process_redirect(self, exc):
            # Keep the authenticated request on the endpoint validated above.
            # websockets otherwise reuses additional_headers across redirects.
            return exc

    return _NoRedirectConnect


def _validate_endpoint(url: str) -> str:
    if not isinstance(url, str):
        raise DeliveryError("invalid Codex delivery endpoint")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise DeliveryError("invalid Codex delivery endpoint") from None
    if (parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None or port == 0
            or parsed.username is not None or parsed.password is not None
            or "?" in url or "#" in url or parsed.path not in {"", "/"}):
        raise DeliveryError("invalid Codex delivery endpoint")
    return url


def _validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str):
        raise DeliveryError("invalid Codex thread identity")
    try:
        canonical = str(uuid.UUID(thread_id))
    except (ValueError, AttributeError):
        raise DeliveryError("invalid Codex thread identity") from None
    if canonical != thread_id:
        raise DeliveryError("invalid Codex thread identity")
    return canonical


class CodexDelivery:
    """Send events through one authenticated loopback app-server connection."""

    def __init__(self, url: str, token: str, thread_id: str, *,
                 connect_timeout: float = 3.0, rpc_timeout: float = 5.0,
                 close_timeout: float = 1.0, max_loaded_pages: int = 32):
        self.url = _validate_endpoint(url)
        if (not isinstance(token, str) or not token
                or any(character.isspace() for character in token)):
            raise DeliveryError("Codex delivery requires bearer authentication")
        self._token = token
        self.thread_id = _validate_thread_id(thread_id)
        self._connect_timeout = connect_timeout
        self._rpc_timeout = rpc_timeout
        self._close_timeout = close_timeout
        self._max_loaded_pages = max_loaded_pages
        self._socket = None
        self._reader = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._send_lock = asyncio.Lock()
        self._delivery_lock = asyncio.Lock()
        self._failure: DeliveryError | None = None
        self._closing = False

    async def __aenter__(self):
        if self._socket is not None:
            raise DeliveryError("Codex delivery connection is already open")
        try:
            connector = _load_connect()
        except ImportError:
            raise DeliveryError(
                "Codex delivery requires the optional websocket dependency") from None
        try:
            connection = connector(
                self.url,
                additional_headers={"Authorization": f"Bearer {self._token}"},
                proxy=None,
                open_timeout=self._connect_timeout,
                close_timeout=self._close_timeout,
                max_size=1_048_576,
            )
            self._socket = await asyncio.wait_for(
                connection, timeout=self._connect_timeout)
            self._reader = asyncio.create_task(self._reader_loop())
            await self._rpc(
                "initialize",
                {
                    "clientInfo": {
                        "name": "pseudolife-codex-delivery",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._notify("initialized")
        except DeliveryError:
            await self._close()
            raise
        except asyncio.CancelledError:
            await self._close()
            raise
        except Exception:
            await self._close()
            raise DeliveryError("Codex delivery connection failed") from None
        return self

    async def __aexit__(self, *exc):
        await self._close()

    async def _send(self, message: dict) -> None:
        if self._socket is None or self._failure is not None:
            raise self._failure or DeliveryError("Codex delivery connection is closed")
        try:
            raw = json.dumps(message, separators=(",", ":"))

            async def write():
                async with self._send_lock:
                    await self._socket.send(raw)

            await asyncio.wait_for(write(), timeout=self._rpc_timeout)
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError("Codex delivery connection failed") from None

    async def _notify(self, method: str) -> None:
        await self._send({"jsonrpc": "2.0", "method": method})

    async def _rpc(self, method: str, params: dict, *, uncertain: bool = False):
        if self._failure is not None:
            raise self._failure
        loop = asyncio.get_running_loop()
        request_id = self._next_id
        self._next_id += 1
        response = loop.create_future()
        self._pending[request_id] = response
        try:
            try:
                await self._send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                })
            except DeliveryError:
                if uncertain:
                    raise DeliveryError(
                        "Codex delivery failed; delivery status is unknown",
                        uncertain=True) from None
                raise
            try:
                payload = await asyncio.wait_for(
                    asyncio.shield(response), timeout=self._rpc_timeout)
            except asyncio.TimeoutError:
                if uncertain:
                    raise DeliveryError(
                        "Codex delivery timed out; delivery status is unknown",
                        uncertain=True) from None
                raise DeliveryError("Codex delivery request timed out") from None
            except DeliveryError:
                if uncertain:
                    raise DeliveryError(
                        "Codex delivery failed; delivery status is unknown",
                        uncertain=True) from None
                raise
        finally:
            self._pending.pop(request_id, None)
            if not response.done():
                response.cancel()
        if not isinstance(payload, dict):
            if uncertain:
                raise DeliveryError(
                    "Codex delivery response was invalid; delivery status is unknown",
                    uncertain=True)
            raise DeliveryError("Codex delivery response was invalid")
        if "error" in payload:
            raise DeliveryError("Codex delivery request was refused")
        if "result" not in payload:
            if uncertain:
                raise DeliveryError(
                    "Codex delivery response was invalid; delivery status is unknown",
                    uncertain=True)
            raise DeliveryError("Codex delivery response was invalid")
        return payload["result"]

    async def _reader_loop(self) -> None:
        failure = None
        try:
            async for raw in self._socket:
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    failure = DeliveryError("Codex delivery returned invalid protocol data")
                    break
                if not isinstance(message, dict):
                    failure = DeliveryError("Codex delivery returned invalid protocol data")
                    break
                request_id = message.get("id")
                if request_id in self._pending and "method" not in message:
                    future = self._pending[request_id]
                    if not future.done():
                        future.set_result(message)
                    continue
                if request_id is not None and isinstance(message.get("method"), str):
                    # The owner client must retain all approval and elicitation handling.
                    # If app-server routes one here unexpectedly, stop the bridge.
                    try:
                        await self._send({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32001,
                                "message": "delivery bridge does not handle server requests",
                            },
                        })
                    except DeliveryError:
                        pass
                    failure = DeliveryError(
                        "Codex delivery stopped after an unexpected server request",
                        uncertain=True)
                    break
                # Notifications are intentionally drained and discarded.
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closing:
                failure = DeliveryError("Codex delivery connection closed")
        finally:
            if failure is None and not self._closing:
                failure = DeliveryError("Codex delivery connection closed")
            if failure is not None:
                self._failure = failure
                for future in list(self._pending.values()):
                    if not future.done():
                        future.set_exception(failure)
                if self._socket is not None:
                    with suppress(Exception):
                        await self._socket.close()

    async def _close(self) -> None:
        self._closing = True
        reader, socket = self._reader, self._socket
        self._reader = None
        self._socket = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader
        if socket is not None:
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(socket.close(), timeout=self._close_timeout)
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def verify(self) -> None:
        """Confirm that the target remains loaded without loading or resuming it."""
        cursor = None
        seen = set()
        for _ in range(self._max_loaded_pages):
            params = {"limit": 100}
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._rpc("thread/loaded/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise DeliveryError("Codex loaded-thread verification returned invalid data")
            if self.thread_id in result["data"]:
                return None
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                raise DeliveryError("Codex target thread is not loaded")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise DeliveryError("Codex loaded-thread verification returned invalid data")
            seen.add(next_cursor)
            cursor = next_cursor
        raise DeliveryError("Codex loaded-thread verification exceeded its page limit")

    async def deliver(self, event: ChannelEvent) -> None:
        """Wake or steer the loaded thread once; uncertain sends are never retried."""
        if not isinstance(event, ChannelEvent):
            raise DeliveryError("invalid Codex delivery event")
        async with self._delivery_lock:
            await self.verify()
            result = await self._rpc(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [],
                    "toolOutput": {
                        "name": "pseudolife_message",
                        "namespace": "pseudolife",
                        "output": event.content,
                    },
                },
                uncertain=True,
            )
            if (not isinstance(result, dict) or not isinstance(result.get("turn"), dict)
                    or not isinstance(result["turn"].get("id"), str)):
                raise DeliveryError(
                    "Codex delivery response was invalid; delivery status is unknown",
                    uncertain=True)
            return None
