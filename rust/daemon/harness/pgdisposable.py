"""Disposable banks on the bench PostgreSQL, through the test login only.

Every database this module creates, clones or drops must match
``DISPOSABLE_NAME``; anything else is refused before a connection opens, so
the harness cannot reach the live bank or a production-named database.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import psycopg
from psycopg import sql
from pseudolife_memory.storage.schema import assert_disposable_database, refuse_production_database
from rust.cli_harness.pg_create import create_from_default_template
from rust.cli_harness.run_names import RunNames

HOST_PORT = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5433")
LOGIN_FILE = Path(os.environ.get("PSEUDOLIFE_TEST_PG_LOGIN_FILE")
                  or Path.home() / ".pseudolife-mcp" / "test-pg.env")
# A slice prefix bounds admission; name() adds a random run suffix so two
# invocations of the same slice cannot share a database.
SLICE = os.environ.get("PL_HARNESS_SLICE", "w1a")
if not re.fullmatch(r"w[0-9][a-z]|http|pgs|prn", SLICE):
    raise SystemExit(f"PL_HARNESS_SLICE={SLICE!r}: expected a slice id like w1a, w2g, http, pgs or prn")
PREFIX = f"pl_cf_{SLICE}_"
DISPOSABLE_NAME = re.compile(re.escape(PREFIX) + r"[a-z0-9_]{1,40}")
NAMES = RunNames()


def name(label: str) -> str:
    return NAMES.name(_check(label), max_length=len(PREFIX) + 40)


def _login() -> tuple[str, str]:
    values = {}
    for line in LOGIN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    try:
        return values["PSEUDOLIFE_TEST_PG_USER"], values["PSEUDOLIFE_TEST_PG_PASSWORD"]
    except KeyError:
        raise SystemExit(f"{LOGIN_FILE}: no test login; run `pseudolife-mcp test-login create`")


def _check(name: str) -> str:
    refuse_production_database(name)
    if not DISPOSABLE_NAME.fullmatch(name):
        raise ValueError(f"refusing non-disposable database name {name!r}")
    return name


def dsn(name: str) -> str:
    NAMES.require_process()
    user, password = _login()
    host, port = HOST_PORT.rsplit(":", 1)
    return (f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{_check(name)}")


def _admin():
    NAMES.require_process()
    user, password = _login()
    host, port = HOST_PORT.rsplit(":", 1)
    return psycopg.connect(host=host, port=int(port), user=user, password=password,
                           dbname="postgres", autocommit=True, connect_timeout=10)


def create(name: str, template: str | None = None) -> str:
    """Drop and recreate ``name`` (optionally as a copy of ``template``)."""
    drop(name)
    with _admin() as conn:
        assert_disposable_database(conn)
        if template is None:
            create_from_default_template(conn, sql.SQL("CREATE DATABASE {}").format(
                sql.Identifier(_check(name))))
        else:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(_check(name)), sql.Identifier(_check(template))))
    return dsn(name)


def drop(name: str, attempts: int = 10) -> None:
    """FORCE cannot end a backend this role does not own (an autovacuum
    worker on a just-used bank): retry until it has gone."""
    import time
    _check(name)
    NAMES.require_owned(name)
    for i in range(attempts):
        try:
            with _admin() as conn:
                assert_disposable_database(conn)
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(_check(name))))
            return
        except psycopg.errors.InsufficientPrivilege:
            if i == attempts - 1:
                raise
            time.sleep(2)


def existing() -> list[str]:
    with _admin() as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE starts_with(datname, %s)", (PREFIX,))
        return sorted(r[0] for r in rows if DISPOSABLE_NAME.fullmatch(r[0]) and NAMES.owns(r[0]))
