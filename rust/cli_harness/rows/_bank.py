"""Disposable banks for CLI rows that read or write PostgreSQL.

Databases live on the bench server (127.0.0.1:5433 unless
``PSEUDOLIFE_TEST_PG_HOST_PORT`` says otherwise) and are created through the
test login (``~/.pseudolife-mcp/test-pg.env``), a role that creates its own
databases and cannot connect to the bank. Every name must start with
``pl_cf_``; the production guard is asked again on every connection.

The schema comes from the oracle's own ``ensure_schema``; rows come from the
oracle's writers or the oracle test suite's seeders, never from this module.
``dump`` reads back every public table, sequence and catalog shape so two
arms' post-states can be compared exactly.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import quote

from ..pg_create import create_from_default_template
from ..run_names import RunNames

_NAME = re.compile(r"^pl_cf_[a-z0-9_]{1,40}$")
NAMES = RunNames()


def name(label: str) -> str:
    if not _NAME.fullmatch(label):
        raise ValueError(f"disposable database names start with pl_cf_: {label!r}")
    return NAMES.name(label, max_length=46)


def _login() -> tuple[str, str]:
    from tests.pg_defaults import default_login  # noqa: PLC0415 (oracle checkout)
    user, password = default_login()[:2]
    if user != "pseudolife_test":
        raise RuntimeError(f"refusing login {user!r}: CLI rows use the test login only")
    return user, password


def _host_port() -> str:
    return os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5433")


def url(name: str) -> str:
    NAMES.require_process()
    if not _NAME.match(name):
        raise ValueError(f"disposable database names start with pl_cf_: {name!r}")
    user, password = _login()
    return f"postgresql://{quote(user)}:{quote(password, safe='')}@{_host_port()}/{name}"


def _connect(name: str, autocommit: bool = True):
    import psycopg  # noqa: PLC0415
    from pseudolife_memory.storage.schema import refuse_production_database  # noqa: PLC0415
    conn = psycopg.connect(url(name), connect_timeout=5, autocommit=autocommit)
    refuse_production_database(conn.execute("SELECT current_database()").fetchone()[0])
    return conn


def _admin():
    NAMES.require_process()
    import psycopg  # noqa: PLC0415
    user, password = _login()
    return psycopg.connect(
        f"postgresql://{quote(user)}:{quote(password, safe='')}@{_host_port()}/postgres",
        connect_timeout=5, autocommit=True)


# A connect that times out is retried, the only connect failure that is: a
# half-dropped bank refuses connections with a different error, which keeps
# its catalog check below. Measured 2026-11-07: 100 psycopg connects to the
# Windows host's bench PostgreSQL took 5.9-32.1 ms (median 24.9 ms), so the
# 5 s connect_timeout is about 150x the slowest. Hosted windows-latest still
# exceeded it once, in a lease row's teardown after every comparison passed
# (run 42348225338, job 128340727137), so a timeout is a transient stall.
# Three attempts, 1 s then 2 s apart: at most about 18 s per connect, so a
# drop() that meets it at both of its connects waits about 36 s.
CONNECT_ATTEMPTS = 3
CONNECT_BACKOFF_SECONDS = (1.0, 2.0)


def _retrying(connect):
    """``connect()``, retried on a connect timeout alone (see CONNECT_ATTEMPTS)."""
    import time  # noqa: PLC0415
    import psycopg  # noqa: PLC0415
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            return connect()
        except psycopg.errors.ConnectionTimeout:
            if attempt + 1 == CONNECT_ATTEMPTS:
                raise
            time.sleep(CONNECT_BACKOFF_SECONDS[attempt])


def teardown(name: str) -> None:
    """Drop a case's bank after its arm ran, from a fixture's close().

    A failed drop does not fail the case by itself: if the bank is gone, the
    failure is printed and the case keeps its result. If the bank is still
    there, or its absence cannot be confirmed, the bank leaked and the
    failure propagates. A guard refusal (a name this run does not own, or a
    production database) always propagates."""
    from pseudolife_memory.storage.schema import ProductionDatabaseError  # noqa: PLC0415
    try:
        drop(name)
    except (ValueError, ProductionDatabaseError):
        raise
    except Exception as failure:
        check = left = None
        try:
            with _retrying(_admin) as conn:
                left = conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        except Exception as error:  # noqa: BLE001 — reported on the original failure
            check = error
        if check is not None:
            # Raised here, outside the check's handler, so the original
            # failure keeps its own context.
            failure.add_note(f"teardown could not confirm {name} is gone: "
                             f"{type(check).__name__}: {check}")
            raise
        if left is not None:
            raise
        print(f"cli_harness: teardown of {name} failed ({type(failure).__name__}: {failure}), "
              "but the bank is gone; the case keeps its result", file=sys.stderr)


def drop(name: str) -> None:
    import psycopg  # noqa: PLC0415
    from pseudolife_memory.storage.schema import (  # noqa: PLC0415
        assert_disposable_database, refuse_production_database)
    NAMES.require_owned(name)
    url(name)  # validates the name
    with _retrying(_admin) as conn:
        if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return
        # Ask the server which database the name reaches before reaping
        # anything on it (tests/test_disposable_database_guard.py). A database
        # dropped in between is already gone; any other failure propagates.
        # psycopg leaves sqlstate unset on connect-time errors, so recheck the
        # catalog rather than parse the message. A database half-dropped by a
        # killed run (PostgreSQL 15+ marks it datconnlimit = -2) refuses
        # connections but must still be droppable: refuse a production name
        # by itself, then carry on to the DROP.
        try:
            with _retrying(lambda: _connect(name)) as target:
                assert_disposable_database(target)
        except psycopg.OperationalError:
            row = conn.execute(
                "SELECT datconnlimit, datallowconn FROM pg_database "
                "WHERE datname = %s", (name,)).fetchone()
            if row is None:
                return
            if row[0] != -2 and row[1]:
                raise
            refuse_production_database(name)
        # Only this login's sessions: an autovacuum worker is not ours to
        # signal. Plain DROP handles it without FORCE's signal privilege.
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid() "
            "AND usename = current_user", (name,))
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def create(name: str, schema: bool = True) -> str:
    """A fresh empty database; with ``schema`` the oracle's ensure_schema ran."""
    drop(name)
    with _admin() as conn:
        create_from_default_template(conn, f'CREATE DATABASE "{name}"')
    if schema:
        from pseudolife_memory.storage.schema import ensure_schema  # noqa: PLC0415
        with _connect(name, autocommit=False) as conn:
            conn.execute("SET search_path TO public")
            ensure_schema(conn)
            conn.commit()
    return url(name)


def connect(name: str, autocommit: bool = False):
    conn = _connect(name, autocommit=autocommit)
    conn.execute("SET search_path TO public")
    return conn


def dump(name: str) -> dict:
    """Every public table's rows (as jsonb text, sorted), sequence positions
    and the catalog shape (columns, indexes, constraints)."""
    out: dict = {"tables": {}, "sequences": {}, "columns": [], "indexes": [], "constraints": []}
    with _connect(name) as conn:
        tables = sorted(r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        for table in tables:
            out["tables"][table] = sorted(r[0] for r in conn.execute(
                f'SELECT to_jsonb(t)::text AS j FROM public."{table}" t')
            )
        out["sequences"] = {r[0]: r[1] for r in sorted(conn.execute(
            "SELECT sequencename, last_value FROM pg_sequences "
            "WHERE schemaname = 'public'"))}
        out["columns"] = sorted(list(r) for r in conn.execute(
            "SELECT table_name, column_name, data_type, udt_name, is_nullable, "
            "column_default FROM information_schema.columns "
            "WHERE table_schema = 'public'"))
        out["indexes"] = sorted(list(r) for r in conn.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public'"))
        out["constraints"] = sorted(list(r) for r in conn.execute(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"))
    return out
