"""Opt-in Rust HTTP boundary, using disposable listeners and client state."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import ssl
from threading import Event, Thread
import time
from types import SimpleNamespace

import httpx2
import pytest


@pytest.fixture(autouse=True)
def private_client_state(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("PSEUDOLIFE_") and name not in {
                "PSEUDOLIFE_RUST_TEST_BINARY", "PSEUDOLIFE_MCP_RUST_HTTP"}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "client-home"))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1")


@pytest.fixture
def binary():
    candidate = os.environ.get("PSEUDOLIFE_RUST_TEST_BINARY")
    if candidate is None:
        candidate = str(Path(__file__).resolve().parents[1] / "rust" / "http-transport"
                        / "target" / "debug" / ("pseudolife-http.exe" if os.name == "nt"
                                                else "pseudolife-http"))
    path = Path(candidate)
    if not path.is_file():
        pytest.skip("build rust/http-transport before running Rust integration tests")
    return path


@contextmanager
def listener(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_default_transport_is_python(monkeypatch):
    from pseudolife_memory.rust_transport import transport_context
    monkeypatch.delenv("PSEUDOLIFE_MCP_RUST_HTTP", raising=False)

    async def drive():
        async with transport_context() as transport:
            assert transport is None
    asyncio.run(drive())


def test_explicit_unusable_binary_fails_closed(monkeypatch, tmp_path):
    from pseudolife_memory.rust_transport import transport_context
    monkeypatch.setenv("PSEUDOLIFE_MCP_RUST_HTTP", str(tmp_path / "missing-secret-name"))

    async def drive():
        with pytest.raises(ValueError) as caught:
            async with transport_context():
                pytest.fail("explicit selection must not silently fall back")
        assert "missing-secret-name" not in str(caught.value)
    asyncio.run(drive())


def test_directory_trust_selects_existing_transport_before_requests(binary, monkeypatch, tmp_path):
    from pseudolife_memory.rust_transport import transport_context
    monkeypatch.setenv("PSEUDOLIFE_MCP_RUST_HTTP", str(binary))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    async def drive():
        async with transport_context("https://example.com") as transport:
            assert transport is None
    asyncio.run(drive())


def test_large_body_uses_python_before_send_without_truncation_or_replay(binary):
    from pseudolife_memory.rust_transport import RustTransport
    seen = []
    body = b"fixture body\x00\xff" * (33 * 1024 * 1024 // 14 + 1)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            incoming = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((len(incoming), hashlib.sha256(incoming).digest()))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow()) as client:
                    assert (await client.post(url, content=body)).content == b"ok"
                    assert transport.pid is None
                    assert (await client.post(url, content=b"small")).content == b"ok"
                    assert transport.pid is not None
        asyncio.run(asyncio.wait_for(drive(), timeout=15))
    assert seen == [(len(body), hashlib.sha256(body).digest()),
                    (5, hashlib.sha256(b"small").digest())]


def test_streaming_preserves_raw_headers_and_reuses_one_child(binary):
    from pseudolife_memory.rust_transport import RustTransport
    arrived = Event()
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Fixture", "first")
            self.send_header("X-Fixture", "second")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            arrived.set()
            release.wait(timeout=3)
            try:
                self.wfile.write(b"data: second\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow(), timeout=2) as client:
                    async with client.stream("GET", url) as response:
                        assert response.headers.get_list("x-fixture") == ["first", "second"]
                        chunks = response.aiter_raw()
                        assert await asyncio.wait_for(anext(chunks), timeout=1) == b"data: first\n\n"
                        pid = transport.pid
                        assert arrived.is_set()
                        release.set()
                        assert b"".join([chunk async for chunk in chunks]) == b"data: second\n\n"
                    assert (await client.get(url)).status_code == 200
                    assert transport.pid == pid
            assert transport.pid is None
        asyncio.run(asyncio.wait_for(drive(), timeout=8))


def test_redirect_sends_no_credentials_to_destination(binary):
    from pseudolife_memory.rust_transport import RustTransport
    received = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            received.append(True)
            self.send_response(204)
            self.end_headers()

    with listener(Target) as target:
        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_GET(self):
                self.send_response(307)
                self.send_header("Location", target)
                self.end_headers()
        with listener(Origin) as url:
            async def drive():
                async with RustTransport(binary) as transport:
                    async with httpx2.AsyncClient(transport=transport.borrow(),
                                                 follow_redirects=False) as client:
                        response = await client.get(url, headers={
                            "Authorization": "Bearer fixture-secret",
                            "X-PL-Agent-Key": "fixture-instance-secret",
                            "X-PL-Bank": "fixture-bank"})
                        assert response.status_code == 307
            asyncio.run(drive())
    assert received == []


def test_cancelled_stream_does_not_block_next_request_or_restart_child(binary):
    from pseudolife_memory.rust_transport import RustTransport
    arrived = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            if self.path == "/stall":
                arrived.set()
                time.sleep(0.7)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b"ok")
            except (BrokenPipeError, ConnectionResetError):
                pass

    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow(), timeout=2) as client:
                    request = asyncio.create_task(client.get(url + "/stall"))
                    assert await asyncio.to_thread(arrived.wait, 2)
                    pid = transport.pid
                    request.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await request
                    assert (await client.get(url)).content == b"ok"
                    assert transport.pid == pid
            assert transport.pid is None
        asyncio.run(asyncio.wait_for(drive(), timeout=6))


def test_helper_restart_recovers_next_request_without_replaying(binary):
    from pseudolife_memory.rust_transport import RustTransport
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow()) as client:
                    assert (await client.get(url + "/first")).content == b"ok"
                    pid = transport.pid
                    transport._process.kill()
                    await transport._process.wait()
                    assert (await client.get(url + "/second")).content == b"ok"
                    assert transport.pid != pid
            import psutil
            assert not psutil.pid_exists(pid)
        asyncio.run(asyncio.wait_for(drive(), timeout=6))
    assert requests == ["/first", "/second"]


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_opaque_body_and_protocol_headers_match_python(binary, backend):
    from pseudolife_memory.rust_transport import RustTransport
    expected = b"\x00\xff\xf0opaque-future-payload"
    headers_seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            headers_seen.append({key: self.headers.get(key) for key in (
                "Mcp-Method", "Mcp-Name", "X-PL-Session", "X-PL-Agent-Key", "Future-Header")})
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow() if backend == "rust" else None) as client:
                    result = await client.post(url, content=expected, headers={
                        "Mcp-Method": "tools/call", "Mcp-Name": "future_tool",
                        "X-PL-Session": "fixture-session", "X-PL-Agent-Key": "fixture-key",
                        "Future-Header": "preserved"})
                    assert result.content == expected
        asyncio.run(drive())
    assert headers_seen == [{"Mcp-Method": "tools/call", "Mcp-Name": "future_tool",
                             "X-PL-Session": "fixture-session", "X-PL-Agent-Key": "fixture-key",
                             "Future-Header": "preserved"}]


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_environment_proxy_matches_python(binary, backend, monkeypatch):
    from pseudolife_memory.rust_transport import RustTransport
    received = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"proxy")

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    with listener(Proxy) as proxy:
        monkeypatch.setenv("HTTP_PROXY", proxy)
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow() if backend == "rust" else None) as client:
                    assert (await client.get("http://fixture.invalid/mcp")).content == b"proxy"
        asyncio.run(drive())
    assert received == ["http://fixture.invalid/mcp"]


@pytest.mark.parametrize("backend", ["python", "rust"])
@pytest.mark.parametrize("trust", ["ca", "leaf", "mixed"])
def test_https_custom_ca_and_untrusted_rejection_match_python(binary, backend, trust, monkeypatch, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from pseudolife_memory.rust_transport import RustTransport
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=trust == "ca", path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "ca.pem", tmp_path / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    trust_file = tmp_path / "trust.pem"
    trust_file.write_bytes(cert_file.read_bytes())
    if trust == "mixed":
        root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture root")])
        root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        trust_file.write_bytes(trust_file.read_bytes() + root.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"verified")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://localhost:{server.server_port}"
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    try:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow() if backend == "rust" else None) as client:
                    with pytest.raises(httpx2.ConnectError):
                        await client.get(url)
            monkeypatch.setenv("SSL_CERT_FILE", str(trust_file))
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow() if backend == "rust" else None) as client:
                    assert (await client.get(url)).content == b"verified"
                    assert transport.pid is None
        asyncio.run(asyncio.wait_for(drive(), timeout=10))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_real_stdio_mcp_metadata_pagination_and_errors_match_python(binary, backend, monkeypatch, tmp_path):
    from mcp.types import PaginatedRequestParams, RequestParamsMeta
    from tests.test_shim_transport_recovery import _proxy_client, _replace_token
    if backend == "rust":
        monkeypatch.setenv("PSEUDOLIFE_MCP_RUST_HTTP", str(binary))
    else:
        monkeypatch.delenv("PSEUDOLIFE_MCP_RUST_HTTP", raising=False)
    seen = []
    identity_headers = []
    thread_id = "11111111-1111-7111-8111-111111111111"
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "fixture-writer")
    instructions = "Fixture instructions: retain Ω and line breaks.\nSecond line."
    tool = {"name": "fixture", "title": "Fixture tool", "description": "Daemon schema",
            "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}}},
            "annotations": {"readOnlyHint": True, "idempotentHint": True}}
    result = {"content": [{"type": "text", "text": "opaque Ω"},
                          {"type": "image", "data": "Ynl0ZXM=", "mimeType": "image/png"}],
              "structuredContent": {"nested": {"values": [None, 1, True, "Ω"]}},
              "isError": False, "resultType": "complete"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            self.send_response(405)
            self.end_headers()
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = request["method"]
            seen.append((method, request.get("params")))
            if method == "tools/call":
                identity_headers.append({key: self.headers.get(key) for key in (
                    "X-PL-Writer", "X-PL-Session", "X-PL-Agent", "X-PL-Agent-Key",
                    "X-PL-Bank", "X-PL-Principal")})
            if "id" not in request:
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                value = {"protocolVersion": request["params"]["protocolVersion"],
                         "serverInfo": {"name": "fixture", "version": "1"},
                         "capabilities": {"tools": {}}, "instructions": instructions}
            elif method == "tools/list":
                value = {"tools": [tool], "nextCursor": "fixture-page-2"}
            elif method == "tools/call":
                value = result
            else:
                value = {}
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": value}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    token_file = tmp_path / "token"
    _replace_token(token_file, "fixture-new-secret")
    with listener(Handler) as url:
        async def drive():
            async with _proxy_client(SimpleNamespace(url=url), token_file, tmp_path / "stderr.log",
                                     operation_timeout=5, codex_metadata=True,
                                     agent_headers={"X-PL-Agent": "fixture-agent",
                                                    "X-PL-Agent-Key": "fixture-key",
                                                    "X-PL-Bank": "fixture-bank",
                                                    "X-PL-Principal": "fixture-principal"}) as client:
                assert client.instructions == instructions
                assert client.server_capabilities.resources is None
                assert client.server_capabilities.prompts is None
                listed = await client.list_tools(params=PaginatedRequestParams(cursor="fixture-page-1"))
                assert listed.next_cursor == "fixture-page-2"
                assert listed.tools[0].model_dump(by_alias=True, exclude_none=True) == tool
                returned = await client.call_tool("fixture", {"request_id": "fixture-idempotency-key"},
                    meta=RequestParamsMeta(threadId=thread_id))
                assert returned.model_dump(by_alias=True, exclude_none=True) == result
                for unsupported in (client.list_resources, client.list_prompts):
                    with pytest.raises(Exception) as caught:
                        await unsupported()
                    assert caught.value.error.code == -32601
        asyncio.run(asyncio.wait_for(drive(), timeout=10))
    lists = [params for method, params in seen if method == "tools/list"]
    assert lists == [{"_meta": {}, "cursor": "fixture-page-1"}]
    calls = [params for method, params in seen if method == "tools/call"]
    assert calls == [{"name": "fixture", "arguments": {"request_id": "fixture-idempotency-key"},
                     "_meta": {}}]
    assert identity_headers == [{"X-PL-Writer": "fixture-writer", "X-PL-Session": thread_id,
                                 "X-PL-Agent": "fixture-agent", "X-PL-Agent-Key": "fixture-key",
                                 "X-PL-Bank": "fixture-bank", "X-PL-Principal": "fixture-principal"}]


def test_response_cleanup_recycles_capacity(binary):
    from pseudolife_memory.rust_transport import RustTransport

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow()) as client:
                    for _ in range(101):
                        assert (await client.get(url)).content == b"ok"
        asyncio.run(asyncio.wait_for(drive(), timeout=10))


def test_distinct_write_timeout_selects_python_before_send(binary):
    from pseudolife_memory.rust_transport import RustTransport

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                timeout = httpx2.Timeout(5, write=1)
                async with httpx2.AsyncClient(transport=transport.borrow(), timeout=timeout) as client:
                    assert (await client.get(url)).content == b"ok"
                    assert transport.pid is None
        asyncio.run(asyncio.wait_for(drive(), timeout=10))


def test_changed_trust_environment_is_revalidated_before_next_send(binary, monkeypatch, tmp_path):
    from pseudolife_memory.rust_transport import RustTransport
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow()) as client:
                    assert (await client.get(url)).content == b"ok"
                    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca"))
                    with pytest.raises(httpx2.ConnectError):
                        await client.get(url)
        asyncio.run(asyncio.wait_for(drive(), timeout=10))
    assert seen == ["/"]

def test_fragmented_command_survives_earlier_exchange_completion(binary):
    first_seen, release_first = Event(), Event()
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/first":
                first_seen.set()
                release_first.wait(timeout=3)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
    with listener(Handler) as url:
        async def drive():
            process = await asyncio.create_subprocess_exec(str(binary), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            async def send(value):
                process.stdin.write(json.dumps(value).encode() + b"\n")
                await process.stdin.drain()
            def request(identifier, path):
                return {"type": "request", "id": identifier, "url": url+path,
                        "method": "GET", "body": "", "headers": [], "proxy": None,
                        "timeout": {"connect": 2, "read": 2, "write": 2, "pool": 2}}
            try:
                await send({"type":"configure","version":1,"origin":url,"certificates":None})
                assert json.loads(await process.stdout.readline())["type"] == "ready"
                await send(request(1, "/first"))
                assert await asyncio.to_thread(first_seen.wait, 2)
                raw = json.dumps(request(2, "/second")).encode() + b"\n"
                split = len(raw)//2
                process.stdin.write(raw[:split])
                await process.stdin.drain()
                await asyncio.sleep(0.05)
                release_first.set()
                while True:
                    event=json.loads(await process.stdout.readline())
                    if event["type"] == "end": break
                await asyncio.sleep(0.05)
                process.stdin.write(raw[split:])
                await process.stdin.drain()
                raw_head=await process.stdout.readline()
                assert raw_head, "fragmented second request lost its prefix"
                assert json.loads(raw_head)["id"] == 2
                while json.loads(await process.stdout.readline())["type"] != "end": pass
                assert seen == ["/first", "/second"]
            finally:
                release_first.set()
                if process.returncode is None: process.kill()
                await process.wait()
        asyncio.run(asyncio.wait_for(drive(), timeout=10))


@pytest.mark.skipif(os.name != "nt", reason="Windows console process contract")
def test_windows_helper_uses_pipes_without_console_host(binary, monkeypatch):
    import psutil
    from pseudolife_memory.rust_transport import RustTransport

    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    bodies = [b"fixture response", b"b" * 1_400_000]
    owned = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with listener(Handler) as url:
        async def drive():
            async with RustTransport(binary) as transport:
                async with httpx2.AsyncClient(transport=transport.borrow()) as client:
                    for body in bodies:
                        response = await client.post(url, content=body)
                        assert response.content == body
                    assert transport.pid is not None
                    helper = psutil.Process(transport.pid)
                    owned.append(helper)
                    # Observe the live persistent helper, after both ordinary
                    # and bulk pipe responses have completed.
                    await asyncio.sleep(0.1)
                    children = helper.children(recursive=True)
                    owned.extend(children)
                    assert "conhost.exe" not in [child.name().lower() for child in children]
        try:
            asyncio.run(asyncio.wait_for(drive(), timeout=10))
        finally:
            # Windows console shutdown may lag the helper's exit briefly.
            _, alive = psutil.wait_procs(owned, timeout=3)
            assert not alive


@pytest.mark.skipif(os.name != "nt", reason="Windows helper parent/pipe lifecycle")
@pytest.mark.parametrize("shutdown", ["stdin-eof", "parent-killed"])
def test_windows_helper_exits_without_parent_cleanup(binary, monkeypatch, shutdown):
    import psutil
    import subprocess
    import sys
    import textwrap

    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    release = Event()
    owned = []
    parent_code = textwrap.dedent("""
        import asyncio, json, os, sys
        from pathlib import Path
        import httpx2
        from pseudolife_memory.rust_transport import RustTransport

        async def drive():
            transport = RustTransport(Path(sys.argv[1]))
            client = httpx2.AsyncClient(transport=transport.borrow(), timeout=30)
            response = await client.send(client.build_request('GET', sys.argv[2]), stream=True)
            chunks = response.aiter_raw()
            assert await anext(chunks) == b'first'
            assert not response.is_closed and transport._pending
            print(json.dumps({'helper_pid': transport.pid}), flush=True)
            await asyncio.to_thread(sys.stdin.readline)
            transport._process.stdin.close()
            await transport._process.stdin.wait_closed()
            code = await asyncio.wait_for(transport._process.wait(), timeout=5)
            print(json.dumps({'helper_returncode': code}), flush=True)
            # Neither transport.aclose nor asyncio shutdown may reap the helper.
            os._exit(0)

        asyncio.run(drive())
    """)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"first")
            self.wfile.flush()
            release.wait(timeout=15)

    with listener(Handler) as url:
        async def drive():
            parent = await asyncio.create_subprocess_exec(
                sys.executable, "-c", parent_code, str(binary), url,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=Path(__file__).resolve().parents[1])
            try:
                raw = await asyncio.wait_for(parent.stdout.readline(), timeout=10)
                assert raw, "disposable parent did not finish its first piped response"
                helper = psutil.Process(json.loads(raw)["helper_pid"])
                owned.append(helper)
                children = helper.children(recursive=True)
                owned.extend(children)
                assert "conhost.exe" not in [child.name().lower() for child in children]
                if shutdown == "stdin-eof":
                    parent.stdin.write(b"close\n")
                    await parent.stdin.drain()
                    raw = await asyncio.wait_for(parent.stdout.readline(), timeout=7)
                    assert raw, "helper did not exit on stdin EOF with an active response"
                    assert json.loads(raw)["helper_returncode"] == 0
                else:
                    parent.kill()
                await asyncio.wait_for(parent.wait(), timeout=5)
                # The psutil process identity prevents PID reuse from fooling
                # this check; os.kill(pid, 0) is a control event on Windows.
                await asyncio.to_thread(helper.wait, timeout=5)
                _, alive = await asyncio.to_thread(psutil.wait_procs, owned, timeout=3)
                assert not alive, "helper survived its parent's pipes closing"
            finally:
                release.set()
                if parent.returncode is None:
                    owned.extend(psutil.Process(parent.pid).children(recursive=True))
                    parent.kill()
                await parent.wait()
                _, alive = await asyncio.to_thread(psutil.wait_procs, owned, timeout=3)
                for process in alive:
                    process.kill()
                _, alive = await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)
                assert not alive, "disposable helper cleanup failed"
        asyncio.run(asyncio.wait_for(drive(), timeout=25))


@pytest.fixture
def capacity_boundary(monkeypatch):
    from pseudolife_memory.rust_transport import RustTransport

    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    python_calls, rust_calls, cancellations = [], [], []

    class PythonFallback(httpx2.AsyncBaseTransport):
        def __init__(self, **_kwargs): pass
        async def handle_async_request(self, request):
            python_calls.append(request.url.path)
            return httpx2.Response(200, content=b"fixture", request=request)
        async def aclose(self): pass

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", PythonFallback)
    owner = RustTransport(Path("unused-helper"), origin="http://127.0.0.1:1")
    async def start(*_args):
        # Startup can yield after reserving capacity, as the real helper does.
        await asyncio.sleep(0)
    async def send(command):
        if command["type"] == "request":
            rust_calls.append(httpx2.URL(command["url"]).path)
            owner._pending[command["id"]].put_nowait(
                {"type": "head", "status": 200, "headers": []})
        elif command["type"] == "cancel":
            cancellations.append(command["id"])
    monkeypatch.setattr(owner, "_start", start)
    monkeypatch.setattr(owner, "_send", send)
    return SimpleNamespace(owner=owner, python_calls=python_calls,
                           rust_calls=rust_calls, cancellations=cancellations)


async def _retained_capacity_wave(boundary, prefix, *, concurrent=True, pool_timeout=0.05):
    owner = boundary.owner
    before_python, before_rust = len(boundary.python_calls), len(boundary.rust_calls)
    requests = [httpx2.Request("GET", f"http://127.0.0.1:1/{prefix}-{index}",
        extensions={"timeout": {"read": 5, "write": 5, "connect": 5, "pool": pool_timeout}})
        for index in range(101)]
    responses = []
    try:
        if concurrent:
            responses = await asyncio.gather(
                *(owner.handle_async_request(request) for request in requests), return_exceptions=True)
        else:
            for request in requests:
                responses.append(await owner.handle_async_request(request))
        assert [type(response).__name__ for response in responses
                if not isinstance(response, httpx2.Response)] == []
        python_paths = boundary.python_calls[before_python:]
        rust_paths = boundary.rust_calls[before_rust:]
        assert len(python_paths) == 1
        assert len(rust_paths) == 100
        assert set(python_paths).isdisjoint(rust_paths)
        assert set(python_paths + rust_paths) == {request.url.path for request in requests}
        assert len(owner._pending) == 100
    finally:
        for response in responses:
            if isinstance(response, httpx2.Response):
                await response.aclose()
                await response.aclose()
    assert owner._pending == {}


@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.parametrize("pool_timeout", [0, 0.05, None])
def test_capacity_overflow_selects_python_before_dispatch(capacity_boundary, concurrent, pool_timeout):
    async def drive():
        async with capacity_boundary.owner:
            # Repeat after closing retained heads: every slot must be returned
            # exactly once, including when response close is called twice.
            for prefix in ("first", "recycled"):
                await _retained_capacity_wave(capacity_boundary, prefix,
                    concurrent=concurrent, pool_timeout=pool_timeout)
    asyncio.run(asyncio.wait_for(drive(), timeout=5))


@pytest.mark.parametrize("stage", ["start", "send", "head", "head_error"])
def test_capacity_reservation_released_after_failure(capacity_boundary, monkeypatch, stage):
    owner = capacity_boundary.owner
    start, send = owner._start, owner._send
    reached, never = asyncio.Event(), asyncio.Event()

    async def interrupted_start(*args):
        if stage == "start":
            reached.set()
            await never.wait()
        await start(*args)

    async def interrupted_send(command):
        if command["type"] == "request":
            reached.set()
            if stage == "send":
                await never.wait()
            elif stage in {"head", "head_error"}:
                capacity_boundary.rust_calls.append(httpx2.URL(command["url"]).path)
                if stage == "head_error":
                    owner._pending[command["id"]].put_nowait({"type": "error", "kind": "timeout"})
                return
        await send(command)

    monkeypatch.setattr(owner, "_start", interrupted_start)
    monkeypatch.setattr(owner, "_send", interrupted_send)
    async def drive():
        async with owner:
            request = httpx2.Request("GET", "http://127.0.0.1:1/interrupted",
                extensions={"timeout": {"read": 5, "write": 5, "connect": 5, "pool": 0.05}})
            task = asyncio.create_task(owner.handle_async_request(request))
            await reached.wait()
            if stage == "head_error":
                with pytest.raises(httpx2.ReadTimeout):
                    await task
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert owner._pending == {}
            assert capacity_boundary.python_calls == []
            assert len(capacity_boundary.cancellations) == (0 if stage == "start" else 1)
            monkeypatch.setattr(owner, "_start", start)
            monkeypatch.setattr(owner, "_send", send)
            await _retained_capacity_wave(capacity_boundary, "after-failure")
    asyncio.run(asyncio.wait_for(drive(), timeout=5))
