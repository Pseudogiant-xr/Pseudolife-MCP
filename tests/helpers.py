"""Small test doubles and utilities shared across otherwise unrelated suites.

Lives here rather than in ``conftest.py`` because these are plain callables,
not pytest fixtures: importing them explicitly keeps each test file honest
about what it depends on. Dream-specific doubles live in
``tests/dream_helpers.py``, the console app's ASGI driver in
``tests/asgi_helpers.py``.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import importlib
import json
import os
import re
import socket

import torch


def unit_vec(seed: int, dim: int = 8) -> torch.Tensor:
    """A deterministic unit embedding — the cortex suites' stand-in for the
    real embedder, so store-level tests stay fast and offline."""
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(dim, generator=g)
    return v / v.norm()


def reload_mcp_filemode(tmp_path, monkeypatch):
    """Reimport ``mcp_server`` bound to a throwaway file-mode data dir.

    The module builds its ``service`` singleton at import time from the
    environment, so tool-layer tests must reload it after pointing
    ``PSEUDOLIFE_MCP_DATA_DIR`` at a tmp dir; the DATABASE_URL delete forces
    file mode even on a machine with the bench Postgres configured.
    """
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


def free_port() -> int:
    """An unused loopback port, for the suites that spawn a real daemon."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pg_reachable(url: str, timeout: float = 3.0) -> bool:
    """Whether the bench Postgres answers — the PG suites' skip gate.

    Returns ``False`` for an absent server unless
    ``PSEUDOLIFE_REQUIRE_TEST_POSTGRES=1``; RAISES ``PostgresAuthError``
    for a server that answered but rejected the credentials (that is a
    misconfiguration, never a skip). ``psycopg`` is imported here rather
    than at module scope so a machine without it can still import this
    module for the non-PG helpers; every caller gates itself with
    ``pytest.importorskip("psycopg")`` first.
    """
    import psycopg

    from tests.pg_defaults import (
        PostgresAuthError,
        PostgresSetupError,
        PostgresUnavailableError,
        RedactedUrl,
        auth_failure_message,
        is_auth_failure,
        is_server_unavailable,
        setup_failure_message,
    )

    # pytest prints this frame's arguments in the report when the error
    # below escapes; the redacting repr keeps the password out of it.
    url = RedactedUrl(url)
    try:
        with psycopg.connect(url, connect_timeout=timeout):
            return True
    except Exception as exc:  # noqa: BLE001
        if is_auth_failure(exc):
            # A reachable server that rejects the password is a
            # misconfiguration, never "not reachable" — surface it. `from
            # None`: psycopg's own frames carry the full conninfo (password
            # included) as an argument, and the FATAL text is in our message.
            raise PostgresAuthError(
                auth_failure_message(url.host, exc, url)
            ) from None
        if is_server_unavailable(exc):
            if os.environ.get("PSEUDOLIFE_REQUIRE_TEST_POSTGRES") == "1":
                # Do not include the raw connection error or DSN: drivers
                # may include credentials in either.
                raise PostgresUnavailableError(
                    "Test PostgreSQL is required but unavailable"
                ) from None
            return False
        raise PostgresSetupError(
            setup_failure_message(url.host, exc, url)
        ) from None


@contextmanager
def private_bank(url: str, label: str):
    """A private database on the test server, for a daemon of its own.

    A bank has exactly one writer (the writer lease), so a daemon that runs
    beside another one, or that may outlive its test (a shim-autostarted
    daemon is only reaped on Windows), needs a bank of its own, as it would
    in any real deployment. Yields the bank's URL. On exit the database is
    dropped ``WITH (FORCE)``, which also cuts off a daemon that outlived its
    test. Locally the name keeps the run's pid as its last ``_`` part, so a
    hard-killed run's leftover is pruned like the run's own database
    (``pg_fixtures._prune_dead_run_dbs``).
    """
    import psycopg

    from tests.pg_defaults import (
        RedactedUrl, conninfo_dbname, conninfo_with_dbname)

    base = conninfo_dbname(url)
    name = (re.sub(r"_(\d+)$", rf"_{label}_\1", base)
            if re.search(r"_\d+$", base) else f"{base}_{label}")
    admin = RedactedUrl(conninfo_with_dbname(url, "postgres"))
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield RedactedUrl(conninfo_with_dbname(url, name))
    finally:
        try:
            with psycopg.connect(admin, autocommit=True,
                                 connect_timeout=5) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        except Exception:  # noqa: BLE001 — best effort; pruning covers leftovers
            pass


