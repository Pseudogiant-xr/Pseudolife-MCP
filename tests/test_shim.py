"""stdio shim: auto-starts the daemon and proxies tool calls over stdio."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from pseudolife_memory import shim as _shim
from tests.helpers import (free_port as _free_port,
                           pg_reachable as _pg_reachable,
                           spawn_serve as _spawn_serve,
                           stop_daemon as _stop_daemon)
from tests.pg_fixtures import resolve_test_db_url

pytest.importorskip("psycopg")

# Outer async guard for tests that may autostart a daemon. It must
# DOMINATE the shim's own two-tier spawn wait (dead-child floor +
# live-child ceiling): 2026-08-14, two full-suite runs under concurrent
# GPU eval load pushed legitimate autostarts past the then-hard 25 s
# deadline, and the shim's exit(1) surfaced here as an opaque McpError.
# Derived rather than hardcoded so a future bump of the shim's ceiling
# cannot silently re-open the gap between the test's patience and the
# shim's contract.
_OUTER_TIMEOUT_S = _shim._SPAWN_WAIT_ALIVE_S + 60


def _shim_env(port: int, data_dir, **extra) -> dict:
    """The environment a shim subprocess is driven with: point it at a daemon
    URL/port and a data dir, and drop the token (loopback needs none)."""
    env = {
        **os.environ,
        "PSEUDOLIFE_MCP_DAEMON_URL": f"http://127.0.0.1:{port}",
        "PSEUDOLIFE_MCP_HOST": "127.0.0.1",
        "PSEUDOLIFE_MCP_PORT": str(port),
        "PSEUDOLIFE_MCP_DATA_DIR": str(data_dir),
        **extra,
    }
    env.pop("PSEUDOLIFE_MCP_TOKEN", None)  # loopback, no token needed
    return env


@pytest.fixture(scope="module")
def shared_daemon(tmp_path_factory):
    """ONE already-running daemon for the shim tests that only need something
    to proxy to.

    ``shim.ensure_daemon`` probes /health before spawning and reuses a live
    daemon, so a shim pointed at this port never starts its own — each test
    still gets its own shim process and its own assertions, but pays for one
    daemon boot per module instead of one per test (~7.7 s each). Same shape
    as tests/test_daemon_http.py's module fixture.

    ``test_shim_autostarts_daemon_and_proxies`` deliberately does NOT use
    this: spawning is its subject. Neither does the ``TOOLSET=minimal`` test,
    which needs a daemon booted with a different toolset tier.
    """
    url = resolve_test_db_url()
    if not _pg_reachable(url):
        pytest.skip("no test Postgres reachable")
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("shim_daemon")
    # Token removed rather than blanked: the shims below send no Authorization
    # header, so a daemon that inherited PSEUDOLIFE_MCP_TOKEN would 401 them.
    proc, _ = _spawn_serve(port, data_dir, url,
                           env_extra={"PSEUDOLIFE_MCP_TOKEN": None})
    try:
        yield {"port": port, "data_dir": data_dir}
    finally:
        _stop_daemon(proc)


def test_shim_autostarts_daemon_and_proxies(tmp_path):
    """Keeps its own free port and lets the shim START the daemon — that
    spawn is the subject, so this one must never see ``shared_daemon``."""
    url = resolve_test_db_url()
    if not _pg_reachable(url):
        pytest.skip("no test Postgres reachable")

    port = _free_port()
    env = _shim_env(port, tmp_path, PSEUDOLIFE_MCP_DATABASE_URL=url)

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
        asyncio.run(asyncio.wait_for(_drive(), timeout=_OUTER_TIMEOUT_S))
    finally:
        _reap_daemon(port)


def test_shim_survives_idle_gap(shared_daemon):
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

    The 8 s idle sleep below is deliberate and stays — it IS the test. Only
    the daemon is shared (see ``shared_daemon``); the shim is still this
    test's own process.
    """
    import asyncio

    env = _shim_env(shared_daemon["port"], shared_daemon["data_dir"])

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

    asyncio.run(asyncio.wait_for(_drive(), timeout=_OUTER_TIMEOUT_S))


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

    # Its own daemon on purpose: the tier expansion needs one booted at the
    # minimal toolset, which ``shared_daemon`` is not.
    port = _free_port()
    env = _shim_env(port, tmp_path, PSEUDOLIFE_MCP_DATABASE_URL=url,
                    PSEUDOLIFE_MCP_TOOLSET="minimal")  # world tools hidden
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
                assert init.capabilities.tools.list_changed is True

                tools = {t.name for t in (await s.list_tools()).tools}
                assert "memory_world_search" not in tools  # minimal tier

                res = await s.call_tool("memory_toolset", {"action": "expand"})
                text = " ".join(getattr(c, "text", "") for c in res.content)
                assert '"changed":true' in text.replace(" ", "").lower()

                await asyncio.wait_for(list_changed.wait(), timeout=10)

                tools = {t.name for t in (await s.list_tools()).tools}
                assert "memory_world_search" in tools  # core tier now

    try:
        asyncio.run(asyncio.wait_for(_drive(), timeout=_OUTER_TIMEOUT_S))
    finally:
        _reap_daemon(port)


