"""stdio shim — find (or start) the daemon, then proxy MCP over to it.

An MCP client launches this per session via the ``pseudolife-mcp`` script.
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
# Floor wait for a spawned daemon: torch import on a cold cache. The lite
# tier's true first boot costs more BEFORE the port binds (pg0 runtime
# extraction + initdb, then the torch import), so as long as the spawned
# child is still alive we keep waiting up to _SPAWN_WAIT_ALIVE_S instead
# of guessing — a dead child fails immediately. The cap is sized from a
# measured cold-cold first boot (see the constant's comment).
_SPAWN_WAIT_S = 25.0
# Measured 2026-08-14 (Windows 11, NVMe, warm HF cache): a cold-cold lite
# first boot — pg0 runtime extraction (~150 MB, Defender-scanned) +
# initdb + torch import — reached /health in 21.5 s, already at the edge
# of the 25 s floor on FAST hardware. 180 s gives slower disks/AV room;
# the child-liveness check above keeps genuine failures fast.
_SPAWN_WAIT_ALIVE_S = 180.0


def _daemon_url() -> str:
    return os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL", DEFAULT_URL).rstrip("/")


def probe_health(url: str, timeout: float = 0.25) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def spawn_daemon() -> subprocess.Popen:
    """Start ``pseudolife-mcp serve`` detached so it outlives this session."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: both keep the daemon off the
        # caller's console, but DETACHED_PROCESS leaves it *needing* one, and
        # Windows 11 hands that allocation to the default terminal app —
        # Windows Terminal then opens a real window and steals foreground
        # focus (same finding as ops/install-shim-autostart.ps1, 2026-07-12).
        # CREATE_NO_WINDOW skips console allocation entirely; the child still
        # outlives its spawner.
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:  # pragma: no cover - windows deployment
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        [sys.executable, "-m", "pseudolife_memory.cli", "serve"], **kwargs,
    )


def ensure_daemon(url: str) -> dict:
    health = probe_health(url)
    if health is not None:
        return health
    print(f"[shim] no daemon at {url} — starting one...", file=sys.stderr)
    child = spawn_daemon()
    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed >= _SPAWN_WAIT_ALIVE_S:
            break
        if elapsed >= _SPAWN_WAIT_S and child.poll() is not None:
            # The spawned daemon exited without serving — waiting longer
            # cannot help. (Before the floor, a poll() result can race
            # the detach on some platforms, so only trust it after.)
            print(
                f"[shim] the spawned daemon exited (code {child.returncode}) "
                f"before serving.", file=sys.stderr,
            )
            break
        time.sleep(0.5)
        health = probe_health(url, timeout=0.5)
        if health is not None:
            return health
    print(
        f"[shim] FAILED to reach the memory daemon at {url}.\n"
        f"  Docker tier:  docker compose -f ops/docker-compose.yml up -d\n"
        f"  Pip tiers:    pseudolife-mcp serve   (run it in a terminal — "
        f"the daemon logs to its own stderr, so this shows why it died)",
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


def _toolset_changed(result) -> bool:
    """True when a memory_toolset call actually moved the tier (its result
    carries ``changed: true``). Reads structured content first, falls back
    to the JSON text block."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        inner = structured.get("result")
        target = inner if isinstance(inner, dict) else structured
        return target.get("changed") is True
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            return json.loads(text).get("changed") is True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _proxy(url: str, token: str | None, session_uid: str) -> None:
    import contextlib

    import mcp.types as types
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.server.lowlevel import Server
    from mcp.server.lowlevel.server import NotificationOptions
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
        # The daemon's tools/list_changed lands on the per-call upstream
        # session above and dies with it, so a tier change would be invisible
        # to the real client — re-emit it on the downstream stdio session.
        if name == "memory_toolset" and _toolset_changed(result):
            try:
                await server.request_context.session.send_tool_list_changed()
            except Exception:  # noqa: BLE001 — notify is best-effort
                pass
        # Forward structured output too — the tools advertise an
        # outputSchema, so a content-only proxy would trip the
        # downstream client's structured-output validation.
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return result.content, structured
        return result.content

    async with stdio_server() as (r, w):
        await server.run(
            r, w, server.create_initialization_options(
                NotificationOptions(tools_changed=True)),
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
