"""RE evidence pilot: immutable artifacts and evidence-gated claims."""

from __future__ import annotations

import json

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


def _sample_payload() -> dict:
    return {
        "function": {
            "address": "0x00B72870",
            "range": {"start": "00b72870", "end": "00b729ab"},
            "raw_name": "FUN_00b72870",
        },
        "relationships": {
            "callees": [{"address": "00B72510"}],
            "callers": [{"address": "00934900"}],
        },
    }


def test_parse_artifact_normalizes_addresses_and_hashes_raw_bytes(tmp_path):
    from pseudolife_memory.re_evidence import parse_evidence_file

    path = tmp_path / "lookup.json"
    path.write_text(json.dumps(_sample_payload()), encoding="utf-8")

    artifact = parse_evidence_file(path)

    assert artifact["locator"] == "00b72870"
    assert artifact["addresses"] == [
        "00934900", "00b72510", "00b72870", "00b729ab"]
    assert len(artifact["content_hash"]) == 64
    assert artifact["payload"]["function"]["raw_name"] == "FUN_00b72870"


def test_parse_artifact_rejects_non_json_and_oversized_input(tmp_path):
    from pseudolife_memory.re_evidence import EvidenceInputError, parse_evidence_file

    text = tmp_path / "evidence.txt"
    text.write_text("not json", encoding="utf-8")
    with pytest.raises(EvidenceInputError, match="JSON"):
        parse_evidence_file(text)

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"{}" + b" " * 32)
    with pytest.raises(EvidenceInputError, match="maximum"):
        parse_evidence_file(huge, max_bytes=16)


def test_storage_deduplicates_artifacts_and_queries_exact_address(pg_url, pg_conn):
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        artifact = {
            "project": "srfn-client",
            "kind": "ghidra-function",
            "locator": "00b72870",
            "source_path": "evidence/world-step.json",
            "content_hash": "a" * 64,
            "payload": _sample_payload(),
            "summary": "World movement step candidate.",
            "binary_id": "decomp_sro_client.exe:sha256:test",
            "addresses": ["00b72510", "00b72870"],
            "raw_bytes": b"{}", "payload_keys": ["function", "relationships"],
        }
        first = storage.insert_re_evidence(artifact)
        second = storage.insert_re_evidence(artifact)
        assert second == first

        rows = storage.query_re_evidence(
            project="srfn-client", binary_id="decomp_sro_client.exe:sha256:test",
            address="0x00B72870")
        assert [row["id"] for row in rows] == [first]
        assert rows[0]["addresses"] == ["00b72510", "00b72870"]
    finally:
        storage.close()


def test_verified_claim_requires_existing_linked_evidence(pg_url, pg_conn):
    from pseudolife_memory.re_evidence import EvidenceInputError
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        with pytest.raises(EvidenceInputError, match="requires linked evidence"):
            storage.upsert_re_claim(
                project="srfn-client", binary_id="client:test", subject="00b72870",
                claim="steps movement in param_3 increments", status="verified",
                evidence_ids=[])

        with pytest.raises(EvidenceInputError, match="not found"):
            storage.upsert_re_claim(
                project="srfn-client", binary_id="client:test", subject="00b72870",
                claim="steps movement in param_3 increments", status="verified",
                evidence_ids=[999999])
    finally:
        storage.close()


def test_claim_query_returns_linked_evidence_ids(pg_url, pg_conn):
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        evidence_id = storage.insert_re_evidence({
            "project": "srfn-client", "kind": "ghidra-function",
            "locator": "00b72870", "source_path": "evidence/world-step.json",
            "content_hash": "b" * 64, "payload": _sample_payload(),
            "summary": None, "binary_id": "client:test", "addresses": ["00b72870"],
            "raw_bytes": b"{}", "payload_keys": ["function", "relationships"],
        })
        claim_id = storage.upsert_re_claim(
            project="srfn-client", binary_id="client:test", subject="00b72870",
            claim="calls 00b72510 before collision dispatch", status="observed",
            evidence_ids=[evidence_id], confidence=1.0)

        result = storage.query_re_claims(
            project="srfn-client", binary_id="client:test", subject="0x00B72870")
        assert result == [{
            "id": claim_id,
            "project": "srfn-client",
            "binary_id": "client:test",
            "subject": "00b72870",
            "claim": "calls 00b72510 before collision dispatch",
            "status": "observed",
            "confidence": 1.0,
            "evidence_ids": [evidence_id],
            "created_at": result[0]["created_at"],
            "updated_at": result[0]["updated_at"],
        }]
    finally:
        storage.close()