def test_shim_forwards_stringified_list_param(shared_daemon):
    """A JSON-in-a-string list param must survive the shim (#175).

    Claude Desktop/Code send `tags` as `'["decision"]'` rather than a real
    array. FastMCP registers its call-tool handler with `validate_input=False`
    precisely so its `pre_parse_json` rescue can unwrap that before the model
    binds — which is why the same call has always worked over direct HTTP.
    The shim registered a bare `@server.call_tool()`, taking the SDK's
    `validate_input=True` default, so it re-validated the RAW arguments
    against the daemon's own inputSchema and failed the call before it was
    ever forwarded. This is the mechanism behind the project's long-standing
    "MCP anyOf-param stringification" note; the daemon is the validating
    authority, not the proxy.
    """
    import asyncio

    env = _shim_env(shared_daemon["port"], shared_daemon["data_dir"])

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
                res = await s.call_tool("memory_store", {
                    "text": "stringified-tags probe: the shim must forward "
                            "JSON-in-a-string list params untouched",
                    "source": "shim-stringify-test",
                    "tags": json.dumps(["decision"]),  # '["decision"]'
                })
                text = " ".join(getattr(c, "text", "") for c in res.content)
                assert not res.is_error, (
                    f"shim rejected a stringified list param: {text}")
                assert "validation error" not in text.lower()
                assert '"stored":true' in text.replace(" ", "").lower()

    asyncio.run(asyncio.wait_for(_drive(), timeout=_OUTER_TIMEOUT_S))


def test_shim_surfaces_the_daemons_real_error_message(shared_daemon):
    """An upstream tool error must reach the client verbatim (#175 follow-up).

    Dropping the shim's own input validation means genuinely bad arguments —
    the ones `pre_parse_json` cannot rescue, like an int param given a
    non-numeric string — now travel to the daemon and come back as an error
    `CallToolResult`: `isError=True`, a TextContent carrying the real reason,
    and `structuredContent=None`. The old return path handed that
    content-without-structured to the shim's OWN output validation, and since
    every tool is annotated `-> dict` the shim has an outputSchema, so the
    client saw "Output validation error: outputSchema defined but no
    structured output returned" — a message about the proxy's plumbing,
    masking the daemon's actual diagnosis. An error result is now passed
    through as-is (the SDK's lowlevel server returns a `types.CallToolResult`
    unchanged, preserving `isError` and text).
    """
    import asyncio

    env = _shim_env(shared_daemon["port"], shared_daemon["data_dir"])

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
                # memory_recent(n: int); "many" is not JSON, so pre_parse_json
                # cannot rescue it and the daemon rejects it on the merits.
                res = await s.call_tool("memory_recent", {"n": "many"})
                text = " ".join(getattr(c, "text", "") for c in res.content)
                assert res.is_error, (
                    f"an unrescuable bad arg must surface as an error: {text}")
                assert "output validation error" not in text.lower(), (
                    f"the shim's own outputSchema masked the daemon's real "
                    f"error message: {text}")
                assert "integer" in text.lower(), (
                    f"the daemon's real validation message should name the "
                    f"expected type: {text}")

    asyncio.run(asyncio.wait_for(_drive(), timeout=_OUTER_TIMEOUT_S))


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


