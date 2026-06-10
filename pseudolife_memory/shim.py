"""stdio shim — find (or start) the daemon, then proxy MCP over to it.

Claude Code launches this per session via the ``pseudolife-mcp`` script.
It owns NO storage and loads NO models: one daemon process holds the
bank, every session attaches through here (or directly over HTTP).

Failure contract: if the daemon can't be reached or started within the
startup budget, exit loudly with the exact recovery commands — never
fall back to embedded storage (that would reintroduce multi-writer
state, the v0.1 bug class).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"
_SPAWN_WAIT_S = 25.0  # daemon import (torch) can take a while on cold cache


def _daemon_url() -> str:
    return os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL", DEFAULT_URL).rstrip("/")


def probe_health(url: str, timeout: float = 0.25) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def spawn_daemon() -> None:
    """Start ``pseudolife-mcp serve`` detached so it outlives this session."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:  # pragma: no cover - windows deployment
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-m", "pseudolife_memory.cli", "serve"], **kwargs,
    )


def ensure_daemon(url: str) -> dict:
    health = probe_health(url)
    if health is not None:
        return health
    print(f"[shim] no daemon at {url} — starting one...", file=sys.stderr)
    spawn_daemon()
    deadline = time.time() + _SPAWN_WAIT_S
    while time.time() < deadline:
        time.sleep(0.5)
        health = probe_health(url, timeout=0.5)
        if health is not None:
            return health
    print(
        f"[shim] FAILED to reach the memory daemon at {url}.\n"
        f"  Check:  docker compose -f ops/docker-compose.yml up -d\n"
        f"  Then:   pseudolife-mcp serve   (or re-run this shim)\n"
        f"  Logs:   the daemon logs to its own stderr; run serve in a "
        f"terminal to see why it died.",
        file=sys.stderr,
    )
    sys.exit(1)


async def _proxy(url: str, token: str | None) -> None:
    import mcp.types as types
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(url + "/mcp", headers=headers) as (
        read, write, _get_session_id,
    ):
        async with ClientSession(read, write) as remote:
            await remote.initialize()
            remote_tools = (await remote.list_tools()).tools

            server: Server = Server("pseudolife-memory")

            @server.list_tools()
            async def _list_tools() -> list[types.Tool]:
                return remote_tools

            @server.call_tool()
            async def _call_tool(name: str, arguments: dict | None):
                result = await remote.call_tool(name, arguments or {})
                return result.content

            async with stdio_server() as (r, w):
                await server.run(
                    r, w, server.create_initialization_options(),
                )


def run_shim() -> None:
    import asyncio

    url = _daemon_url()
    ensure_daemon(url)
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None
    try:
        asyncio.run(_proxy(url, token))
    except KeyboardInterrupt:  # session closed
        pass
