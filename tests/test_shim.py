"""stdio shim: auto-starts the daemon and proxies tool calls over stdio."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from tests.pg_fixtures import resolve_test_db_url

psycopg = pytest.importorskip("psycopg")


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg_reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def test_shim_autostarts_daemon_and_proxies(tmp_path):
    url = resolve_test_db_url()
    if not _pg_reachable(url):
        pytest.skip("no test Postgres reachable")

    port = _free_port()
    env = {
        **os.environ,
        "PSEUDOLIFE_MCP_DAEMON_URL": f"http://127.0.0.1:{port}",
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1",
        "PSEUDOLIFE_MCP_PORT": str(port),
        "PSEUDOLIFE_MCP_DATABASE_URL": url,
        "PSEUDOLIFE_MCP_DATA_DIR": str(tmp_path),
    }
    env.pop("PSEUDOLIFE_MCP_TOKEN", None)  # loopback, no token needed

    async def _drive():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pseudolife_memory.cli"],  # no arg -> shim
            env=env,
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                assert "memory_store" in tools and "memory_stats" in tools
                res = await s.call_tool("memory_stats", {})
                text = " ".join(getattr(c, "text", "") for c in res.content)
                assert "bands" in text

    import asyncio
    try:
        asyncio.run(asyncio.wait_for(_drive(), timeout=90))
    finally:
        # The shim spawned a detached daemon — reap it via /health port.
        import urllib.request
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            )
        except Exception:  # noqa: BLE001
            pass
        # Best-effort: kill any serve process bound to our port.
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                 f"-ErrorAction SilentlyContinue | "
                 f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force "
                 f"-ErrorAction SilentlyContinue }}"],
                capture_output=True,
            )
