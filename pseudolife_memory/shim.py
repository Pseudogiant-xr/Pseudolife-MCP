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
import uuid

from pseudolife_memory.session_title import title_from_cwd

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


def _session_headers(token: str | None, session_uid: str) -> dict[str, str]:
    """Headers that ride every upstream call. ``X-PL-Writer`` attributes the
    writer (v0.4 keying); ``X-PL-Session`` is this shim's stable per-session id
    — the daemon keys episode stamping by it so concurrent sessions don't
    cross-contaminate."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    writer_id = os.environ.get("PSEUDOLIFE_WRITER_ID")
    if writer_id:
        headers["X-PL-Writer"] = writer_id
    headers["X-PL-Session"] = session_uid
    return headers


def _post_episode(url: str, token: str | None, path: str, payload: dict) -> None:
    """Best-effort REST call to open/close the session episode. Swallows every
    error so episode bookkeeping can never break or slow a Claude session."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url + path, data=data, method="POST")
        req.add_header("content-type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except Exception:  # noqa: BLE001
        pass


async def _proxy(url: str, token: str | None, session_uid: str) -> None:
    import contextlib

    import mcp.types as types
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    headers = _session_headers(token, session_uid)
    @contextlib.asynccontextmanager
    async def _upstream():
        # A FRESH upstream connection per call. The shim owns no state and the
        # daemon owns the bank, so a short-lived connection costs only a local
        # handshake and CANNOT go stale. A single long-lived session (the prior
        # design) gets reaped after an idle gap — uvicorn's keep-alive (~5s) and
        # Docker's loopback proxy both drop idle connections — and the mcp client
        # has no reconnect, so the first call after an idle pause hung on a dead
        # stream until the client timeout (~4 min). Per-call connect sidesteps
        # that whole failure class. Writer attribution (X-PL-Writer) rides every
        # connection's headers, so it survives; only the daemon-side session_id
        # (audit granularity, not correctness) becomes per-call.
        async with streamablehttp_client(url + "/mcp", headers=headers or None) as (
            read, write, _get_session_id,
        ):
            async with ClientSession(read, write) as remote:
                await remote.initialize()
                yield remote

    server: Server = Server("pseudolife-memory")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        async with _upstream() as remote:
            return (await remote.list_tools()).tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None):
        async with _upstream() as remote:
            result = await remote.call_tool(name, arguments or {})
            # Forward structured output too — the tools advertise an
            # outputSchema, so a content-only proxy would trip the
            # downstream client's structured-output validation.
            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                return result.content, structured
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
    # One shim == one Claude session. This uid keys BOTH the session episode
    # (opened/closed here) and per-store stamping (rides every call as
    # X-PL-Session), so lifecycle and attribution always agree — no dependency
    # on Claude's session_id (which MCP servers don't receive).
    session_uid = uuid.uuid4().hex
    _post_episode(url, token, "/api/episode/start", {
        "session_key": session_uid,
        "title": title_from_cwd(os.getcwd()),
    })
    try:
        asyncio.run(_proxy(url, token, session_uid))
    except KeyboardInterrupt:  # session closed
        pass
    finally:
        # Close the session episode (prune-on-empty if it captured nothing).
        _post_episode(url, token, "/api/episode/end",
                      {"session_key": session_uid})
