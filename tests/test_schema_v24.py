"""Schema v24 -- per-entity kind, the input to the freshness policy.

Keyed on entity_norm, NOT entity_id: that is what cortex slots key on, a third
of cortex entities have no graph node at all, and a graph merge would
otherwise silently retarget the kind.
"""
from __future__ import annotations

import time

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage import schema


def test_schema_version_is_24():
    assert schema.SCHEMA_META_VERSION == 27


def test_entity_kinds_table_present(pg_conn):
    assert pg_conn.execute(
        "SELECT to_regclass('public.entity_kinds')").fetchone()[0]
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='entity_kinds'").fetchall()}
    assert {"entity_norm", "kind", "origin", "confidence", "decided_at"} <= cols


def test_entity_norm_is_the_primary_key(pg_conn):
    """One kind per entity -- a second write to the same entity updates it."""
    rows = pg_conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid='entity_kinds'::regclass AND i.indisprimary").fetchall()
    assert [r[0] for r in rows] == ["entity_norm"]


def _storage(pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage
    return PostgresStorage(pg_url)


def test_upsert_then_load_round_trip(pg_conn, pg_url):
    st = _storage(pg_url)
    try:
        n = st.upsert_entity_kinds([
            {"entity_norm": "daemon", "kind": "system",
             "origin": "model", "confidence": 0.9, "decided_at": time.time()},
            {"entity_norm": "0-9-0-release", "kind": "artifact",
             "origin": "rule", "confidence": 1.0, "decided_at": time.time()},
        ])
        assert n == 2
        assert st.load_entity_kinds() == {
            "daemon": "system", "0-9-0-release": "artifact"}
    finally:
        st.close()


def test_upsert_is_idempotent_and_updates_in_place(pg_conn, pg_url):
    st = _storage(pg_url)
    try:
        row = {"entity_norm": "daemon", "kind": "system",
               "origin": "model", "confidence": 0.9, "decided_at": time.time()}
        st.upsert_entity_kinds([row])
        st.upsert_entity_kinds([{**row, "kind": "concept", "origin": "user"}])
        assert st.load_entity_kinds() == {"daemon": "concept"}
        assert pg_conn.execute(
            "SELECT count(*) FROM entity_kinds").fetchone()[0] == 1
    finally:
        st.close()


def test_load_on_empty_table_returns_empty_dict(pg_conn, pg_url):
    st = _storage(pg_url)
    try:
        assert st.load_entity_kinds() == {}
    finally:
        st.close()