def test_spawn_daemon_never_allocates_a_console_window(monkeypatch):
    """The auto-started daemon must not cost the user a window.

    ``DETACHED_PROCESS`` gives the child no console but leaves it *needing*
    one, and on Windows 11 with Windows Terminal as the default terminal app
    WT takes that allocation and opens a real window that steals foreground
    focus — the same finding ops/install-shim-autostart.ps1 recorded live on
    2026-07-12. Measured here 2026-07-21 with a window watcher over a real
    suite run: three ``test_shim`` daemon spawns produced three focus-stealing
    ``WindowsTerminal.exe`` windows; swapping the flag to ``CREATE_NO_WINDOW``
    produced zero, with detachment (child outlives its spawner) intact.
    """
    if sys.platform != "win32":
        pytest.skip("windows-only console-allocation semantics")
    from pseudolife_memory import shim

    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(shim.subprocess, "Popen", fake_popen)
    shim.spawn_daemon()

    flags = seen["kwargs"]["creationflags"]
    assert not flags & subprocess.DETACHED_PROCESS, (
        "DETACHED_PROCESS defers console allocation to the default terminal "
        "app, which opens a focus-stealing window")
    assert flags & subprocess.CREATE_NO_WINDOW, (
        "CREATE_NO_WINDOW skips console allocation entirely")
    # Still its own group: Ctrl+C in the caller's console must not reach it.
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP


def test_shipped_package_never_spawns_with_detached_process():
    """Guard the class, not just today's one site.

    The 2026-07-20 pass added CREATE_NO_WINDOW to the *test files'* own daemon
    spawns but left ``shim.spawn_daemon`` on DETACHED_PROCESS, so the windows
    came straight back — the shipped shim was the actual source all along.
    Any future spawn in the package has to make the same choice.
    """
    import io
    import tokenize
    from pathlib import Path

    def _code_only(path: Path) -> str:
        """Source with comments and strings dropped.

        The flag name is legitimate in a comment explaining why it is not
        used — only a real reference to it should fail this guard.
        """
        src = path.read_text(encoding="utf-8", errors="ignore")
        try:
            return " ".join(
                t.string
                for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.COMMENT, tokenize.STRING)
            )
        except (tokenize.TokenError, IndentationError):  # pragma: no cover
            return src

    pkg = Path(__file__).resolve().parents[1] / "pseudolife_memory"
    offenders = sorted(
        p.relative_to(pkg.parent).as_posix()
        for p in pkg.rglob("*.py")
        if "DETACHED_PROCESS" in _code_only(p)
    )
    assert offenders == [], (
        f"DETACHED_PROCESS in shipped code: {offenders} — use "
        f"CREATE_NO_WINDOW so no console window is ever allocated")


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


# ── Spawn hardening (2026-08-29 port-shadowing incident) ─────────────────────
#
# After a Windows reboot, Docker Desktop was still booting when the morning
# session's shim probed the daemon port, so the shim spawned a host-side
# fallback daemon. Its bind beat Docker's port proxy (which then failed its
# publish silently), and the fallback served a retired 384-d file bank in
# place of the real Docker bank. Later, two shims racing each spawned one
# more daemon apiece. These tests pin the two shim-side defenses: the
# Docker-tier no-spawn marker, and the cross-process spawn lock.


def test_probe_health_returns_the_payload_of_a_degraded_daemon(monkeypatch):
    """A 503 response IS a daemon. ``urlopen`` raises ``HTTPError`` on it,
    and swallowing that as "no daemon" would make the shim spawn a second
    daemon over a reachable-but-refusing one — whose bind then fails on the
    held port, so the refusal diagnostic in the 503 body reaches no one."""
    import io
    import urllib.error

    from pseudolife_memory import shim

    body = json.dumps({"status": "degraded", "init_refusal": "dim mismatch"})

    def fake_urlopen(url, timeout=0.25):
        raise urllib.error.HTTPError(
            url, 503, "Service Unavailable", None, io.BytesIO(body.encode()))

    monkeypatch.setattr(shim.urllib.request, "urlopen", fake_urlopen)
    health = shim.probe_health("http://127.0.0.1:8765")
    assert health is not None
    assert health["init_refusal"] == "dim mismatch"


def test_shim_surfaces_a_boot_refusal_instead_of_spawning_over_it(
        monkeypatch, capsys):
    """A daemon that is up but refusing (``init_refusal`` in /health — the
    hydrated-dim guard) holds the port, so spawning over it can only fail.
    The shim must print the daemon's own diagnosis and exit."""
    from pseudolife_memory import shim

    monkeypatch.delenv("PSEUDOLIFE_MCP_NO_SPAWN", raising=False)
    monkeypatch.setattr(
        shim, "spawn_daemon",
        lambda: pytest.fail("must not spawn over a refusing daemon"))
    refusal = ("Refusing to serve: 12 hydrated row(s) are embedded at 384 "
               "dims, but the live embedder produces 1024-d vectors")
    monkeypatch.setattr(
        shim, "probe_health",
        lambda url, timeout=0.25: {"status": "degraded",
                                   "init_refusal": refusal})
    with pytest.raises(SystemExit) as exc:
        shim.ensure_daemon("http://127.0.0.1:8765")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Refusing to serve" in err and "384" in err


