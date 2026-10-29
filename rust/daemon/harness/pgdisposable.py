"""Disposable banks on the bench PostgreSQL, through the test login only.

Every database this module creates, clones or drops must match its slice's
disposable name pattern; anything else is refused before a connection opens,
so the harness cannot reach the live bank or a production-named database.

A harness binds its own slice with ``pg = pgdisposable.Slice("w2f")`` (or
``pgdisposable.Slice.from_env("pgs")`` to follow the launcher's
``PL_HARNESS_SLICE``), so importing several harnesses into one process
cannot change which names another admits. The module-level functions follow
``PL_HARNESS_SLICE`` (default ``w1a``) at each call.
"""
from __future__ import annotations

import os
import re
import sys
import time
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
SLICE_ID = re.compile(r"w[0-9][a-z]|http|pgs|prn")
DEFAULT_SLICE = "w1a"
# One run owns every slice's names: name() adds a random run suffix so two
# invocations of the same slice cannot share a database.
NAMES = RunNames()


def _pattern(prefix: str) -> re.Pattern:
    return re.compile(re.escape(prefix) + r"[a-z0-9_]{1,40}")


class Slice:
    """One slice's disposable namespace: its prefix bounds admission.

    Helpers it does not define (``_admin``, ``_login``, ``NAMES``,
    ``HOST_PORT``...) are this module's, looked up at each use, so a patch
    on the module or on one handle both take effect."""

    def __init__(self, slice_id: str):
        if not isinstance(slice_id, str) or not SLICE_ID.fullmatch(slice_id):
            raise SystemExit(f"PL_HARNESS_SLICE={slice_id!r}: expected a slice id like w1a, w2g, http, pgs or prn")
        self.SLICE = slice_id
        self.PREFIX = f"pl_cf_{slice_id}_"
        self.DISPOSABLE_NAME = _pattern(self.PREFIX)

    @classmethod
    def from_env(cls, default: str = DEFAULT_SLICE) -> Slice:
        return cls(os.environ.get("PL_HARNESS_SLICE", default))

    def __getattr__(self, attr: str):
        try:
            return globals()[attr]
        except KeyError:
            raise AttributeError(attr) from None

    def __repr__(self) -> str:
        return f"pgdisposable.Slice({self.SLICE!r})"

    def _check(self, name: str) -> str:
        return _check(name, self)

    def name(self, label: str) -> str:
        return self.NAMES.name(self._check(label), max_length=len(self.PREFIX) + 40)

    def dsn(self, name: str) -> str:
        self.NAMES.require_process()
        user, password = self._login()
        host, port = self.HOST_PORT.rsplit(":", 1)
        return (f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
                f"@{host}:{port}/{self._check(name)}")

    def create(self, name: str, template: str | None = None) -> str:
        """Drop and recreate ``name`` (optionally as a copy of ``template``)."""
        self.drop(name)
        with self._admin() as conn:
            self.assert_disposable_database(conn)
            if template is None:
                self.create_from_default_template(conn, sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(self._check(name))))
            else:
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(self._check(name)), sql.Identifier(self._check(template))))
        return self.dsn(name)

    def drop(self, name: str, attempts: int = 10) -> None:
        """FORCE cannot end a backend this role does not own (an autovacuum
        worker on a just-used bank): retry until it has gone."""
        self._check(name)
        self.NAMES.require_owned(name)
        for i in range(attempts):
            try:
                with self._admin() as conn:
                    self.assert_disposable_database(conn)
                    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(self._check(name))))
                return
            except psycopg.errors.InsufficientPrivilege:
                if i == attempts - 1:
                    raise
                time.sleep(2)

    def existing(self) -> list[str]:
        with self._admin() as conn:
            rows = conn.execute("SELECT datname FROM pg_database WHERE starts_with(datname, %s)",
                                (self.PREFIX,))
            return sorted(r[0] for r in rows
                          if self.DISPOSABLE_NAME.fullmatch(r[0]) and self.NAMES.owns(r[0]))


def current() -> Slice:
    """The launcher's slice, read now."""
    return Slice.from_env()


def __getattr__(attr: str):
    # SLICE, PREFIX and DISPOSABLE_NAME follow PL_HARNESS_SLICE at each read.
    if attr in ("SLICE", "PREFIX", "DISPOSABLE_NAME"):
        return getattr(current(), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")


def name(label: str) -> str:
    return current().name(label)


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


def _check(name: str, space: Slice | None = None) -> str:
    PREFIX = (space or current()).PREFIX
    DISPOSABLE_NAME = _pattern(PREFIX)
    refuse_production_database(name)
    if not DISPOSABLE_NAME.fullmatch(name):
        raise ValueError(f"refusing non-disposable database name {name!r}")
    return name


def dsn(name: str) -> str:
    return current().dsn(name)


def _admin():
    NAMES.require_process()
    user, password = _login()
    host, port = HOST_PORT.rsplit(":", 1)
    return psycopg.connect(host=host, port=int(port), user=user, password=password,
                           dbname="postgres", autocommit=True, connect_timeout=10)


def create(name: str, template: str | None = None) -> str:
    """Drop and recreate ``name`` (optionally as a copy of ``template``)."""
    return current().create(name, template)


def drop(name: str, attempts: int = 10) -> None:
    current().drop(name, attempts)


def existing() -> list[str]:
    return current().existing()
