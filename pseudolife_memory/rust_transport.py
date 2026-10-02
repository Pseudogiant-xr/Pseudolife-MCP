"""Optional Rust HTTP transport; Python retains all MCP and session policy.

The helper speaks private, versioned JSON lines over pipes. Credentials never
appear in arguments, diagnostics or helper configuration files. One helper
serves the entire stdio session, with independent cancellable response streams.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx2


_MAX_BODY = 32 * 1024 * 1024
_MAX_FRAME = 1024 * 1024
_MESSAGE = "Rust HTTP transport unavailable."


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


@asynccontextmanager
async def transport_context(origin: str | None = None):
    selected = os.environ.get("PSEUDOLIFE_MCP_RUST_HTTP")
    if not selected:
        yield None
        return
    path = Path(selected)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("PSEUDOLIFE_MCP_RUST_HTTP requires an existing absolute executable path.")
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        print("pseudolife-mcp: using Python HTTP transport for configured certificate trust.", file=sys.stderr)
        yield None
        return
    async with RustTransport(path, origin=origin) as transport:
        yield transport


class _BorrowedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, owner):
        self.owner = owner

    async def handle_async_request(self, request):
        return await self.owner.handle_async_request(request)

    async def aclose(self):
        # A per-operation SDK client owns its responses, not the session helper.
        pass


class _ResponseStream(httpx2.AsyncByteStream):
    def __init__(self, owner, identifier, queue, request):
        self.owner, self.identifier, self.queue, self.request = owner, identifier, queue, request
        self.closed = False

    async def __aiter__(self):
        try:
            while True:
                event = await self.queue.get()
                kind = event.get("type")
                if kind == "end":
                    return
                if kind != "chunk":
                    raise self.owner._error(event, self.request)
                try:
                    chunk = _decode(event["data"])
                except (KeyError, ValueError, TypeError):
                    raise httpx2.RemoteProtocolError(_MESSAGE, request=self.request) from None
                yield chunk
                await self.owner._send({"type": "credit", "id": self.identifier})
        finally:
            await self.aclose()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        self.owner._pending.pop(self.identifier, None)
        self.owner._slots.release()
        with anyio.CancelScope(shield=True):
            await self.owner._cancel(self.identifier)


class RustTransport(httpx2.AsyncBaseTransport):
    def __init__(self, binary: Path, *, origin: str | None = None):
        from httpx2._utils import URLPattern, get_environment_proxies

        self.binary, self.origin = binary, origin
        self._process = None
        self._reader = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(100)  # SDK/httpx default connection limit.
        self._pending = {}
        self._next_id = 0
        self._closed = False
        self._ready = None
        self._trust_marker = None
        self._fallback_transports = {}
        self._proxies = sorted(
            [(URLPattern(pattern), proxy) for pattern, proxy in get_environment_proxies().items()])

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    def borrow(self):
        return _BorrowedTransport(self)

    @staticmethod
    def _trust_snapshot(request):
        filename = os.environ.get("SSL_CERT_FILE")
        directory = os.environ.get("SSL_CERT_DIR") if not filename else None
        try:
            raw = Path(filename).read_bytes() if filename else None
        except OSError:
            raise httpx2.ConnectError(_MESSAGE, request=request) from None
        marker = (filename, hashlib.sha256(raw).digest() if raw is not None else None, directory)
        return marker, raw

    async def _start(self, request, trust_marker):
        async with self._start_lock:
            if self._closed:
                raise httpx2.ConnectError(_MESSAGE, request=request)
            if self._process is not None and self._process.returncode is None:
                return
            if self._process is not None:
                await self._stop()
            if self.origin is None:
                self.origin = _origin(str(request.url))
            child_env = {key: value for key, value in os.environ.items()
                         if not key.startswith("PSEUDOLIFE_")
                         and key not in {"SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"}}
            # Retain redirected pipes without allocating a Windows console host.
            options = ({"creationflags": subprocess.DETACHED_PROCESS}
                       if os.name == "nt" else {})
            try:
                self._process = await asyncio.create_subprocess_exec(
                    str(self.binary), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    env=child_env, limit=_MAX_FRAME, **options)
            except OSError:
                raise httpx2.ConnectError(_MESSAGE, request=request) from None
            self._ready = asyncio.get_running_loop().create_future()
            self._reader = asyncio.create_task(self._read(self._process))
            try:
                await self._send({"type": "configure", "version": 1,
                                  "origin": self.origin, "certificates": None})
                await asyncio.wait_for(asyncio.shield(self._ready), timeout=5)
                self._trust_marker = trust_marker
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await self._stop()
                raise

    async def _send(self, command):
        process = self._process
        if process is None or process.returncode is not None:
            raise httpx2.ConnectError(_MESSAGE)
        raw = json.dumps(command, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        try:
            async with self._write_lock:
                process.stdin.write(raw)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError):
            raise httpx2.ConnectError(_MESSAGE) from None

    async def _read(self, process):
        try:
            while raw := await process.stdout.readline():
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError
                if event.get("type") == "ready":
                    if event.get("version") != 1 or self._ready.done():
                        raise ValueError
                    self._ready.set_result(None)
                    continue
                queue = self._pending.get(event.get("id"))
                if queue is not None:
                    queue.put_nowait(event)
        except (ValueError, KeyError, asyncio.QueueFull):
            pass
        finally:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(httpx2.ConnectError(_MESSAGE))
            for queue in list(self._pending.values()):
                # Credits bound each queue to four chunks plus headers/end.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "error", "kind": "connection"})
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    @staticmethod
    def _error(event, request):
        error_type = {"timeout": httpx2.ReadTimeout,
                      "connect_timeout": httpx2.ConnectTimeout,
                      "connection": httpx2.ConnectError,
                      "protocol": httpx2.RemoteProtocolError}.get(
                          event.get("kind"), httpx2.RemoteProtocolError)
        return error_type(_MESSAGE, request=request)

    async def _cancel(self, identifier):
        try:
            with anyio.move_on_after(1):
                await self._send({"type": "cancel", "id": identifier})
        except httpx2.TransportError:
            pass

    async def handle_async_request(self, request):
        if self._closed:
            raise httpx2.ConnectError(_MESSAGE, request=request)
        if self.origin is None:
            self.origin = _origin(str(request.url))
        expected = httpx2.URL(self.origin)
        parsed = urlsplit(str(request.url))
        if ((request.url.scheme, request.url.host, request.url.port)
                != (expected.scheme, expected.host, expected.port)
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            raise httpx2.RemoteProtocolError(_MESSAGE, request=request)
        body = await request.aread()
        trust_marker, _ = self._trust_snapshot(request)
        proxy = next((value for pattern, value in self._proxies if pattern.matches(request.url)), None)
        timeouts = request.extensions.get("timeout", {})
        # Decide before sending anything: preserve the existing transport's
        # unbounded bodies, directory trust and optional SOCKS support. Never
        # switch transports after a request has been dispatched.
        if (len(body) > _MAX_BODY or self._slots.locked()
                or timeouts.get("write") != timeouts.get("read")
                or (self._process is not None and self._process.returncode is None
                    and self._trust_marker != trust_marker)
                or os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR")
                or (proxy and urlsplit(proxy).scheme.startswith("socks"))):
            key = (proxy, trust_marker)
            if key not in self._fallback_transports:
                try:
                    self._fallback_transports[key] = httpx2.AsyncHTTPTransport(
                        proxy=proxy, limits=httpx2.Limits(max_keepalive_connections=0))
                except (OSError, ssl.SSLError):
                    raise httpx2.ConnectError(_MESSAGE, request=request) from None
            return await self._fallback_transports[key].handle_async_request(request)
        # A free semaphore acquire does not yield. Keep this reservation in
        # the same task as the capacity check, before startup or dispatch.
        await self._slots.acquire()
        identifier = None
        try:
            await self._start(request, trust_marker)
            self._next_id += 1
            identifier = self._next_id
            queue = asyncio.Queue(maxsize=8)
            self._pending[identifier] = queue
            await self._send({"type": "request", "id": identifier, "url": str(request.url),
                              "method": request.method, "body": _encode(body), "proxy": proxy,
                              "headers": [[_encode(key), _encode(value)] for key, value in request.headers.raw],
                              "timeout": request.extensions.get("timeout", {})})
            event = await queue.get()
            if event.get("type") != "head":
                raise self._error(event, request)
            try:
                headers = [(_decode(key), _decode(value)) for key, value in event["headers"]]
                status = event["status"]
            except (KeyError, ValueError, TypeError):
                raise httpx2.RemoteProtocolError(_MESSAGE, request=request) from None
            return httpx2.Response(status, headers=headers,
                                   stream=_ResponseStream(self, identifier, queue, request), request=request)
        except BaseException:
            self._slots.release()
            if identifier is not None:
                self._pending.pop(identifier, None)
                with anyio.CancelScope(shield=True):
                    await self._cancel(identifier)
            raise

    async def _stop(self):
        process, reader = self._process, self._reader
        self._process = self._reader = None
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass

    async def aclose(self):
        self._closed = True
        with anyio.CancelScope(shield=True):
            await self._stop()
            for transport in self._fallback_transports.values():
                await transport.aclose()