def test_shim_attaches_to_a_degraded_daemon_and_says_so(monkeypatch, capsys):
    """Degraded WITHOUT a refusal (e.g. ``db: error`` while Postgres
    restarts) still attaches — per-call upstream connections recover on
    their own — but leaves one honest line about the state."""
    from pseudolife_memory import shim

    payload = {"status": "degraded", "db": "error: connection refused"}
    monkeypatch.setattr(
        shim, "probe_health", lambda url, timeout=0.25: dict(payload))
    health = shim.ensure_daemon("http://127.0.0.1:8765")
    assert health["status"] == "degraded"
    assert "degraded" in capsys.readouterr().err


def test_no_spawn_env_waits_for_the_daemon_instead_of_spawning(monkeypatch):
    """With PSEUDOLIFE_MCP_NO_SPAWN set, the shim must wait for the real
    daemon to come up (Docker starting slower than the session) and never
    spawn a fallback whose bind would race Docker's port proxy."""
    from pseudolife_memory import shim

    monkeypatch.setenv("PSEUDOLIFE_MCP_NO_SPAWN", "1")
    monkeypatch.setattr(
        shim, "spawn_daemon",
        lambda: pytest.fail("must never spawn under PSEUDOLIFE_MCP_NO_SPAWN"))
    monkeypatch.setattr(shim.time, "sleep", lambda s: None)

    calls = {"n": 0}

    def fake_probe(url, timeout=0.25):
        calls["n"] += 1
        return {"status": "ok"} if calls["n"] >= 3 else None

    monkeypatch.setattr(shim, "probe_health", fake_probe)
    health = shim.ensure_daemon("http://127.0.0.1:8765")
    assert health["status"] == "ok"


def test_no_spawn_env_times_out_loudly_with_the_docker_remedy(
        monkeypatch, capsys):
    """When the daemon never appears, the no-spawn path must exit 1 with a
    message that names the env var (so the reader knows why nothing was
    spawned) and the Docker recovery command."""
    from pseudolife_memory import shim

    monkeypatch.setenv("PSEUDOLIFE_MCP_NO_SPAWN", "true")
    monkeypatch.setattr(
        shim, "spawn_daemon",
        lambda: pytest.fail("must never spawn under PSEUDOLIFE_MCP_NO_SPAWN"))
    monkeypatch.setattr(shim, "probe_health", lambda url, timeout=0.25: None)
    monkeypatch.setattr(shim, "_NO_SPAWN_WAIT_S", 0.0, raising=False)
    with pytest.raises(SystemExit) as exc:
        shim.ensure_daemon("http://127.0.0.1:8765")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "PSEUDOLIFE_MCP_NO_SPAWN" in err
    assert "docker compose" in err


