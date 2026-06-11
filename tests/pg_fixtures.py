"""Shared Postgres test fixtures (Phase 1).

Resolution order for the test server:

1. ``PSEUDOLIFE_TEST_DATABASE_URL`` env var (any reachable PG 16+vector).
2. The repo's dev container at ``127.0.0.1:5433`` (ops/docker-compose.yml).

If neither is reachable, PG-backed tests skip cleanly so the pure-logic
suites stay runnable anywhere.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

_DEFAULT_ADMIN = "postgresql://pseudolife:pseudolife@127.0.0.1:5433/postgres"
_TEST_DB = "pseudolife_memory_test"

_ALL_TABLES = (
    "edges", "entity_aliases", "relations", "facts", "entries",
    "episodes", "entities", "meta",
)


def _admin_url() -> str:
    url = os.environ.get("PSEUDOLIFE_TEST_DATABASE_URL")
    if url:
        # Point at the server's postgres db for admin ops.
        base, _, _db = url.rpartition("/")
        return base + "/postgres"
    return _DEFAULT_ADMIN


def resolve_test_db_url() -> str:
    url = os.environ.get("PSEUDOLIFE_TEST_DATABASE_URL")
    if url:
        return url
    return _DEFAULT_ADMIN.rsplit("/", 1)[0] + f"/{_TEST_DB}"


@pytest.fixture(scope="session")
def pg_url() -> str:
    """Session fixture: ensure the test database exists; skip if no server."""
    try:
        with psycopg.connect(_admin_url(), connect_timeout=3, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (_TEST_DB,)
            ).fetchone()
            if row is None:
                conn.execute(f'CREATE DATABASE "{_TEST_DB}"')
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no test Postgres reachable: {exc}")
    return resolve_test_db_url()


@pytest.fixture()
def pg_conn(pg_url):
    """Per-test connection with schema ensured and all tables truncated."""
    from pseudolife_memory.storage.schema import ensure_schema

    with psycopg.connect(pg_url) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE " + ", ".join(_ALL_TABLES) + " RESTART IDENTITY CASCADE"
            )
        conn.commit()
        # Re-seed meta (schema_version) that the truncate just wiped.
        ensure_schema(conn)
        yield conn
