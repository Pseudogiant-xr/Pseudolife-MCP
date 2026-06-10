"""Schema v8 + PostgresStorage round-trips (skips without a PG server)."""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


def test_ensure_schema_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    flags1 = ensure_schema(pg_conn)
    flags2 = ensure_schema(pg_conn)
    assert flags1["age_available"] is True  # ops container ships AGE
    assert flags1 == flags2


def test_schema_version_recorded(pg_conn):
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    row = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row is not None and int(row[0]) == SCHEMA_META_VERSION


def test_vector_column_roundtrip(pg_conn):
    import numpy as np
    from pgvector.psycopg import register_vector

    register_vector(pg_conn)
    vec = np.arange(384, dtype=np.float32) / 384.0
    pg_conn.execute(
        "INSERT INTO entries (band, text, embedding, ts) VALUES (%s, %s, %s, %s)",
        ("working", "vector probe", vec, 0.0),
    )
    out = pg_conn.execute("SELECT embedding FROM entries").fetchone()[0]
    assert np.allclose(out, vec, atol=1e-6)
