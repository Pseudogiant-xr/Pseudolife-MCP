"""RE Hub extension schema — independent from upstream's integer version."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401

from pseudolife_memory.storage.schema import (
    REHUB_SCHEMA_VERSION,
    SCHEMA_META_VERSION,
    ensure_schema,
)


def test_rehub_schema_has_an_independent_namespaced_version():
    assert REHUB_SCHEMA_VERSION == "v34-rehub"


def test_populated_v37_rehub_bank_upgrades_without_changing_proof(pg_conn):
    from psycopg import sql
    from pseudolife_memory.re_evidence import parse_evidence_bytes
    from pseudolife_memory.storage.re_evidence import ReEvidenceArchiveStorage

    # Frozen from the fork's pre-upgrade 1d1129d5 schema initializer. Use
    # actual historical DDL, rather than resetting a current bank's number.
    historical = {}
    fixture = Path(__file__).parent / "fixtures" / "schema_v37_rehub.py.txt"
    exec(compile(fixture.read_text(encoding="utf-8"), str(fixture), "exec"), historical)
    assert historical["SCHEMA_META_VERSION"] == 37
    tables = [row[0] for row in pg_conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
    pg_conn.execute(sql.SQL("DROP TABLE {} CASCADE").format(
        sql.SQL(", ").join(sql.Identifier(name) for name in tables)))
    pg_conn.commit()
    try:
        historical["ensure_schema"](pg_conn)
        raw = b'{ "address": "00B72870", "value": 1.00 }\r\n'
        artifact = parse_evidence_bytes(raw, source_path="evidence/original.json")
        artifact.update(project="upgrade-proof", binary_id="client:test", kind="function")
        store = ReEvidenceArchiveStorage(pg_conn)
        evidence_id = store.insert_re_evidence(artifact)
        claim_id = store.upsert_re_claim(
            project="upgrade-proof", binary_id="client:test", subject="00b72870",
            claim="calls the indexed function", status="verified", evidence_ids=[evidence_id])
        before = store.get_re_evidence_for_export(
            artifact_id=evidence_id, project="upgrade-proof", binary_id="client:test")
        assert pg_conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == 37

        ensure_schema(pg_conn)
        after = store.get_re_evidence_for_export(
            artifact_id=evidence_id, project="upgrade-proof", binary_id="client:test")
        assert after == before
        assert after["raw_bytes"] == raw
        assert after["content_hash"] == artifact["content_hash"]
        claims = store.query_re_claims(project="upgrade-proof", binary_id="client:test")
        assert [(row["id"], row["status"], row["evidence_ids"]) for row in claims] == [
            (claim_id, "verified", [evidence_id])]
        versions = dict(pg_conn.execute(
            "SELECT key, value FROM meta WHERE key IN "
            "('schema_version', 'rehub_schema_version')").fetchall())
        assert versions == {
            "schema_version": SCHEMA_META_VERSION,
            "rehub_schema_version": REHUB_SCHEMA_VERSION,
        }
    finally:
        pg_conn.rollback()
        ensure_schema(pg_conn)


def test_rehub_evidence_tables_and_address_index_exist(pg_conn):
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

    # pg_conn truncates and manually re-seeds only upstream's schema_version
    # after ensuring DDL. Re-run the idempotent startup path to prove the RE Hub
    # marker is restored independently on an already-created database.
    ensure_schema(pg_conn)
    base_meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    extension_meta = pg_conn.execute(
        "SELECT value FROM meta WHERE key = 'rehub_schema_version'").fetchone()
    assert base_meta is not None and int(base_meta[0]) == SCHEMA_META_VERSION
    assert extension_meta is not None and extension_meta[0] == REHUB_SCHEMA_VERSION


def test_rehub_schema_refuses_an_incompatible_pilot_shape(pg_conn):
    pg_conn.execute(
        "DROP TABLE re_claim_evidence, re_claims, "
        "re_evidence_artifacts CASCADE")
    pg_conn.execute(
        "CREATE TABLE re_evidence_artifacts ("
        "id BIGSERIAL PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL, "
        "locator TEXT NOT NULL, source_path TEXT NOT NULL, "
        "content_hash TEXT NOT NULL, raw_bytes TEXT NOT NULL, "
        "payload JSONB NOT NULL, payload_keys TEXT[] NOT NULL DEFAULT '{}', "
        "summary TEXT, binary_id TEXT NOT NULL, "
        "addresses TEXT[] NOT NULL DEFAULT '{}', "
        "ingested_at DOUBLE PRECISION NOT NULL)")
    pg_conn.commit()

    try:
        with pytest.raises(RuntimeError, match="incompatible.*raw_bytes"):
            ensure_schema(pg_conn)
    finally:
        # The PostgreSQL fixture reuses its database between tests. Restore the
        # valid extension DDL so this deliberate corruption cannot leak into
        # whichever test happens to run next.
        pg_conn.execute(
            "DROP TABLE IF EXISTS re_claim_evidence, re_claims, "
            "re_evidence_artifacts CASCADE")
        pg_conn.commit()
        ensure_schema(pg_conn)


@pytest.mark.parametrize("mutation", [
    "ALTER TABLE re_evidence_artifacts ALTER COLUMN id DROP DEFAULT",
    "ALTER TABLE re_claims DROP CONSTRAINT re_claims_status_check; "
    "ALTER TABLE re_claims ADD CHECK (status IS NOT NULL)",
    "ALTER TABLE re_claim_evidence DROP CONSTRAINT "
    "re_claim_evidence_claim_id_fkey; ALTER TABLE re_claim_evidence "
    "ADD FOREIGN KEY (claim_id) REFERENCES re_claims(id)",
    "ALTER TABLE re_claims ADD CHECK (false)",
    "ALTER TABLE re_evidence_artifacts ADD COLUMN pilot_required TEXT NOT NULL",
])
def test_rehub_schema_refuses_incompatible_defaults_and_constraints(
        pg_conn, mutation):
    try:
        for statement in mutation.split("; "):
            pg_conn.execute(statement)
        pg_conn.commit()
        with pytest.raises(RuntimeError, match="incompatible"):
            ensure_schema(pg_conn)
    finally:
        pg_conn.execute(
            "DROP TABLE IF EXISTS re_claim_evidence, re_claims, "
            "re_evidence_artifacts CASCADE")
        pg_conn.commit()
        ensure_schema(pg_conn)
