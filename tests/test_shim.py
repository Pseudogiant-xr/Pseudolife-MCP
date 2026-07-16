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
        _reap_daemon(port)


def test_shim_survives_idle_gap(tmp_path):
    """Two calls separated by an idle gap must both succeed.

    Behavioural guard for the per-call upstream design. NOTE: this does NOT
    reproduce the original production hang on its own — that was triggered by
    Docker's loopback proxy reaping the idle connection (Desktop -> host shim
    -> *Docker* daemon), and this test connects straight to a host uvicorn with
    no proxy in between, so the old persistent-session shim passes it too. What
    it does guard: that the shim survives an idle pause and serves sequential
    calls, so a future change can't reintroduce a session that wedges after
    idle even without the proxy. The real fix is structural — the shim no
    longer holds any long-lived upstream connection that *could* be reaped.
    """
    import asyncio

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

        def _has_bands(res) -> bool:
            return "bands" in " ".join(getattr(c, "text", "") for c in res.content)

        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                assert _has_bands(await s.call_tool("memory_stats", {}))
                # Idle past uvicorn's default keep-alive (5s): a persistent
                # upstream would be reaped here and the next call would hang.
                await asyncio.sleep(8.0)
                assert _has_bands(await s.call_tool("memory_stats", {}))

    try:
        asyncio.run(asyncio.wait_for(_drive(), timeout=120))
    finally:
        _reap_daemon(port)


def test_shim_forwards_list_changed_on_toolset_expand(tmp_path):
    """The 2026-07-16 morning-brief regression: a tier expansion must reach
    the REAL client. The shim's per-call upstream design means the daemon's
    tools/list_changed lands on an ephemeral session and dies there — the
    shim itself must (a) advertise tools.listChanged downstream and (b) emit
    the notification when memory_toolset reports changed=true. The final
    list_tools also proves the session override survives per-call reconnects
    (X-PL-Session keying)."""
    import asyncio

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
        "PSEUDOLIFE_MCP_TOOLSET": "minimal",  # world tools start hidden
    }
    env.pop("PSEUDOLIFE_MCP_TOKEN", None)  # loopback, no token needed
    env.pop("PSEUDOLIFE_MCP_TIER_MAP", None)

    async def _drive():
        import mcp.types as types
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "pseudolife_memory.cli"],  # no arg -> shim
            env=env,
        )

        list_changed = asyncio.Event()

        async def _on_message(message) -> None:
            root = getattr(message, "root", message)
            if isinstance(root, types.ToolListChangedNotification):
                list_changed.set()

        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w, message_handler=_on_message) as s:
                init = await s.initialize()
                assert init.capabilities.tools.listChanged is True

                tools = {t.name for t in (await s.list_tools()).tools}
                assert "memory_world_search" not in tools  # minimal tier

                res = await s.call_tool("memory_toolset", {"action": "expand"})
                text = " ".join(getattr(c, "text", "") for c in res.content)
                assert '"changed":true' in text.replace(" ", "").lower()

                await asyncio.wait_for(list_changed.wait(), timeout=10)

                tools = {t.name for t in (await s.list_tools()).tools}
                assert "memory_world_search" in tools  # core tier now

    try:
        asyncio.run(asyncio.wait_for(_drive(), timeout=120))
    finally:
        _reap_daemon(port)


# ── Session identity + lifecycle (unit; no daemon) ────────────────────────────


def test_session_headers_include_writer_and_session(monkeypatch):
    from pseudolife_memory import shim

    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "writer-7")
    headers = shim._session_headers(token="tok", session_uid="uid-123")
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-PL-Writer"] == "writer-7"
    assert headers["X-PL-Session"] == "uid-123"


def test_post_episode_is_best_effort(monkeypatch):
    from pseudolife_memory import shim

    def boom(*a, **k):
        raise OSError("daemon down")

    monkeypatch.setattr(shim.urllib.request, "urlopen", boom)
    # Must NOT raise — episode bookkeeping can never break a session.
    shim._post_episode("http://127.0.0.1:8765", None, "/api/episode/start",
                       {"session_key": "x", "title": "t"})


def _reap_daemon(port: int) -> None:
    """Best-effort cleanup of the detached daemon the shim auto-spawned."""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "win32":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue | "
             f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force "
             f"-ErrorAction SilentlyContinue }}"],
            capture_output=True,
        )
