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

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import NoReturn

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
# How long the no-spawn path waits for an EXTERNAL daemon to appear. Sized
# for the 2026-08-29 incident's scenario — a Claude session starting at
# logon while Docker Desktop is still booting after a reboot: Docker's
# port proxy arrived within seconds of the shim's failed probe there, and
# a cold Docker Desktop start is typically tens of seconds to a couple of
# minutes, so the spawn ceiling above is a comfortable cap for this too.
_NO_SPAWN_WAIT_S = _SPAWN_WAIT_ALIVE_S


def _daemon_url() -> str:
    return os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL", DEFAULT_URL).rstrip("/")


def probe_health(url: str, timeout: float = 0.25) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # An HTTP response IS a daemon: /health serves degraded payloads as
        # 503, and urlopen raises on those. Swallowing them as "no daemon"
        # made the shim spawn a second daemon over a reachable-but-refusing
        # one — whose bind then failed on the held port, so the refusal
        # diagnostic in the 503 body reached no one (2026-08-29 review).
        try:
            return json.loads(e.read().decode())
        except Exception:  # noqa: BLE001 — a non-JSON error page is not ours
            return None
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


def _notice_if_cortex_is_inert(health: dict) -> dict:
    """Say, once per session, when the bank cannot fill its own cortex.

    The lite tier (``pip install "pseudolife-mcp[lite]"``) ships no
    extractor, so dream consolidation writes no canonical facts —
    ``memory_fact_set`` becomes the only cortex writer. The daemon logs
    that at startup, but :func:`spawn_daemon` sends its stderr to DEVNULL,
    so this is the one place the user can meet it. Silence here reads as
    "the cortex is broken"; the note is short and names the fix.

    Only fires on an explicit ``extractor: "none"`` — a daemon predating
    the field, a configured extractor, and a deliberately dream-disabled
    bank all stay quiet.
    """
    if health.get("extractor") == "none":
        print(
            "[shim] no dream extractor configured: memories are stored and "
            "searchable, but consolidation writes no canonical facts — "
            "memory_fact_set is the only cortex writer.\n"
            "  Fix with any OpenAI-compatible endpoint, e.g. a local Ollama:\n"
            "    PSEUDOLIFE_DREAM_BASE_URL=http://localhost:11434/v1\n"
            "    PSEUDOLIFE_DREAM_MODEL=qwen2.5:7b\n"
            "  (set both in the daemon's environment, then restart it; the "
            "Docker tier ships an extractor sidecar instead)",
            file=sys.stderr,
        )
    return health