def test_no_spawn_env_falsy_value_still_spawns(monkeypatch, tmp_path):
    """`PSEUDOLIFE_MCP_NO_SPAWN=0` (and unset) must keep today's autostart
    behavior — the marker is a Docker-tier opt-out, not a default change."""
    from pseudolife_memory import shim

    monkeypatch.setenv("PSEUDOLIFE_MCP_NO_SPAWN", "0")
    # Never contend on the real %TEMP% lock a live shim may hold.
    monkeypatch.setattr(
        shim, "_spawn_lock_path", lambda url: tmp_path / "spawn.lock",
        raising=False)
    monkeypatch.setattr(shim.time, "sleep", lambda s: None)

    spawned = []

    class _Child:
        returncode = None

        def poll(self):
            return None

    def fake_spawn():
        spawned.append(1)
        return _Child()

    monkeypatch.setattr(shim, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(
        shim, "probe_health",
        lambda url, timeout=0.25: {"status": "ok"} if spawned else None)
    health = shim.ensure_daemon("http://127.0.0.1:8765")
    assert health["status"] == "ok"
    assert len(spawned) == 1


def test_concurrent_shims_spawn_exactly_one_daemon(monkeypatch, tmp_path):
    """Failure mode 3 of the incident: two shims racing at logon each
    spawned a host daemon (one venv, one global Python311 — the package is
    installed in both). The cross-process spawn lock must make the loser
    wait for the winner's daemon instead of starting a second one."""
    import threading

    from pseudolife_memory import shim

    monkeypatch.delenv("PSEUDOLIFE_MCP_NO_SPAWN", raising=False)
    monkeypatch.setattr(
        shim, "_spawn_lock_path", lambda url: tmp_path / "spawn.lock",
        raising=False)

    spawned: list[float] = []
    guard = threading.Lock()

    class _Child:
        returncode = None

        def poll(self):
            return None

    def fake_spawn():
        with guard:
            spawned.append(time.time())
        return _Child()

    def fake_probe(url, timeout=0.25):
        # The "daemon" becomes healthy 1.5s after the first spawn — long
        # enough that a second racing shim probes at least once while it
        # is still down (the pre-fix double-spawn window).
        with guard:
            first = spawned[0] if spawned else None
        if first is not None and time.time() - first > 1.5:
            return {"status": "ok"}
        return None

    monkeypatch.setattr(shim, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(shim, "probe_health", fake_probe)

    results: list[dict] = []

    def run() -> None:
        results.append(shim.ensure_daemon("http://127.0.0.1:18765"))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 2
    assert all(r["status"] == "ok" for r in results)
    assert len(spawned) == 1, f"both racing shims spawned a daemon: {spawned}"


def test_spawn_lock_is_released_after_the_daemon_is_up(monkeypatch, tmp_path):
    """The lock guards only the spawn window — once ensure_daemon returns,
    a later shim (say, after the daemon dies) must be able to take it."""
    from pseudolife_memory import shim

    monkeypatch.delenv("PSEUDOLIFE_MCP_NO_SPAWN", raising=False)
    monkeypatch.setattr(
        shim, "_spawn_lock_path", lambda url: tmp_path / "spawn.lock",
        raising=False)
    monkeypatch.setattr(shim.time, "sleep", lambda s: None)

    spawned = []

    class _Child:
        returncode = None

        def poll(self):
            return None

    def fake_spawn():
        spawned.append(1)
        return _Child()

    monkeypatch.setattr(shim, "spawn_daemon", fake_spawn)
    monkeypatch.setattr(
        shim, "probe_health",
        lambda url, timeout=0.25: {"status": "ok"} if spawned else None)
    health = shim.ensure_daemon("http://127.0.0.1:18766")
    assert health["status"] == "ok"
    assert len(spawned) == 1

    fh = shim._open_spawn_lock("http://127.0.0.1:18766")
    assert fh is not None
    assert shim._try_spawn_lock(fh), "spawn lock was not released"
    shim._release_spawn_lock(fh, held=True)


def test_sdk_guard_passes_on_a_v2_environment():
    from pseudolife_memory import shim

    # The test venv satisfies pyproject's ``mcp>=2.1`` floor, so the guard
    # must be a silent no-op there.
    assert shim._require_mcp_sdk_v2() is None


def test_sdk_guard_names_the_fix_and_exits_before_daemon_traffic(
        monkeypatch, capsys):
    """A pre-v2 SDK must die with the recovery command, not a traceback.

    The registered shim command can live outside the repo venv (an editable
    install runs the working copy's code against whatever SDK that env has),
    so a dep-floor bump strands it on an SDK missing the modules ``_proxy``
    imports. Seen live 2026-08-28: a global-env shim on mcp 1.28.1 crashed
    with a raw ModuleNotFoundError at every session start, which the MCP
    client log surfaced only as "Connection closed". The guard must fire
    BEFORE any daemon probe or spawn, and its message must name the exact
    interpreter and pip command that fix the environment.
    """
    from pseudolife_memory import shim

    monkeypatch.setattr(
        shim, "_SDK_V2_PROBE_MODULE", "pseudolife_test_absent_module",
        raising=False)
    monkeypatch.setattr(
        shim, "probe_health",
        lambda *a, **k: pytest.fail("guard must fire before daemon traffic"))
    with pytest.raises(SystemExit) as exc:
        shim.run_shim()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "mcp>=2.1" in err
    assert sys.executable in err

def test_sdk_guard_survives_a_fully_absent_mcp(monkeypatch, capsys):
    # find_spec RAISES (rather than returning None) when the probe module's
    # parent package is absent or broken — mcp not installed at all, or a
    # partial install whose parent import fails. The guard must land on the
    # same recovery message, not propagate the ImportError it was built to
    # replace.
    from pseudolife_memory import shim

    monkeypatch.setattr(
        shim, "_SDK_V2_PROBE_MODULE", "pseudolife_test_absent_parent.sub",
        raising=False)
    with pytest.raises(SystemExit) as exc:
        shim._require_mcp_sdk_v2()
    assert exc.value.code == 1
    assert "mcp>=2.1" in capsys.readouterr().err