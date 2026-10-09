"""Disposable banks on the bench PostgreSQL, through the test login only.

Every database this module creates, clones or drops must match
``DISPOSABLE_NAME``; anything else is refused before a connection opens, so
the harness cannot reach the live bank or a production-named database.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

HOST_PORT = os.environ.get("PSEUDOLIFE_TEST_PG_HOST_PORT", "127.0.0.1:5433")
LOGIN_FILE = Path(os.environ.get("PSEUDOLIFE_TEST_PG_LOGIN_FILE")
                  or Path.home() / ".pseudolife-mcp" / "test-pg.env")
DISPOSABLE_NAME = re.compile(r"pl_cf_w1a_[a-z0-9_]{1,40}")


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
    if not DISPOSABLE_NAME.fullmatch(name):
        raise ValueError(f"refusing non-disposable database name {name!r}")
    return name


def dsn(name: str) -> str:
    user, password = _login()
    host, port = HOST_PORT.rsplit(":", 1)
    return (f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{_check(name)}")


def _admin():
    user, password = _login()
    host, port = HOST_PORT.rsplit(":", 1)
    return psycopg.connect(host=host, port=int(port), user=user, password=password,
                           dbname="postgres", autocommit=True, connect_timeout=10)


def create(name: str, template: str | None = None) -> str:
    """Drop and recreate ``name`` (optionally as a copy of ``template``)."""
    drop(name)
    with _admin() as conn:
        if template is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_check(name))))
        else:
            conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(_check(name)), sql.Identifier(_check(template))))
    return dsn(name)


def drop(name: str) -> None:
    with _admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
            sql.Identifier(_check(name))))


def existing() -> list[str]:
    with _admin() as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'pl_cf_w1a_%'")
        return sorted(r[0] for r in rows if DISPOSABLE_NAME.fullmatch(r[0]))
