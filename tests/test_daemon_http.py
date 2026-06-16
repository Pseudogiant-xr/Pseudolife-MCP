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
    h = daemon["health"]
    assert h["status"] == "ok" and h["schema"] == 8
    assert h["storage"] == "postgres" and h["auth"] is True


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


def test_non_loopback_with_trust_bind_allowed(tmp_path):
    """PSEUDOLIFE_MCP_TRUST_BIND bypasses the loopback guard (container case).

    The daemon should come up healthy on a 0.0.0.0 bind with no token —
    the container's port publish (not the bind host) is the boundary.
    """
    url = resolve_test_db_url()
    if not _pg_reachable(url):
        pytest.skip("no test Postgres reachable")
    port = _free_port()
    env = {
        **os.environ,
        "PSEUDOLIFE_MCP_HOST": "0.0.0.0",
        "PSEUDOLIFE_MCP_PORT": str(port),
        "PSEUDOLIFE_MCP_DATABASE_URL": url,
        "PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path),
        "PSEUDOLIFE_MCP_TRUST_BIND": "1",
    }
    env.pop("PSEUDOLIFE_MCP_TOKEN", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pseudolife_memory.cli", "serve"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 60
        health = None
        while time.time() < deadline:
            health = _health(port)
            if health is not None:
                break
            if proc.poll() is not None:
                pytest.fail(f"daemon exited early ({proc.returncode})")
            time.sleep(0.5)
        assert health is not None and health["status"] == "ok"
        assert health["auth"] is False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