SERVE_ARGV = ("-m", "pseudolife_memory.cli", "serve")
# The daemon runs from the checkout under test: ``-m`` resolves the package
# from the working directory first, and from anywhere else a machine with an
# editable install of another checkout would run that checkout's code.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# asyncio's own wording for a failed listen (uvicorn logs the OSError and
# exits 1), on Windows and POSIX alike.
_BIND_FAILED = "error while attempting to bind"


def _owns_listener(proc, port: int) -> bool | None:
    """Whether ``proc`` (or a child of it: a venv's python.exe is a
    launcher that runs the real interpreter as its child) listens on
    ``port``. ``None`` when the OS will not say."""
    import psutil

    try:
        root = psutil.Process(proc.pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return None
    for p in procs:
        # net_connections() is psutil 6+; pyproject allows 5.9.
        listing = getattr(p, "net_connections", None) or p.connections
        try:
            conns = listing(kind="tcp")
        except psutil.AccessDenied:
            return None
        except psutil.Error:
            continue
        if any(c.status == psutil.CONN_LISTEN and c.laddr
               and c.laddr.port == port for c in conns):
            return True
    return False


def _is_run_database(database_url: str) -> bool:
    """Whether ``database_url`` names this run's own test database."""
    from tests.pg_defaults import conninfo_dbname

    try:
        dbname = conninfo_dbname(database_url)
    except ValueError:
        return False  # not a DSN at all (a stand-in daemon's placeholder)
    from tests.pg_fixtures import _target_db_name

    return dbname == _target_db_name()


def spawn_serve(data_dir, database_url: str, *,
                env_extra: dict | None = None, wait_s: float = 60.0,
                argv: tuple[str, ...] = SERVE_ARGV, attempts: int = 3):
    """Start the real ``pseudolife-mcp serve`` daemon and wait for /health.

    Returns ``(proc, health, port)``; raises ``RuntimeError`` (with the
    daemon's stderr tail) if the process exits early or never answers. The
    60 s default matches the daemon suites: a cold torch import is slow.

    Refuses the run's own database with ``ValueError``: in-process tests
    hold its writer lease, and a daemon there boots degraded (see
    :func:`serve_on_private_bank`, which every daemon fixture uses).

    The port is picked here, not by the caller, because a picked port is
    only free until something else binds it. A daemon that loses its port
    logs the failed bind (and may take a while to exit: its warmup holds
    the service lock its exit flush needs), and a daemon that is still
    booting when something else answers /health on its port is not the one
    answering; either way it is stopped and started again on a fresh port,
    up to ``attempts`` times. ``argv`` replaces the module arguments after
    ``sys.executable``, for tests of this helper.

    ``CREATE_NO_WINDOW`` is not optional on Windows — a child python.exe
    launched from a hidden/detached parent otherwise allocates its own
    console window and steals foreground focus (measured 2026-07-21).

    An ``env_extra`` entry whose value is ``None`` REMOVES that variable from
    the child's environment (how a caller runs a token-less daemon on a
    machine that exports ``PSEUDOLIFE_MCP_TOKEN``).

    Used by the module-scoped daemon fixtures whose tests only need *a* live
    daemon to talk to (through :func:`serve_on_private_bank`); a test whose
    subject IS the spawn (the shim's autostart) must not use this.
    """
    import os
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    # Before any spawn: a missing psutil must not leave a daemon running.
    import psutil  # noqa: F401 — used by _owns_listener

    if _is_run_database(database_url):
        raise ValueError(
            "a test daemon never serves the run's own database; use "
            "tests.helpers.serve_on_private_bank")
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    stderr = ""
    for attempt in range(1, attempts + 1):
        port = free_port()
        env = {
            **os.environ,
            "PSEUDOLIFE_MCP_HOST": "127.0.0.1",
            "PSEUDOLIFE_MCP_PORT": str(port),
            "PSEUDOLIFE_MCP_DATABASE_URL": database_url,
            "PSEUDOLIFE_MCP_DATA_DIR": str(data_dir),
            **(env_extra or {}),
        }
        env = {k: v for k, v in env.items() if v is not None}
        stderr_path = Path(data_dir) / f"daemon-stderr-{attempt}.log"
        with stderr_path.open("wb") as stderr_log:
            proc = subprocess.Popen(
                [sys.executable, *argv], env=env, cwd=_REPO_ROOT,
                stdout=subprocess.DEVNULL, stderr=stderr_log,
                creationflags=no_window,
            )

        lost_port = False
        deadline = time.time() + wait_s
        while True:
            exited = proc.poll() is not None
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            if _BIND_FAILED in stderr:
                lost_port = True
                break
            if exited:
                raise RuntimeError(
                    f"daemon exited early ({proc.returncode}):\n"
                    + stderr[-4000:])
            if time.time() >= deadline:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1.0
                ) as r:
                    health = json.loads(r.read().decode())
            except Exception:  # noqa: BLE001 — not up yet
                time.sleep(0.5)
                continue
            if (_owns_listener(proc, port) is not False
                    and proc.poll() is None):
                return proc, health, port
            lost_port = True  # something else answers on our port
            break
        stop_daemon(proc)
        if not lost_port:
            raise RuntimeError("daemon never became healthy:\n"
                               + stderr[-4000:])
    raise RuntimeError(f"daemon lost its port on all {attempts} attempts; "
                       "last stderr:\n" + stderr[-4000:])


