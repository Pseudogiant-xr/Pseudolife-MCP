"""Schema v11 stamp columns + meta version upsert + service round-trip.

Skips without a PG server (mirrors test_pg_storage / test_lessons_storage).
"""
from __future__ import annotations

import tempfile

import pytest

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

    assert SCHEMA_META_VERSION == 22
    # Simulate a stale recorded version, then re-ensure: it must update to current.
    pg_conn.execute("UPDATE meta SET value = '8'::jsonb WHERE key = 'schema_version'")
    pg_conn.commit()
    ensure_schema(pg_conn)
    row = pg_conn.execute(
        "SELECT value::text FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert int(row[0].strip('"')) == SCHEMA_META_VERSION


@pytest.fixture()
def svc(pg_conn, pg_url):
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def test_fact_write_persists_and_hydrates_stamp(svc):
    svc.cortex_write("server", "port", "8080", support="user")
    rows = svc._storage.load_facts()
    r = next(x for x in rows if x["entity"] == "server")
    assert r["hlc_phys"] and r["hlc_phys"] > 0          # HLC physical stamped
    assert r["tx_time"] and r["valid_time"]             # both temporal anchors
    assert r["writer_id"]                               # stamped (default until T4)
    assert r["version"] == 1

    # Survives a fresh hydrate into a new store.
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.storage import sync
    c = CortexStore()
    sync.hydrate_cortex(c, svc._storage)
    rec = c.lookup("server", "port")
    assert rec is not None and rec.value == "8080"
    assert rec.hlc_phys == r["hlc_phys"] and rec.writer_id == r["writer_id"]
