"""Shared Postgres test fixtures (Phase 1).

Resolution order for the test server:

1. ``PSEUDOLIFE_TEST_DATABASE_URL`` env var (any reachable PG 16+vector).
2. The repo's dev container at ``127.0.0.1:5433`` (ops/docker-compose.yml).

If neither is reachable, PG-backed tests skip cleanly so the pure-logic
suites stay runnable anywhere.

Without the env override, each pytest process gets its own private
database (``pseudolife_memory_test_<pid>``), dropped at interpreter
exit. This is load-bearing, not cosmetic: ``pg_conn`` reaps every other
backend on its database before truncating, so two concurrent suite runs
sharing one database would terminate each other's live connections
(AdminShutdown on a different victim set every run). A private database
scopes the reaper to this run's own leaked backends.
"""

from __future__ import annotations

import atexit
import os
import sys

import pytest

psycopg = pytest.importorskip("psycopg")

_DEFAULT_ADMIN = "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres"
# Per-run private database — see module docstring. A fixed name here would
# reintroduce the concurrent-run reaper crossfire.
_TEST_DB = f"pseudolife_memory_test_{os.getpid()}"

# The truncate list. Was a hand-maintained copy of the FK-free tables
# CASCADE cannot reach — which is a list that has to be re-derived on every
# schema bump, and it had already drifted (`communities` was missing) while
# the eval harness's own copy had drifted much further (#181, 2026-08-25).
# One list now, defined beside the DDL and completeness-checked by
# tests/test_bench_reset_tables.py.
from pseudolife_memory.storage.schema import (  # noqa: E402
    BENCH_RESET_TABLES as _ALL_TABLES,
)


def _admin_url() -> str:
    url = os.environ.get("PSEUDOLIFE_TEST_DATABASE_URL")
    if url:
        # Point at the server's postgres db for admin ops.
        base, _, _db = url.rpartition("/")
        return base + "/postgres"
    return _DEFAULT_ADMIN


