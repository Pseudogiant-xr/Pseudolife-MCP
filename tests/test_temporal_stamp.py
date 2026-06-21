"""Schema v11 stamp columns + meta version upsert + service round-trip.

Skips without a PG server (mirrors test_pg_storage / test_lessons_storage).
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

_STAMPED = ("facts", "world_facts", "lessons", "edges")
_COLS = ("tx_time", "valid_time", "hlc_phys", "hlc_logical",
         "writer_id", "session_id", "version")


def test_stamp_columns_present(pg_conn):
    for tbl in _STAMPED:
        cols = {r[0] for r in pg_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (tbl,)).fetchall()}
        assert set(_COLS) <= cols, f"{tbl} missing {set(_COLS) - cols}"


def test_meta_version_is_current(pg_conn):
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION, ensure_schema

    assert SCHEMA_META_VERSION == 11
    # Simulate a stale recorded version, then re-ensure: it must update to current.
    pg_conn.execute("UPDATE meta SET value = '8'::jsonb WHERE key = 'schema_version'")
    pg_conn.commit()
    ensure_schema(pg_conn)
    row = pg_conn.execute(
        "SELECT value::text FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert int(row[0].strip('"')) == SCHEMA_META_VERSION

# (service round-trip of the stamp is added in Task 3, once records + sync carry it)