def stop_daemon(proc) -> None:
    """Terminate a :func:`spawn_serve` daemon, killing it if it lingers."""
    import subprocess

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@contextmanager
def serve_on_private_bank(label: str, data_dir, *,
                          env_extra: dict | None = None):
    """A live daemon on a bank of its own, for a module's daemon fixture.

    Never on the run's own database: a bank has one writer (the writer
    lease), and in-process tests build PG-backed services on that database
    without all closing them. A dropped one keeps the lease until Python's
    cyclic GC frees it, and only the next ``pg_conn`` reaps its backend, so
    a daemon booted on the run's database in that gap came up degraded and
    refused every tool call for its whole module: three tests/test_shim.py
    failures in each of two full runs on 2026-09-25, never in isolation.

    Skips when no test Postgres is reachable. Yields ``port``, ``url``,
    ``db`` (the bank's DSN), ``health`` and ``data_dir``; stops the daemon
    and drops the bank on exit.
    """
    import pytest

    from tests.pg_fixtures import resolve_test_db_url

    url = resolve_test_db_url()
    if not pg_reachable(url):
        pytest.skip("no test Postgres reachable")
    with private_bank(url, label) as bank_url:
        proc, health, port = spawn_serve(data_dir, bank_url,
                                         env_extra=env_extra)
        try:
            yield {"port": port, "url": f"http://127.0.0.1:{port}",
                   "db": bank_url, "health": health, "data_dir": data_dir}
        finally:
            stop_daemon(proc)


def invoke_tool(tool_name: str, args: dict) -> dict:
    """Call a registered MCP tool through FastMCP and parse the JSON result.

    SDK v1 FastMCP returned ``(content_list, structured_dict)`` (or bare
    content); v2 MCPServer returns a ``CallToolResult``. All three shapes are
    handled here so the tool-layer suites do not each carry the version
    knowledge. The structured payload is what a real MCP client uses, so it
    wins when present.
    """
    from pseudolife_memory import mcp_server  # noqa: PLC0415

    result = asyncio.run(mcp_server.mcp.call_tool(tool_name, args))
    if isinstance(result, tuple):
        content, structured = result
    elif hasattr(result, "content"):
        content = result.content
        structured = getattr(result, "structured_content", None)
    else:
        content, structured = result, None
    if structured is not None:
        return structured
    return json.loads(
        "".join(item.text for item in content if hasattr(item, "text")))
