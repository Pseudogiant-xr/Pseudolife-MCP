"""Schema v22: edges(dst_id) index (2026-07-12 perf review).

The UNIQUE(src_id, relation, dst_id) constraint on ``edges`` supports
src_id-leading lookups, but dst_id-only lookups (``merge_entity``'s
dst-side dedup/repoint, any "what points to X" traversal) had no
supporting index and fell back to a sequential scan. Skips without a PG
server (mirrors test_pg_storage / test_schema_v16).
"""
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)
from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_schema_version_is_current():
    assert SCHEMA_META_VERSION == 22


def test_edges_dst_id_index_present(pg_conn):
    row = pg_conn.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'edges' AND indexname = 'edges_dst_idx'"
    ).fetchone()
    assert row is not None, "edges_dst_idx is missing"
    assert "dst_id" in row[0]