def test_claim_update_replaces_its_evidence_set(pg_url, pg_conn):
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        def add(locator: str, digest: str) -> int:
            return storage.insert_re_evidence({
                "project": "srfn-client", "kind": "ghidra-function",
                "locator": locator, "source_path": f"evidence/{locator}.json",
                "content_hash": digest * 64, "payload": {"address": locator},
                "summary": None, "binary_id": "client:test",
                "addresses": [locator], "raw_bytes": b"{}", "payload_keys": ["address"],
            })

        first, second = add("00b72870", "c"), add("00b72510", "d")
        storage.upsert_re_claim(
            project="srfn-client", binary_id="client:test", subject="00b72870",
            claim="collision dispatch succeeds", status="observed",
            evidence_ids=[first])
        storage.upsert_re_claim(
            project="srfn-client", binary_id="client:test", subject="00b72870",
            claim="collision dispatch succeeds", status="rejected",
            evidence_ids=[second])

        rows = storage.query_re_claims(
            project="srfn-client", binary_id="client:test", subject="00b72870")
        assert rows[0]["status"] == "rejected"
        assert rows[0]["evidence_ids"] == [second]
    finally:
        storage.close()


def test_replay_with_conflicting_metadata_is_rejected(pg_url, pg_conn):
    from pseudolife_memory.re_evidence import EvidenceInputError
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        artifact = {
            "project": "srfn-client", "kind": "ghidra-function",
            "locator": "00b72870", "source_path": "evidence/a.json",
            "content_hash": "e" * 64, "payload": {"address": "00b72870"},
            "summary": "first", "binary_id": "client:test",
            "addresses": ["00b72870"], "raw_bytes": b"{}",
            "payload_keys": ["address"],
        }
        storage.insert_re_evidence(artifact)
        artifact["kind"] = "packet-capture"
        with pytest.raises(EvidenceInputError, match="metadata conflicts"):
            storage.insert_re_evidence(artifact)
    finally:
        storage.close()


def test_address_query_plan_can_use_gin_index(pg_url, pg_conn):
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        storage.conn.execute("SET enable_seqscan = off")
        storage.conn.execute("SET enable_indexscan = off")
        plan = "\n".join(row[0] for row in storage.conn.execute(
            "EXPLAIN SELECT id FROM re_evidence_artifacts "
            "WHERE addresses @> ARRAY[%s]::text[]",
            ("00b72870",)).fetchall())
        assert "re_evidence_addresses_idx" in plan
    finally:
        storage.close()


def test_database_rejects_verified_claim_without_link(pg_conn):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation, match="requires linked evidence"):
        with pg_conn.transaction():
            pg_conn.execute(
                "INSERT INTO re_claims "
                "(project, binary_id, subject, claim, status, created_at, updated_at) "
                "VALUES ('srfn-client', 'client:test', '00b72870', "
                "'unlinked claim', 'verified', 1, 1)")


def test_database_rejects_cross_build_link_and_link_reassignment(pg_conn):
    import psycopg

    def artifact(binary_id: str, digest: str) -> int:
        return int(pg_conn.execute(
            "INSERT INTO re_evidence_artifacts "
            "(project, binary_id, kind, locator, source_path, content_hash, "
            "raw_bytes, payload, payload_keys, addresses, ingested_at) "
            "VALUES ('srfn-client', %s, 'ghidra-function', '00b72870', "
            "'evidence.json', %s, '{}'::bytea, '{}'::jsonb, '{}', '{}', 1) "
            "RETURNING id", (binary_id, digest)).fetchone()[0])

    evidence_a = artifact("build:a", "a" * 64)
    evidence_b = artifact("build:b", "b" * 64)
    claim_a = int(pg_conn.execute(
        "INSERT INTO re_claims "
        "(project, binary_id, subject, claim, status, created_at, updated_at) "
        "VALUES ('srfn-client', 'build:a', '00b72870', 'claim a', "
        "'verified', 1, 1) RETURNING id").fetchone()[0])
    claim_b = int(pg_conn.execute(
        "INSERT INTO re_claims "
        "(project, binary_id, subject, claim, status, created_at, updated_at) "
        "VALUES ('srfn-client', 'build:b', '00b72870', 'claim b', "
        "'verified', 1, 1) RETURNING id").fetchone()[0])
    pg_conn.execute(
        "INSERT INTO re_claim_evidence VALUES (%s, %s, 1)",
        (claim_a, evidence_a))
    pg_conn.execute(
        "INSERT INTO re_claim_evidence VALUES (%s, %s, 1)",
        (claim_b, evidence_b))
    pg_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation, match="project/binary"):
        with pg_conn.transaction():
            pg_conn.execute(
                "UPDATE re_claim_evidence SET evidence_id = %s "
                "WHERE claim_id = %s", (evidence_b, claim_a))

    with pytest.raises(psycopg.errors.CheckViolation, match="requires linked evidence"):
        with pg_conn.transaction():
            pg_conn.execute(
                "UPDATE re_claim_evidence SET claim_id = %s WHERE claim_id = %s",
                (claim_b, claim_a))


