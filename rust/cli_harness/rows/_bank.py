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
from urllib.parse import quote

_NAME = re.compile(r"^pl_cf_[a-z0-9_]{1,40}$")


def _login() -> tuple[str, str]:
    from tests.pg_defaults import default_login  # noqa: PLC0415 (oracle checkout)
    user, password = default_login()[:2]
    if user != "pseudolife_test":
        raise RuntimeError(f"refusing login {user!r}: CLI rows use the test login only")
    return user, password


def _host_port() -> str:
    return os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5433")


def url(name: str) -> str:
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
    import psycopg  # noqa: PLC0415
    user, password = _login()
    return psycopg.connect(
        f"postgresql://{quote(user)}:{quote(password, safe='')}@{_host_port()}/postgres",
        connect_timeout=5, autocommit=True)


def drop(name: str) -> None:
    url(name)  # validates the name
    with _admin() as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def create(name: str, schema: bool = True) -> str:
    """A fresh empty database; with ``schema`` the oracle's ensure_schema ran."""
    drop(name)
    with _admin() as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
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
        tables = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]
        for table in tables:
            out["tables"][table] = [r[0] for r in conn.execute(
                f'SELECT to_jsonb(t)::text AS j FROM public."{table}" t ORDER BY j')]
        out["sequences"] = {r[0]: r[1] for r in conn.execute(
            "SELECT sequencename, last_value FROM pg_sequences "
            "WHERE schemaname = 'public' ORDER BY 1")}
        out["columns"] = [list(r) for r in conn.execute(
            "SELECT table_name, column_name, data_type, udt_name, is_nullable, "
            "column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' ORDER BY 1, 2")]
        out["indexes"] = [list(r) for r in conn.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' ORDER BY 1, 2")]
        out["constraints"] = [list(r) for r in conn.execute(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
            "ORDER BY 1, 2")]
    return out
