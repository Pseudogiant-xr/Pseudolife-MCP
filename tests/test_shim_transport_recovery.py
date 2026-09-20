"""Recovery and sanitization at the stdio shim's upstream boundary."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event, Thread
import time

import pytest

try:
    from builtins import ExceptionGroup
except ImportError:  # pragma: no cover - Python 3.10
    from exceptiongroup import ExceptionGroup

from pseudolife_memory import shim


OLD_TOKEN = "fixture-old-secret"
NEW_TOKEN = "fixture-new-secret"
BODY_MARKER = "private-body-marker"


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class _Fixture:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None, dict]] = []
        self.fault_method: str | None = None
        self.fault: int | str | None = None
        self.block_initialize = False
        self.initialize_blocked = Event()
        self.release_initialize = Event()
        self.writes = 0
        self.server: _ReusableHTTPServer | None = None
        self.worker: Thread | None = None

    def start(self, port: int = 0) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _reply(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _reply_sse(self, events: list[dict] | None) -> None:
                """A streamable-HTTP reply as the daemon sends it: one
                server-sent event per JSON-RPC message, then the stream ends.
                ``None`` closes the stream without any event."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    for body in events or ():
                        raw = json.dumps(body)
                        self.wfile.write(f"event: message\ndata: {raw}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self) -> None:
                if self.path.startswith("/bulk"):
                    # A listen-stream style GET carrying one large event.
                    size = int(self.path.partition("=")[2] or 0)
                    self._reply_sse([{"jsonrpc": "2.0", "method": "notifications/message",
                                      "params": {"data": "b" * size}}])
                    return
                self._reply(405, {})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                method = request["method"]
                auth = self.headers.get("Authorization")
                owner.requests.append((method, auth, request.get("params") or {}))
                fault = None

                if owner.block_initialize and method == "initialize":
                    owner.block_initialize = False

                    owner.initialize_blocked.set()
                    owner.release_initialize.wait(timeout=3)

                if owner.fault_method == method and owner.fault is not None:
                    fault, owner.fault = owner.fault, None
                    if isinstance(fault, int):
                        self._reply(fault, {
                            "error": BODY_MARKER,
                            "credential": OLD_TOKEN,
                            "url": "http://private.invalid/mcp",
                        })
                        return
                    if fault == "stall":
                        time.sleep(0.5)

                if auth not in {f"Bearer {OLD_TOKEN}", f"Bearer {NEW_TOKEN}"}:
                    self._reply(401, {"error": BODY_MARKER})
                    return

                if method == "initialize":
                    result = {
                        "protocolVersion": request["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": [
                        {"name": "read", "inputSchema": {"type": "object"}},
                        {"name": "write", "inputSchema": {"type": "object"}},
                        {"name": "bulk", "inputSchema": {"type": "object"}},
                    ]}
                elif method == "tools/call":
                    if request["params"]["name"] == "write":
                        owner.writes += 1
                    if fault == "drop_after_commit":
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    if fault == "commit_503":
                        self._reply(503, {"error": BODY_MARKER})
                        return
                    if fault == "commit_401":
                        self._reply(401, {"error": BODY_MARKER})
                        return
                    if fault == "sse_cut":
                        # Headers went out, the tool ran, no result event
                        # arrived: what an oversized event or a dropped
                        # connection looks like from the client side.
                        self._reply_sse(None)
                        return
                    if request["params"]["name"] == "bulk":
                        size = int(request["params"].get("arguments", {}).get("bytes", 0))
                        self._reply_sse([{
                            "jsonrpc": "2.0", "id": request["id"], "result": {
                                "content": [{"type": "text", "text": "b" * size}],
                                "isError": False,
                            }}])
                        return
                    result = {
                        "content": [{"type": "text", "text": json.dumps({
                            "writes": owner.writes,
                            "label": request["params"].get("arguments", {}).get("label"),
                        })}],
                        "isError": False,
                    }
                else:
                    self._reply(202, {})
                    return

                self._reply(200, {
                    "jsonrpc": "2.0", "id": request["id"], "result": result,
                })

        self.server = _ReusableHTTPServer(("127.0.0.1", port), Handler)
        self.worker = Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()


    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self) -> int:
        assert self.server is not None and self.worker is not None
        port = self.server.server_port
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.server = None
        self.worker = None
        return port


def test_daemon_url_rejects_secret_bearing_components_without_echo(monkeypatch, capsys):
    secret = "private-url-secret"
    monkeypatch.setenv(
        "PSEUDOLIFE_MCP_DAEMON_URL",
        f"http://fixture:{secret}@127.0.0.1:8765/private?token={secret}")
    with pytest.raises(SystemExit):
        shim._daemon_url()
    assert secret not in capsys.readouterr().err


def test_operation_timeout_preserves_cold_start_budget_and_finite_override(monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", raising=False)
    assert shim._operation_timeout_seconds() == 180.0
    monkeypatch.setenv("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", "240")
    assert shim._operation_timeout_seconds() == 240.0
    for invalid in ("nan", "inf", "0", "-1", "invalid"):
        monkeypatch.setenv("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS", invalid)
        assert shim._operation_timeout_seconds() == 180.0


@pytest.fixture
def upstream():
    fixture = _Fixture()
    fixture.start()
    try:
        yield fixture
    finally:
        if fixture.server is not None:
            fixture.stop()


def test_episode_post_never_forwards_bearer_across_redirect(tmp_path):
    received = []

    class TargetHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(204)
            self.end_headers()

    target = _ReusableHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_worker = Thread(target=target.serve_forever, daemon=True)
    target_worker.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_port}/capture")
            self.end_headers()

    origin = _ReusableHTTPServer(("127.0.0.1", 0), RedirectHandler)
    origin_worker = Thread(target=origin.serve_forever, daemon=True)
    origin_worker.start()
    try:
        shim._post_episode(
            f"http://127.0.0.1:{origin.server_port}", OLD_TOKEN,
            "/api/episode/start", {"session_key": "fixture"})
        assert received == []
    finally:
        origin.shutdown()
        origin.server_close()
        origin_worker.join(timeout=2)
        target.shutdown()
        target.server_close()
        target_worker.join(timeout=2)


def test_mcp_transport_never_follows_cross_origin_redirect(tmp_path):
    destination_reached = Event()

    class TargetHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            destination_reached.set()
            self.send_response(204)
            self.end_headers()
        do_POST = do_GET

    target = _ReusableHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_worker = Thread(target=target.serve_forever, daemon=True)
    target_worker.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_port}/capture")
            self.end_headers()
        def do_GET(self):
            self.send_response(405)
            self.end_headers()

    origin = _ReusableHTTPServer(("127.0.0.1", 0), RedirectHandler)
    origin_worker = Thread(target=origin.serve_forever, daemon=True)
    origin_worker.start()
    token_file = tmp_path / "token"
    _replace_token(token_file, OLD_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = {**os.environ, "PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file)}
        env.pop("PSEUDOLIFE_MCP_TOKEN", None)
        code = (
            "import asyncio,sys; "
            "from pseudolife_memory.credentials import CredentialProvider; "
            "from pseudolife_memory.shim import _proxy; "
            "asyncio.run(_proxy(sys.argv[1],None,'fixture-session',"
            "provider=CredentialProvider.from_environment(),agent_headers={"
            "'X-PL-Agent':'fixture-agent','X-PL-Agent-Key':'fixture-key',"
            "'X-PL-Bank':'11111111-1111-4111-8111-111111111111',"
            "'X-PL-Principal':'fixture-principal'}))"
        )
        params = StdioServerParameters(
            command=sys.executable, args=[
                "-c", code, f"http://127.0.0.1:{origin.server_port}"], env=env)
        with stderr.open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as streams:
                async with ClientSession(*streams[:2]) as client:
                    await client.initialize()

    try:
        asyncio.run(asyncio.wait_for(drive(), timeout=8))
        assert not destination_reached.is_set()
    finally:
        origin.shutdown()
        origin.server_close()
        origin_worker.join(timeout=2)
        target.shutdown()
        target.server_close()
        target_worker.join(timeout=2)


def _replace_token(path: Path, token: str) -> None:
    from pseudolife_memory.credentials import _write_token_file
    _write_token_file(path, token)


@asynccontextmanager
async def _proxy_client(upstream: _Fixture, token_file: Path, stderr: Path,
                        *, operation_timeout: float = 1.0):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        **os.environ,
        "PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file),
        "PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS": str(operation_timeout),
    }
    env.pop("PSEUDOLIFE_MCP_TOKEN", None)
    code = (
        "import asyncio,sys; "
        "from pseudolife_memory.credentials import CredentialProvider; "
        "from pseudolife_memory.shim import _proxy; "
        "asyncio.run(_proxy(sys.argv[1],None,'fixture-session',"
        "provider=CredentialProvider.from_environment()))"
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-c", code, upstream.url], env=env)
    with stderr.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as streams:
            async with ClientSession(*streams[:2]) as client:
                await client.initialize()
                yield client


def _error_data(exc: BaseException) -> dict:
    error = getattr(exc, "error", None)
    assert error is not None, repr(exc)
    assert error.code == -32603
    assert isinstance(error.data, dict)
    return error.data


def test_each_operation_uses_one_fresh_credential_snapshot(tmp_path, upstream):
    """Rotation may overlap a call without mixing bearers inside either call."""
    token_file = tmp_path / "token"
    _replace_token(token_file, OLD_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        async with _proxy_client(upstream, token_file, stderr) as client:
            upstream.block_initialize = True
            old = asyncio.create_task(client.call_tool("read", {"label": "old"}))
            assert await asyncio.to_thread(upstream.initialize_blocked.wait, 2)
            _replace_token(token_file, NEW_TOKEN)
            new = asyncio.create_task(client.call_tool("read", {"label": "new"}))
            upstream.release_initialize.set()
            old_result, new_result = await asyncio.gather(
                old, new, return_exceptions=True)
            assert isinstance(old_result, Exception)
            assert _error_data(old_result) == {
                "classification": "credential_unavailable",
                "phase": "initialize",
                "operation_outcome": "not_dispatched",
            }
            assert json.loads(new_result.content[0].text)["label"] == "new"

    asyncio.run(asyncio.wait_for(drive(), timeout=8))
    calls = [(auth, params["arguments"]["label"])
             for method, auth, params in upstream.requests if method == "tools/call"]
    assert (f"Bearer {OLD_TOKEN}", "old") not in calls
    assert (f"Bearer {NEW_TOKEN}", "new") in calls
    assert any(method == "initialize" and auth == f"Bearer {OLD_TOKEN}"
               for method, auth, _params in upstream.requests)


@pytest.mark.parametrize("status,classification,outcome", [
    (401, "authentication_required", "unknown"),
    (503, "service_unavailable", "unknown"),
])
def test_http_failure_is_sanitized_and_next_call_recovers(
        tmp_path, upstream, status, classification, outcome):
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        async with _proxy_client(upstream, token_file, stderr) as client:
            upstream.fault_method = "tools/call"
            upstream.fault = status
            with pytest.raises(Exception) as caught:
                await client.call_tool("read", {})
            data = _error_data(caught.value)
            assert data == {
                "classification": classification,
                "phase": "call",
                "operation_outcome": outcome,
            }
            if outcome == "unknown":
                assert "check its result before retrying" in caught.value.message.lower()
            recovered = await client.call_tool("read", {})
            assert json.loads(recovered.content[0].text)["writes"] == 0
            rendered = repr(caught.value) + str(caught.value)
            assert BODY_MARKER not in rendered
            assert OLD_TOKEN not in rendered
            assert "private.invalid" not in rendered

    asyncio.run(asyncio.wait_for(drive(), timeout=8))
    errors = stderr.read_text(encoding="utf-8")
    assert BODY_MARKER not in errors
    assert OLD_TOKEN not in errors
    assert "private.invalid" not in errors


def test_lost_write_response_is_unknown_and_never_replayed(tmp_path, upstream):
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        async with _proxy_client(upstream, token_file, stderr) as client:
            upstream.fault_method = "tools/call"
            upstream.fault = "drop_after_commit"
            with pytest.raises(Exception) as caught:
                unexpected = await client.call_tool("write", {})
                pytest.fail(f"unexpected successful shape: {unexpected!r}")
            data = _error_data(caught.value)
            assert data["phase"] == "call"
            assert data["operation_outcome"] == "unknown"
            assert upstream.writes == 1
            recovered = await client.call_tool("read", {})
            assert json.loads(recovered.content[0].text)["writes"] == 1

    asyncio.run(asyncio.wait_for(drive(), timeout=8))


def test_write_committed_before_503_is_unknown_and_never_replayed(tmp_path, upstream):
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        async with _proxy_client(upstream, token_file, stderr) as client:
            upstream.fault_method = "tools/call"
            upstream.fault = "commit_503"
            with pytest.raises(Exception) as caught:
                await client.call_tool("write", {})
            assert _error_data(caught.value) == {
                "classification": "service_unavailable",
                "phase": "call",
                "operation_outcome": "unknown",
            }
            assert "check its result before retrying" in caught.value.message.lower()
            assert upstream.writes == 1
            recovered = await client.call_tool("read", {})
            assert json.loads(recovered.content[0].text)["writes"] == 1

    asyncio.run(asyncio.wait_for(drive(), timeout=8))


def test_write_committed_before_401_has_safe_unknown_outcome_guidance(tmp_path, upstream):
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        async with _proxy_client(upstream, token_file, stderr) as client:
            upstream.fault_method = "tools/call"
            upstream.fault = "commit_401"
            with pytest.raises(Exception) as caught:
                await client.call_tool("write", {"request_id": "fixture-request"})
            assert _error_data(caught.value) == {
                "classification": "authentication_required",
                "phase": "call",
                "operation_outcome": "unknown",
            }
            assert "check its result before retrying" in caught.value.message.lower()
            assert "same request_id" in caught.value.message
            assert upstream.writes == 1
            recovered = await client.call_tool("read", {})
            assert json.loads(recovered.content[0].text)["writes"] == 1

    asyncio.run(asyncio.wait_for(drive(), timeout=8))


def test_closed_listener_recovers_on_next_operation(tmp_path, upstream):
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        async with _proxy_client(
                upstream, token_file, stderr, operation_timeout=0.3) as client:
            port = await asyncio.to_thread(upstream.stop)
            with pytest.raises(Exception) as caught:
                await client.call_tool("read", {})
            assert _error_data(caught.value) == {
                "classification": "connection_failure",
                "phase": "initialize",
                "operation_outcome": "not_dispatched",
            }
            upstream.start(port)
            recovered = await client.call_tool("read", {})
            assert json.loads(recovered.content[0].text)["writes"] == 0

    asyncio.run(asyncio.wait_for(drive(), timeout=8))


def test_call_timeout_is_unknown_and_next_operation_recovers(tmp_path, upstream):
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        async with _proxy_client(
                upstream, token_file, stderr, operation_timeout=0.1) as client:
            upstream.fault_method = "tools/call"
            upstream.fault = "stall"
            with pytest.raises(Exception) as caught:
                await client.call_tool("read", {})
            assert _error_data(caught.value) == {
                "classification": "timeout",
                "phase": "call",
                "operation_outcome": "unknown",
            }
            recovered = await client.call_tool("read", {})
            assert json.loads(recovered.content[0].text)["writes"] == 0

    asyncio.run(asyncio.wait_for(drive(), timeout=8))


def test_missing_dynamic_credential_fails_closed_then_recovers(tmp_path, upstream):
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"

    async def drive():
        async with _proxy_client(upstream, token_file, stderr) as client:
            token_file.unlink()
            request_count = len(upstream.requests)
            with pytest.raises(Exception) as caught:
                await client.call_tool("read", {})
            assert _error_data(caught.value) == {
                "classification": "credential_unavailable",
                "phase": "initialize",
                "operation_outcome": "not_dispatched",
            }
            assert len(upstream.requests) == request_count
            _replace_token(token_file, NEW_TOKEN)
            recovered = await client.call_tool("read", {})
            assert json.loads(recovered.content[0].text)["writes"] == 0

    asyncio.run(asyncio.wait_for(drive(), timeout=8))


def test_sanitizer_flattens_nested_errors_without_echoing_details():
    nested = ExceptionGroup("private-group", [
        RuntimeError(f"{BODY_MARKER} {OLD_TOKEN} http://private.invalid"),
        ExceptionGroup("nested", [ConnectionResetError(OLD_TOKEN)]),
    ])
    error = shim._transport_error(
        nested, shim._UpstreamAttempt(phase="call", dispatched=True), "call")
    assert error.data == {
        "classification": "connection_failure",
        "phase": "call",
        "operation_outcome": "unknown",
    }
    rendered = repr(error) + str(error) + repr(error.data)
    assert BODY_MARKER not in rendered
    assert OLD_TOKEN not in rendered
    assert "private.invalid" not in rendered


def test_protocol_code_is_preserved_only_from_the_sdk_error_type():
    from mcp.shared.exceptions import MCPError

    sdk_error = MCPError(-32601, BODY_MARKER, {"credential": OLD_TOKEN})
    sanitized = shim._transport_error(
        sdk_error, shim._UpstreamAttempt(phase="list"), "list")
    assert sanitized.code == -32601
    assert sanitized.message == "The memory daemon returned an invalid MCP response."
    assert sanitized.data == {
        "classification": "protocol",
        "phase": "list",
        "operation_outcome": "not_dispatched",
    }
    assert BODY_MARKER not in repr(sanitized)
    assert OLD_TOKEN not in repr(sanitized)

    class ForgedError(RuntimeError):
        code = -32601
        data = {"credential": OLD_TOKEN}

    forged = shim._transport_error(
        ForgedError(BODY_MARKER), shim._UpstreamAttempt(phase="list"), "list")
    assert forged.code == -32603
    assert BODY_MARKER not in repr(forged)
    assert OLD_TOKEN not in repr(forged)


def test_large_tool_results_survive_the_client_sse_event_cap(tmp_path, upstream):
    """The SDK client's SSE decoder drops any event over 1 MiB (httpx2
    DEFAULT_MAX_EVENT_SIZE_BYTES) and the SDK reports it as a closed
    connection. A deep dream over a 300-proposal review queue is 1.12 MB on
    the wire (2026-09-20); the shim must raise the limit, not lose the
    result."""
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"
    size = 1_400_000

    async def drive():
        async with _proxy_client(upstream, token_file, stderr,
                                 operation_timeout=10.0) as client:
            got = await client.call_tool("bulk", {"bytes": size})
            assert got.is_error is False
            assert len(got.content[0].text) == size

    asyncio.run(asyncio.wait_for(drive(), timeout=40))


def test_listen_and_resume_streams_get_the_wider_event_limit(upstream):
    """The SDK opens its listen stream and resumes a cut response through the
    http client's own ``sse`` method, which passes the 1 MiB default
    explicitly, so rebinding the module's EventSource alone leaves those
    paths capped. The shim wraps the client too."""
    from mcp.client import streamable_http

    size = 1_400_000
    url = f"{upstream.url}/bulk?bytes={size}"

    async def drive():
        # The cap is real: an unwrapped client refuses the event.
        async with streamable_http.create_mcp_http_client() as plain:
            with pytest.raises(Exception) as caught:
                async with plain.sse(url) as source:
                    [event async for event in source]
            assert "byte limit" in str(caught.value)
        async with streamable_http.create_mcp_http_client() as http:
            shim._widen_client_sse_limit(http)
            async with http.sse(url) as source:
                events = [event async for event in source]
        assert len(events) == 1
        assert len(json.loads(events[0].data)["params"]["data"]) == size

    asyncio.run(asyncio.wait_for(drive(), timeout=40))


def test_stream_cut_after_dispatch_is_reported_as_a_lost_response(tmp_path, upstream):
    token_file = tmp_path / "token"
    _replace_token(token_file, NEW_TOKEN)
    stderr = tmp_path / "stderr.log"
    upstream.fault_method = "tools/call"
    upstream.fault = "sse_cut"

    async def drive():
        async with _proxy_client(upstream, token_file, stderr) as client:
            with pytest.raises(Exception) as caught:
                await client.call_tool("read", {})
            assert _error_data(caught.value) == {
                "classification": "response_lost",
                "phase": "call",
                "operation_outcome": "unknown",
            }
            assert "response stream closed before a result arrived" in str(caught.value)
            assert BODY_MARKER not in str(caught.value)

    asyncio.run(asyncio.wait_for(drive(), timeout=8))


def test_closed_connection_code_maps_to_response_lost():
    from mcp.shared.exceptions import MCPError
    from mcp.types import CONNECTION_CLOSED

    lost = shim._transport_error(
        MCPError(CONNECTION_CLOSED, "SSE stream ended without a response"),
        shim._UpstreamAttempt(phase="call", dispatched=True), "call")
    assert lost.code == -32603
    assert lost.data == {
        "classification": "response_lost",
        "phase": "call",
        "operation_outcome": "unknown",
    }
    assert lost.message.startswith(
        "The memory daemon's response stream closed before a result arrived")
    assert "may have completed" in lost.message

    early = shim._transport_error(
        MCPError(CONNECTION_CLOSED, "SSE stream ended without a response"),
        shim._UpstreamAttempt(phase="initialize"), "call")
    assert early.data["classification"] == "response_lost"
    assert early.data["operation_outcome"] == "not_dispatched"
    assert "may have completed" not in early.message


def test_cancellation_remains_cancellation():
    async def cancel():
        attempt = shim._UpstreamAttempt(phase="call")
        try:
            raise asyncio.CancelledError
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - documents shim boundary
            raise shim._transport_error(exc, attempt, "call") from None

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel())