def _with_worker_suffix(db: str) -> str:
    """Append the xdist worker id to an overridden database name.

    A verbatim override shared by N workers would put every worker's
    backend reaper on one database — the exact cross-run crossfire this
    module's per-PID naming prevents, relocated inside a single CI job.
    Applied by BOTH ``_target_db_name`` (what ``ensure_test_db`` creates)
    and ``resolve_test_db_url`` (what tests connect to), so the two can
    never disagree about which database the run means. Suffixed CI
    databases are never dropped — the override contract leaves lifecycle
    to the caller, and CI runners are discarded at job end.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    return f"{db}_{worker}" if worker else db


def _target_db_name() -> str:
    url = os.environ.get("PSEUDOLIFE_TEST_DATABASE_URL")
    if url:
        return _with_worker_suffix(url.rsplit("/", 1)[1].split("?")[0])
    return _TEST_DB


def resolve_test_db_url() -> str:
    url = os.environ.get("PSEUDOLIFE_TEST_DATABASE_URL")
    if url:
        # Explicit override: returned verbatim (single-process) and
        # provisioning stays pg_url's job (CI relies on that) — no
        # connection attempts from a mere resolve. Under an xdist worker
        # the database name gets the worker id appended; see
        # _with_worker_suffix for why.
        base, _, db = url.rpartition("/")
        return f"{base}/{_with_worker_suffix(db)}"
    # Best-effort creation so direct consumers (daemon/shim fixtures,
    # single-file runs) get an existing per-run database without depending
    # on pg_url having run first; their own reachability probes handle the
    # no-server case.
    try:
        ensure_test_db()
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_ADMIN.rsplit("/", 1)[0] + f"/{_TEST_DB}"


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5  # ACCESS_DENIED: exists, not ours
    try:
        os.kill(pid, 0)  # signal 0 probes existence; POSIX only (kills on win32)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours
    return True


def _prune_dead_run_dbs(conn) -> None:
    """Drop private DBs leaked by hard-killed runs (atexit never fired).

    Only names carrying a pid suffix whose process is gone — a live
    concurrent run's database matches the pattern but its pid is alive,
    so it is never touched (its connection count may legitimately be
    zero between PG-backed tests, which is why liveness is checked on
    the pid, not on pg_stat_activity).
    """
    rows = conn.execute(
        "SELECT datname FROM pg_database "
        "WHERE datname LIKE 'pseudolife_memory_test_%' "
        "   OR datname LIKE 'pseudolife_memory_bench_%'"
    ).fetchall()
    for (name,) in rows:
        suffix = name.rsplit("_", 1)[1]
        if not suffix.isdigit() or int(suffix) == os.getpid():
            continue
        if _pid_alive(int(suffix)):
            continue
        try:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        except Exception:  # noqa: BLE001 — a race with another pruner is fine
            pass


def _drop_run_db() -> None:
    try:
        with psycopg.connect(_admin_url(), connect_timeout=3, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_TEST_DB}" WITH (FORCE)')
    except Exception:  # noqa: BLE001 — best-effort; pruning covers leftovers
        pass


# Memo per (admin url, db name): None = created OK, str = failure message.
# Keyed, not a plain flag, so a test that toggles the env override cannot
# poison provisioning of the other target for the rest of the process.
_ensure_state: dict[tuple[str, str], str | None] = {}


def ensure_test_db() -> None:
    """Create the run's test database if missing (memoized per target).

    Raises on an unreachable server — callers translate that into a
    skip. ``resolve_test_db_url()`` calls this too (default-URL path),
    so single-file runs work on a fresh server without depending on
    ``pg_url`` having run first.
    """
    overridden = bool(os.environ.get("PSEUDOLIFE_TEST_DATABASE_URL"))
    db_name = _target_db_name()
    key = (_admin_url(), db_name)
    if key in _ensure_state:
        if _ensure_state[key] is not None:
            raise RuntimeError(_ensure_state[key])
        return
    try:
        with psycopg.connect(_admin_url(), connect_timeout=3, autocommit=True) as conn:
            if not overridden:
                _prune_dead_run_dbs(conn)
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
            ).fetchone()
            if row is None:
                conn.execute(f'CREATE DATABASE "{db_name}"')
            if not overridden:
                atexit.register(_drop_run_db)
    except Exception as exc:  # noqa: BLE001
        _ensure_state[key] = f"no test Postgres reachable: {exc}"
        raise RuntimeError(_ensure_state[key]) from exc
    _ensure_state[key] = None


@pytest.fixture(scope="session")
def pg_url() -> str:
    """Session fixture: ensure the test database exists; skip if no server."""
    try:
        ensure_test_db()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(str(exc))
    return resolve_test_db_url()


@pytest.fixture()
def pg_service(pg_url, pg_conn, tmp_path, monkeypatch):
    """A ``MemoryService`` bound to the real bench Postgres (schema ensured
    and tables truncated by ``pg_conn``).

    Lives here, beside ``pg_conn``/``pg_url``, because three suites want it:
    it used to be defined in tests/test_outcome_inference.py and imported
    across into tests/test_session_identity.py, which made an unrelated
    parser suite a load-bearing dependency of the session-identity suite.
    Imported the same way its siblings are —
    ``from tests.pg_fixtures import pg_conn, pg_service, pg_url``.

    Function-scoped, and it must stay that way: ``pg_conn`` reaps every
    other backend on the test database at each test setup, so a
    module-scoped PG-backed service would have its connection terminated
    mid-module.
    """
    from pseudolife_memory.service import MemoryService

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()
    return svc


@pytest.fixture()
def pg_conn(pg_url):
    """Per-test connection with schema ensured and all tables truncated."""
    from pseudolife_memory.storage.schema import (
        SCHEMA_META_VERSION,
        ensure_schema,
    )

    with psycopg.connect(pg_url) as conn:
        # Pin to public BEFORE any schema/truncate work — mirrors
        # PostgresStorage.__init__. The DB role `pseudolife` can clash with
        # schema names, so the default ("$user", public) search_path could
        # shadow the real bank. Pinning to public before truncate ensures
        # we always clear the real tables.
        conn.execute("SET search_path TO public")
        conn.commit()
        # Reap leaked backends from tests that built a MemoryService /
        # PostgresStorage and never closed it. Such a connection holds locks on
        # the public tables, so the TRUNCATE below would block and hit
        # lock_timeout. Safe: this database is private to this pytest process
        # (see module docstring), so only this run's own leftovers die here.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        conn.commit()
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE " + ", ".join(_ALL_TABLES) + " RESTART IDENTITY CASCADE"
            )
        conn.commit()
        # Re-seed the one meta row (schema_version) that the truncate wiped.
        # This used to be a second full ensure_schema(conn) — the whole DDL
        # script re-run for a single INSERT, 320+ times per suite (measured
        # 5-16s, 2026-08-28). Mirrors the only INSERT INTO meta in
        # storage/schema.py; the first ensure_schema above still runs per
        # test, because tests deliberately break DDL it has to repair.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) "
                "VALUES ('schema_version', %s::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(SCHEMA_META_VERSION),),
            )
        conn.commit()
        yield conn
