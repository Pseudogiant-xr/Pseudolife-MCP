"""Schema v35 — isolated reverse-engineering evidence and claim tables."""

from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_35():
    assert SCHEMA_META_VERSION == 35


def test_re_evidence_tables_and_address_index_exist(pg_conn):
    tables = {
        row[0] for row in pg_conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 're_%'"
        ).fetchall()
    }
    assert {"re_evidence_artifacts", "re_claims", "re_claim_evidence"} <= tables
    index = pg_conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = "
        "'re_evidence_addresses_idx'").fetchone()
    assert index is not None and "USING gin" in index[0]
    columns = {
        row[0]: row[1] for row in pg_conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 're_evidence_artifacts' AND column_name IN "
            "('binary_id', 'raw_bytes', 'payload_keys')").fetchall()
    }
    assert columns == {
        "binary_id": "NO", "raw_bytes": "NO", "payload_keys": "NO"}
    triggers = {
        row[0] for row in pg_conn.execute(
            "SELECT tgname FROM pg_trigger WHERE tgname LIKE 're_claim_gate_%'"
        ).fetchall()
    }
    assert triggers == {"re_claim_gate_on_claim", "re_claim_gate_on_link"}
    meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert meta is not None and int(meta[0]) == SCHEMA_META_VERSION