def test_portable_archive_round_trip_preserves_original_bytes(pg_url, pg_conn, tmp_path):
    import hashlib

    from pseudolife_memory.re_evidence import (
        export_evidence_archive, import_evidence_archive, parse_evidence_bytes)
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        raw = b'\xef\xbb\xbf{ "address": "00B72870", "value": 1.00 }\r\n'
        artifact = parse_evidence_bytes(raw, source_path="evidence/original.json")
        artifact.update({
            "project": "srfn-client", "binary_id": "client:test",
            "kind": "ghidra-function", "summary": "round trip",
        })
        old_id = storage.insert_re_evidence(artifact)
        storage.upsert_re_claim(
            project="srfn-client", binary_id="client:test", subject="00b72870",
            claim="round-trip claim", status="verified", evidence_ids=[old_id])

        archive_path = tmp_path / "proof.zip"
        exported = export_evidence_archive(
            storage, path=archive_path, project="srfn-client",
            binary_id="client:test")
        assert exported["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
        from pseudolife_memory.re_evidence import EvidenceInputError
        with pytest.raises(EvidenceInputError, match="empty project/build"):
            import_evidence_archive(
                storage, path=archive_path, project="srfn-client",
                binary_id="client:test")

        with storage._txn():
            storage.conn.execute("DELETE FROM re_claims WHERE project = 'srfn-client'")
            storage.conn.execute(
                "DELETE FROM re_evidence_artifacts WHERE project = 'srfn-client'")
        imported = import_evidence_archive(
            storage, path=archive_path, project="srfn-client",
            binary_id="client:test")
        assert imported == {
            "format": "pseudolife-re-evidence-v1", "project": "srfn-client",
            "binary_id": "client:test", "artifacts": 1, "claims": 1}

        ids = storage.re_evidence_export_ids(
            project="srfn-client", binary_id="client:test")
        restored = storage.get_re_evidence_for_export(
            artifact_id=ids[0], project="srfn-client", binary_id="client:test")
        assert restored["raw_bytes"] == raw
        claims = storage.query_re_claims(
            project="srfn-client", binary_id="client:test")
        assert claims[0]["evidence_ids"] == ids
    finally:
        storage.close()


def test_archive_prevalidation_rejects_duplicate_member_reference(tmp_path):
    import hashlib
    import zipfile

    from pseudolife_memory.re_evidence import (
        EvidenceInputError, import_evidence_archive)

    raw = b'{"address":"00b72870"}'
    digest = hashlib.sha256(raw).hexdigest()
    member = f"artifacts/1-{digest}.json"
    artifact = {
        "id": 1, "project": "srfn-client", "binary_id": "client:test",
        "kind": "ghidra-function", "locator": "00b72870",
        "content_hash": digest, "summary": None, "addresses": ["00b72870"],
        "payload_keys": ["address"], "member": member,
    }
    manifest = {
        "format": "pseudolife-re-evidence-v1", "project": "srfn-client",
        "binary_id": "client:test",
        "artifacts": [artifact, {**artifact, "id": 2}], "claims": [],
    }
    path = tmp_path / "duplicate-member.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, raw)
        archive.writestr("manifest.json", json.dumps(manifest))

    with pytest.raises(EvidenceInputError, match="duplicate artifact member"):
        import_evidence_archive(
            object(), path=path, project="srfn-client", binary_id="client:test")
