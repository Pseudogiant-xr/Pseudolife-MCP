"""Daemon integration: health, token auth, tool round-trip, concurrency.

Spawns the real ``pseudolife-mcp serve`` process against the test DB so
the module-level singletons in ``mcp_server`` don't leak between tests.
Skips cleanly when no Postgres is reachable.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

# Keep spawned daemons off the desktop: without this flag, a child
# python.exe launched from a hidden/detached parent (pytest under an
# agent harness or CI wrapper) allocates its OWN console window and
# steals foreground focus on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
import urllib.error
import urllib.request

import pytest

from tests.pg_fixtures import resolve_test_db_url

psycopg = pytest.importorskip("psycopg")

_TOKEN = "test-secret-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg_reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _health(port: int, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def _post_mcp_with_host(port: int, host_header: str,
                        token: str | None = None) -> int:
    """POST a real ``initialize`` to /mcp over loopback while claiming an
    arbitrary ``Host``, and return the HTTP status.

    Built on raw ``http.client`` (``skip_host=True``) because the point of
    the probe IS the Host header — urllib would synthesise its own from the
    URL. A DNS-rebinding rejection surfaces as 421 from the SDK's transport
    security middleware, before auth or any handler runs.
    """
    import http.client

    import mcp.types as types

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": types.LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "host-header-probe", "version": "0"},
        },
    }).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.putrequest("POST", "/mcp", skip_host=True,
                        skip_accept_encoding=True)
        conn.putheader("Host", host_header)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Accept", "application/json, text/event-stream")
        if token:
            conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        conn.send(body)
        return conn.getresponse().status
    finally:
        conn.close()


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    url = resolve_test_db_url()
    if not _pg_reachable(url):
        pytest.skip("no test Postgres reachable")
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("daemon_data")
    env = {
        **os.environ,
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1",
        "PSEUDOLIFE_MCP_PORT": str(port),
        "PSEUDOLIFE_MCP_DATABASE_URL": url,
        "PSEUDOLIFE_MCP_DATA_DIR": str(data_dir),
        "PSEUDOLIFE_MCP_TOKEN": _TOKEN,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "pseudolife_memory.cli", "serve"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    deadline = time.time() + 60  # torch import is slow on a cold cache
    health = None
    while time.time() < deadline:
        health = _health(port)
        if health is not None:
            break
        if proc.poll() is not None:
            pytest.fail(f"daemon exited early ({proc.returncode})")
        time.sleep(0.5)
    if health is None:
        proc.terminate()
        pytest.fail("daemon never became healthy")
    yield {"port": port, "url": f"http://127.0.0.1:{port}", "health": health}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_health_unauthenticated(daemon):
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    h = daemon["health"]
    assert h["status"] == "ok" and h["schema"] == SCHEMA_META_VERSION
    assert h["storage"] == "postgres" and h["auth"] is True
    assert h["persist_errors"] == 0  # healthy: no swallowed save failures


def test_tool_call_requires_token(daemon):
    req = urllib.request.Request(
        daemon["url"] + "/mcp",
        data=b"{}",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 401


async def _call(url: str, tool: str, args: dict):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {_TOKEN}"}
    async with streamablehttp_client(url + "/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool(tool, args)


def _result_text(result) -> str:
    return " ".join(
        getattr(c, "text", "") for c in result.content
    )


def test_store_and_search_roundtrip(daemon):
    url = daemon["url"]
    store = asyncio.run(_call(
        url, "memory_store",
        {"text": "the vextra service default port is 9931", "source": "daemon-test"},
    ))
    assert "true" in _result_text(store).lower()
    found = asyncio.run(_call(
        url, "memory_search", {"query": "what port does vextra use?"},
    ))
    assert "9931" in _result_text(found)


def test_two_clients_no_lost_writes(daemon):
    url = daemon["url"]

    async def _interleave():
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        headers = {"Authorization": f"Bearer {_TOKEN}"}

        async def session_stores(tag: str, n: int):
            async with streamablehttp_client(url + "/mcp", headers=headers) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    for i in range(n):
                        await s.call_tool("memory_store", {
                            "text": f"concurrency probe {tag} item {i}",
                            "source": "concurrency",
                        })

        await asyncio.gather(session_stores("A", 6), session_stores("B", 6))

    asyncio.run(_interleave())
    recent = asyncio.run(_call(url, "memory_recent", {"n": 50}))
    text = _result_text(recent)
    for tag in ("A", "B"):
        for i in range(6):
            assert f"concurrency probe {tag} item {i}" in text


def test_non_loopback_without_token_refused():
    """A daemon told to bind 0.0.0.0 with no token must exit(2)."""
    env = {
        **os.environ,
        "PSEUDOLIFE_MCP_HOST": "0.0.0.0",
        "PSEUDOLIFE_MCP_PORT": str(_free_port()),
    }
    env.pop("PSEUDOLIFE_MCP_TOKEN", None)
    env.pop("PSEUDOLIFE_MCP_TRUST_BIND", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pseudolife_memory.cli", "serve"],
        env=env, capture_output=True, timeout=60,
    )
    assert proc.returncode == 2


def test_transport_security_policy_is_explicit_not_inherited():
    """The Host allowlist must be OUR decision, not the SDK's heuristic (#174).

    FastMCP auto-enables DNS-rebinding protection when it sees a loopback
    `host=` and disables it entirely otherwise. We pass neither host nor
    settings today, so the daemon inherits the loopback allowlist no matter
    what it actually binds — and naively forwarding the container's 0.0.0.0
    would flip the same heuristic to "no protection at all". Both directions
    are wrong; the policy keys on whether a bearer token gates the endpoint.
    """
    from pseudolife_memory import mcp_server

    tokenless = mcp_server.transport_security_for(auth_configured=False)
    assert tokenless.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in tokenless.allowed_hosts

    # The documented LAN recipe: PSEUDOLIFE_MCP_HOST=0.0.0.0 + a token.
    authenticated = mcp_server.transport_security_for(auth_configured=True)
    assert authenticated.enable_dns_rebinding_protection is False

    # And the shipped default the daemon carries before configuration is the
    # protected one — never whatever the SDK inferred.
    assert mcp_server.mcp.settings.transport_security is not None


@pytest.fixture(scope="module")
def trust_bind_daemon(tmp_path_factory):
    """A tokenless 0.0.0.0 daemon — the shipped container shape.

    ``PSEUDOLIFE_MCP_TRUST_BIND`` is the operator's assertion that the
    exposure boundary is external (compose publishes the port to 127.0.0.1
    only), so the bind guard lets it start without a token. Module-scoped:
    a daemon spawn costs a cold torch import, and two tests need this shape.
    """
    url = resolve_test_db_url()
    if not _pg_reachable(url):
        pytest.skip("no test Postgres reachable")
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("trust_bind_data")
    env = {
        **os.environ,
        "PSEUDOLIFE_MCP_HOST": "0.0.0.0",
        "PSEUDOLIFE_MCP_PORT": str(port),
        "PSEUDOLIFE_MCP_DATABASE_URL": url,
        "PSEUDOLIFE_MCP_DATA_DIR": str(data_dir),
        "PSEUDOLIFE_MCP_TRUST_BIND": "1",
    }
    env.pop("PSEUDOLIFE_MCP_TOKEN", None)
    env.pop("PSEUDOLIFE_MCP_TOKENS", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pseudolife_memory.cli", "serve"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    deadline = time.time() + 60
    health = None
    while time.time() < deadline:
        health = _health(port)
        if health is not None:
            break
        if proc.poll() is not None:
            pytest.fail(f"daemon exited early ({proc.returncode})")
        time.sleep(0.5)
    if health is None:
        proc.terminate()
        pytest.fail("trust-bind daemon never became healthy")
    yield {"port": port, "health": health}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_non_loopback_with_trust_bind_allowed(trust_bind_daemon):
    """PSEUDOLIFE_MCP_TRUST_BIND bypasses the loopback guard (container case).

    The daemon should come up healthy on a 0.0.0.0 bind with no token —
    the container's port publish (not the bind host) is the boundary.
    """
    health = trust_bind_daemon["health"]
    assert health["status"] == "ok"
    assert health["auth"] is False


def test_remote_host_header_reaches_mcp_when_authenticated(daemon):
    """A token-gated daemon must serve /mcp under ANY Host header (#174).

    The documented LAN recipe (bind + PSEUDOLIFE_MCP_TOKEN, remote client at
    the LAN URL) — and equally a reverse proxy, a Tailscale name, or a
    compose service name — sends a Host the daemon never chose. The SDK's
    DNS-rebinding allowlist rejected all of those with 421 before auth ran,
    so /health and /api worked while every MCP call failed. With a token
    configured the bearer gate is the boundary, exactly as the Console's
    own `_browser_gate` already reasons for /api.
    """
    status = _post_mcp_with_host(
        daemon["port"], f"10.0.0.5:{daemon['port']}", token=_TOKEN)
    assert status != 421, (
        "MCP endpoint rejected a non-loopback Host with 421 Invalid Host "
        "header — the documented LAN/proxy recipe is broken")
    assert status == 200


def test_tokenless_daemon_still_rejects_hostile_host(trust_bind_daemon):
    """DNS-rebinding protection is retained where it actually protects.

    No token configured means no bearer gate in front of /mcp, and step 6 of
    the Console app deliberately does not run `_browser_gate` on it — so the
    SDK's Host allowlist is the only thing standing between a rebinding
    browser and the bank. This is the shipped container default; it must
    keep 421ing a foreign Host.
    """
    port = trust_bind_daemon["port"]
    assert _post_mcp_with_host(port, "evil.example.com") == 421
    # ...while the loopback Host the container's port publish presents is
    # still served.
    assert _post_mcp_with_host(port, f"127.0.0.1:{port}") == 200