def _spawn_disabled() -> bool:
    """True when this install opted out of the daemon-spawn fallback.

    The Docker-tier installers set ``PSEUDOLIFE_MCP_NO_SPAWN=1`` on the
    shim registration: there the real daemon is the compose container, and
    a spawned host-side fallback is never right — after the 2026-08-29
    reboot, Docker Desktop was still booting when the shim probed, the
    fallback's bind beat Docker's port proxy (whose publish then failed
    silently), and it served a retired file bank in place of the real one.
    """
    raw = os.environ.get("PSEUDOLIFE_MCP_NO_SPAWN", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _spawn_lock_path(url: str):
    """Lock file keyed on the daemon URL — every shim aiming at the same
    daemon contends on the same file, whichever Python install it runs
    from (the incident's double spawn was one venv shim plus one
    global-env shim). Lives in the temp dir (per-user on Windows, shared
    on POSIX); a foreign-owned file there just fails the open, which
    degrades to the unlocked behavior rather than blocking anyone."""
    from pathlib import Path

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"pseudolife-mcp-spawn-{digest}.lock"


def _open_spawn_lock(url: str):
    """Open (never lock) the spawn-lock file; ``None`` if that fails.

    The lock is best-effort protection against a concurrent shim's spawn —
    a filesystem that can't even open the file must degrade to the old
    unlocked behavior, not brick the session.
    """
    try:
        return open(_spawn_lock_path(url), "a+b")
    except OSError:
        return None


def _try_spawn_lock(fh) -> bool:
    """Non-blocking exclusive lock. OS locks die with their holder, so a
    crashed spawner never leaves a stale lock behind."""
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - exercised on the Linux CI lane
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_spawn_lock(fh, held: bool) -> None:
    try:
        if held:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on the Linux CI lane
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        fh.close()


def _accept_health(url: str, health: dict) -> dict:
    """Vet a probed /health payload before the shim commits to this daemon.

    ``init_refusal`` means the daemon booted but refuses to serve its bank
    (the hydrated-dim guard, or schema.py's vector-dim refusal): it holds
    the port, so spawning over it can only fail — print ITS diagnosis (the
    only channel a shim-spawned daemon has: spawn_daemon devnulls stderr)
    and exit. Any other degraded state still attaches — per-call upstream
    connections recover on their own once e.g. Postgres is back — but
    leaves one honest line so a failing session has a lead.
    """
    refusal = health.get("init_refusal")
    if refusal:
        print(
            f"[shim] the daemon at {url} is up but refusing to serve:\n"
            f"  {refusal}",
            file=sys.stderr,
        )
        sys.exit(1)
    if health.get("status") not in (None, "ok"):
        print(
            f"[shim] note: the daemon at {url} reports status="
            f"{health.get('status')}"
            + (f" (db: {health['db']})" if health.get("db") else ""),
            file=sys.stderr,
        )
    return _notice_if_cortex_is_inert(health)


def _exit_unreachable(url: str) -> NoReturn:
    no_spawn_note = (
        "  (PSEUDOLIFE_MCP_NO_SPAWN is set, so no fallback daemon was "
        "spawned.)\n" if _spawn_disabled() else "")
    print(
        f"[shim] FAILED to reach the memory daemon at {url}.\n"
        f"{no_spawn_note}"
        f"  Docker tier:  docker compose -f ops/docker-compose.yml up -d\n"
        f"  Pip tiers:    pseudolife-mcp serve   (run it in a terminal — "
        f"the daemon logs to its own stderr, so this shows why it died)",
        file=sys.stderr,
    )
    sys.exit(1)


def ensure_daemon(url: str) -> dict:
    health = probe_health(url)
    if health is not None:
        return _accept_health(url, health)
    if _spawn_disabled():
        # Docker-tier install: the daemon is external (compose), so wait
        # for it instead of racing its port bind with a fallback spawn.
        print(
            f"[shim] no daemon at {url} and PSEUDOLIFE_MCP_NO_SPAWN is set — "
            f"waiting up to {_NO_SPAWN_WAIT_S:.0f}s for it instead of "
            f"spawning a fallback (Docker may still be starting)...",
            file=sys.stderr,
        )
        start = time.time()
        while time.time() - start < _NO_SPAWN_WAIT_S:
            time.sleep(0.5)
            health = probe_health(url, timeout=0.5)
            if health is not None:
                return _accept_health(url, health)
        _exit_unreachable(url)
    lock = _open_spawn_lock(url)
    held = False
    try:
        if lock is not None:
            waiting_announced = False
            wait_start = time.time()
            while not _try_spawn_lock(lock):
                # Another shim holds the lock, i.e. is mid-spawn: wait for
                # ITS daemon rather than racing it with a second one (the
                # incident's 13:39 double spawn, venv + global env).
                if not waiting_announced:
                    print(
                        f"[shim] no daemon at {url} — another shim is "
                        f"already starting one; waiting for it...",
                        file=sys.stderr,
                    )
                    waiting_announced = True
                health = probe_health(url, timeout=0.5)
                if health is not None:
                    return _accept_health(url, health)
                if time.time() - wait_start >= _SPAWN_WAIT_ALIVE_S:
                    _exit_unreachable(url)
                time.sleep(0.5)
            held = True
            # The previous holder may have brought the daemon up between
            # our first probe and taking the lock — re-check before
            # spawning over it.
            health = probe_health(url)
            if health is not None:
                return _accept_health(url, health)
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
                    f"[shim] the spawned daemon exited (code "
                    f"{child.returncode}) before serving.", file=sys.stderr,
                )
                break
            time.sleep(0.5)
            health = probe_health(url, timeout=0.5)
            if health is not None:
                return _accept_health(url, health)
        _exit_unreachable(url)
    finally:
        if lock is not None:
            _release_spawn_lock(lock, held)


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
    to the JSON text block. (v2 types are snake_case: ``structured_content``.)"""
    structured = (getattr(result, "structured_content", None)
                  or getattr(result, "structuredContent", None))
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

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import (
        create_mcp_http_client, streamable_http_client)
    from mcp.server import Server
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.server.stdio import stdio_server
    from mcp.server.subscriptions import (
        InMemorySubscriptionBus, ListenHandler, ToolsListChanged)

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
        # that whole failure class; under the 2026-07-28 stateless protocol a
        # connection is nothing but the HTTP exchange anyway. Writer/session
        # attribution (X-PL-Writer / X-PL-Session) rides the httpx client's
        # headers on every request (SDK v2 moved headers off the transport
        # helper onto the http_client).
        async with create_mcp_http_client(headers=headers or None) as http:
            async with streamable_http_client(
                url + "/mcp", http_client=http,
            ) as (read, write):
                async with ClientSession(read, write) as remote:
                    await remote.initialize()
                    yield remote

    # v2 low-level handlers are constructor params taking (ctx, params) and
    # returning result types verbatim. The proxy registers NO tool schemas of
    # its own — the DAEMON is the validating authority (v1 needed
    # validate_input=False plus content/structured juggling to preserve
    # that; v2's pass-through result types make it the default).

    async def _list_tools(ctx, params):
        # Forward pagination params verbatim — swallowing a client cursor
        # would replay page 1 forever if the daemon ever paginates.
        async with _upstream() as remote:
            return await remote.list_tools(params=params)

    async def _call_tool(ctx, params):
        async with _upstream() as remote:
            # Seed the output-schema cache: v2's call_tool otherwise fetches
            # the full 36-tool manifest (list_tools) on every call to
            # revalidate structured output — and this session is fresh per
            # call by design. None = known, no schema, no validation; the
            # DAEMON is the validating authority, exactly as on v1.
            remote._tool_output_schemas[params.name] = None
            result = await remote.call_tool(params.name, params.arguments or {})
        # The daemon's tools/list_changed lands on the per-call upstream
        # session above and dies with it, so a tier change would be invisible
        # to the real client — re-emit it downstream on BOTH eras: the
        # subscription bus for 2026-07-28 clients (whose outbound path drops
        # plain session notifications) and the session send for handshake-era
        # clients. A failed call cannot have changed the tier.
        if (not result.is_error and params.name == "memory_toolset"
                and _toolset_changed(result)):
            try:
                await bus.publish(ToolsListChanged())
            except Exception:  # noqa: BLE001 — notify is best-effort
                pass
            try:
                await ctx.session.send_tool_list_changed()
            except Exception:  # noqa: BLE001 — notify is best-effort
                pass
        return result

    # Serving subscriptions/listen is ALSO what makes 2026-07-28 capability
    # derivation advertise tools.listChanged — without it, modern clients
    # are told the list never changes and the bus has no outlet.
    bus = InMemorySubscriptionBus()
    server = Server(
        "pseudolife-memory",
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
        on_subscriptions_listen=ListenHandler(bus),
    )

    async with stdio_server() as (r, w):
        await server.run(
            r, w, server.create_initialization_options(
                NotificationOptions(tools_changed=True)),
        )


# The capability :func:`_proxy` actually needs, probed as a module so the
# guard tracks the code's real requirement rather than a version string.
_SDK_V2_PROBE_MODULE = "mcp.server.subscriptions"


def _require_mcp_sdk_v2() -> None:
    """Exit with the recovery command when this env's MCP SDK predates v2.

    The registered shim command can live outside the repo venv — an
    editable install runs the working copy's current code against whatever
    SDK that environment has installed — so a dep-floor bump in pyproject
    strands such an env on an SDK missing the modules :func:`_proxy`
    imports. Without this guard that surfaces as a raw ModuleNotFoundError
    on the shim's stderr and "Connection closed" in the MCP client, with
    no hint that the fix is one pip command (seen live 2026-08-28, mcp
    1.28.1 in a global env crashing every session start since the v2
    migration merged).
    """
    try:
        present = importlib.util.find_spec(_SDK_V2_PROBE_MODULE) is not None
    except ImportError:
        # find_spec raises ModuleNotFoundError when a PARENT package is
        # absent (mcp not installed at all), and a broken partial install
        # can raise any ImportError from the parent's own import — same
        # remedy either way.
        present = False
    if present:
        return
    try:
        from importlib.metadata import version
        installed = version("mcp")
    except Exception:  # noqa: BLE001 - absence reads the same as too-old
        installed = "not installed"
    print(
        f"[shim] this environment's MCP SDK (mcp {installed}) predates v2 — "
        f"the shim needs mcp>=2.1 (no {_SDK_V2_PROBE_MODULE}).\n"
        f'  Fix:  "{sys.executable}" -m pip install -U "mcp>=2.1,<3"\n'
        f"  (or re-run the repo installer, which registers the project "
        f"venv's shim)",
        file=sys.stderr,
    )
    sys.exit(1)


def run_shim() -> None:
    import asyncio

    _require_mcp_sdk_v2()
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
