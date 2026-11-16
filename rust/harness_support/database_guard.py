"""Fail-closed production-bank guards for disposable test connections.

Keep the configured bank snapshot authoritative when a fixture temporarily
changes the daemon DSN. Ask the server before DDL or destructive statements;
a disposable-looking connection string alone is insufficient.
"""
from __future__ import annotations

import os
from typing import Iterable

DEFAULT_PRODUCTION_DATABASE = "pseudolife_memory"
PRODUCTION_DATABASES = frozenset({DEFAULT_PRODUCTION_DATABASE})
PRODUCTION_DATABASE_ENV = "_PSEUDOLIFE_PRODUCTION_DB"
UNRESOLVED_PRODUCTION_DATABASE = "<unresolved>"


class ProductionDatabaseError(RuntimeError):
    """A test or bench reset was pointed at a production bank."""


def dsn_database_name(dsn: str) -> str | None:
    """The database a libpq DSN connects to, when that is certain.

    Parsed by libpq's own rules: both spellings, percent-encoding and a
    ``?dbname=`` query parameter (which libpq lets override the URI path) —
    a hand-rolled split misses the last two. A DSN naming no database falls
    back to ``PGDATABASE`` only when no service is in play: libpq fills
    unset parameters from a service file (``service=`` or ``PGSERVICE``)
    before the environment. Otherwise libpq picks the service file's
    database or the one named after the user, and neither is knowable here.
    An explicit EMPTY dbname (``dbname=''``, ``?dbname=``) counts as set to
    libpq — it skips both the service file and ``PGDATABASE`` and uses the
    user name — so it is unknown too. None means exactly that — unknown,
    never "none" — and also covers a DSN that does not parse.
    """
    try:
        from psycopg.conninfo import conninfo_to_dict

        params = conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 — the message can carry credentials
        return None
    if "dbname" in params:
        return params["dbname"] or None
    if params.get("service") or os.environ.get("PGSERVICE"):
        return None
    return os.environ.get("PGDATABASE") or None


def _unresolved_error() -> ProductionDatabaseError:
    return ProductionDatabaseError(
        "refusing every test/bench reset: PSEUDOLIFE_MCP_DATABASE_URL is set "
        "but does not name its database (libpq would use the user name or a "
        "service file) or does not parse, so the production bank cannot be "
        "identified. Unset it — tests and benches never need it — or add an "
        "explicit dbname."
    )


def _fold(name: str) -> str:
    # A trailing slash is dropped: libpq reads `/pseudolife_memory/` as a
    # different, nonexistent database, but a guard should treat it as the
    # typo it is rather than let it launder the bank's name.
    return name.rstrip("/").casefold()


def _production_database_names() -> frozenset[str]:
    """Folded: the default bank, plus conftest's recorded bank inside the
    test suite, or else the bank PSEUDOLIFE_MCP_DATABASE_URL names.

    Raises ProductionDatabaseError when that DSN is set but leaves its
    database implicit — fail closed rather than guess."""
    names = set(PRODUCTION_DATABASES)
    recorded = os.environ.get(PRODUCTION_DATABASE_ENV)
    if recorded == UNRESOLVED_PRODUCTION_DATABASE:
        raise _unresolved_error()
    if recorded:
        names.add(recorded)
    else:
        live_dsn = os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
        if live_dsn:
            live_name = dsn_database_name(live_dsn)
            if live_name is None:
                raise _unresolved_error()
            names.add(live_name)
    return frozenset(_fold(name) for name in names if name)


def is_production_database(name: str, *, extra: Iterable[str] = ()) -> bool:
    """Whether ``name`` (case-folded) is a production bank, or one of the
    caller's own ``extra`` refusals. Raises ProductionDatabaseError instead
    when the production bank cannot be identified (see above)."""
    refused = _production_database_names() | {_fold(e) for e in extra}
    return _fold(name) in refused


def refuse_production_database(name: str) -> None:
    """Raise before a test or bench harness resolves ``name`` as its target."""
    if is_production_database(name):
        raise ProductionDatabaseError(
            f"refusing to reset database {name!r}: it is a production bank "
            "(pseudolife_memory, or the database PSEUDOLIFE_MCP_DATABASE_URL "
            "names). Test and bench resets reap its connections, migrate it "
            "and truncate every table; point them at a disposable database."
        )


def assert_disposable_database(conn) -> str:
    """Refuse to reset a production bank; returns the database name.

    Asks the SERVER which database ``conn`` reached, so no DSN spelling can
    walk around it. Call it as the first statement at every site that reaps
    backends on, runs DDL against, or truncates a database it connected to
    itself — before the reap, which would already have killed the daemon's
    connections (tests/test_disposable_database_guard.py).
    """
    name = conn.execute("SELECT current_database()").fetchone()[0]
    refuse_production_database(name)
    return name
