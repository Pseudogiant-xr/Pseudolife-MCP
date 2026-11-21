"""Load pinned captures into fresh disposable banks through the Rust schema."""
import gzip
import hashlib
import json
from pathlib import Path

import psycopg
from psycopg import sql

from principal_fixtures import check_target
from rust.harness_support.bank_fixture import assert_fixture_database
from rust.harness_support.fixture_rows import checked_fixture_insert
from rust.harness_support.schema import ensure_schema


def load_capture(name, digest):
    path = Path(__file__).with_name("fixtures") / f"{name}.json.gz"
    with gzip.open(path, "rb") as source:
        raw = source.read()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"{name}: fixture differs from its pinned capture")
    return json.loads(raw)


def replay(dsn, captured):
    check_target(dsn)
    with psycopg.connect(dsn) as conn:
        assert_fixture_database(conn)
        if conn.execute("SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S'))").fetchone()[0]:
            raise ValueError("fixed seed requires a fresh disposable bank; create it without a base template")
        ensure_schema(conn)
        for table, rows in captured["tables"].items():
            statement, params = checked_fixture_insert(conn, table, rows)
            if table == "meta":
                statement += sql.SQL(" WHERE true ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
            conn.execute(statement, params)
        for name, (last, called) in captured["sequences"].items():
            conn.execute("SELECT setval(%s::regclass, %s, %s)", (f"public.{name}", last, called))
