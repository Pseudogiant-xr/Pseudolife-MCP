"""RE evidence pilot: immutable artifacts and evidence-gated claims."""

from __future__ import annotations

import json
import threading
import time
from contextlib import nullcontext

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

        text_rows = storage.query_re_evidence(
            project="srfn-client", binary_id="decomp_sro_client.exe:sha256:test",
            text="00b72510")
        assert [row["id"] for row in text_rows] == [first]
    finally:
        storage.close()


def test_storage_lists_re_evidence_scopes_with_counts(pg_url, pg_conn):
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        def add(project: str, binary_id: str, locator: str, digest: str) -> int:
            return storage.insert_re_evidence({
                "project": project, "kind": "ghidra-function",
                "locator": locator, "source_path": f"evidence/{locator}.json",
                "content_hash": digest * 64, "payload": {"address": locator},
                "summary": f"Function {locator}", "binary_id": binary_id,
                "addresses": [locator], "raw_bytes": b"{}",
                "payload_keys": ["address"],
            })

        old_id = add("srfn-client", "client:old", "00100000", "1")
        storage.upsert_re_claim(
            project="srfn-client", binary_id="client:old", subject="00100000",
            claim="old build behavior", status="verified",
            evidence_ids=[old_id], confidence=1.0)
        time.sleep(0.001)
        add("srfn-client", "client:new", "00200000", "2")

        scopes = storage.re_evidence_scopes()

        selected = [s for s in scopes if s["project"] == "srfn-client"]
        assert [s["binary_id"] for s in selected] == ["client:new", "client:old"]
        assert selected[0]["artifacts"] == 1
        assert selected[0]["claims"] == {}
        assert selected[1]["artifacts"] == 1
        assert selected[1]["claims"] == {"verified": 1}
    finally:
        storage.close()


def test_service_re_evidence_dashboard_uses_latest_scope_and_filters():
    from pseudolife_memory.service import MemoryService

    class Storage:
        def re_evidence_scopes(self):
            return [{
                "project": "srfn-client", "binary_id": "client:test",
                "artifacts": 2, "claims": {"verified": 1},
                "last_activity": 123.0,
            }]

        def query_re_evidence(self, **kwargs):
            assert kwargs == {
                "project": "srfn-client", "binary_id": "client:test",
                "text": "login", "limit": 25, "include_payload": False,
            }
            return [{"id": 7, "locator": "00abcdef"}]

        def query_re_claims(self, **kwargs):
            assert kwargs == {
                "project": "srfn-client", "binary_id": "client:test",
                "status": "verified", "text": "login", "limit": 25,
            }
            return [{"id": 3, "status": "verified"}]

    service = MemoryService.__new__(MemoryService)
    service._lock = threading.RLock()
    service._ensure_postgres_storage = lambda: Storage()

    out = service.re_evidence_dashboard(
        text="login", status="verified", limit=25)

    assert out["selection"] == {
        "project": "srfn-client", "binary_id": "client:test"}
    assert out["totals"] == {"artifacts": 2, "claims": {"verified": 1}}
    assert out["artifacts"] == [{"id": 7, "locator": "00abcdef"}]
    assert out["claims"] == [{"id": 3, "status": "verified"}]


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


def test_claim_update_preserves_links_when_evidence_ids_are_omitted(
        pg_url, pg_conn):
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    try:
        evidence_id = storage.insert_re_evidence({
            "project": "srfn-client", "kind": "ghidra-function",
            "locator": "00b72870", "source_path": "evidence/a.json",
            "content_hash": "9" * 64, "payload": {"address": "00b72870"},
            "summary": None, "binary_id": "client:test",
            "addresses": ["00b72870"], "raw_bytes": b"{}",
            "payload_keys": ["address"],
        })
        storage.upsert_re_claim(
            project="srfn-client", binary_id="client:test", subject="00b72870",
            claim="collision dispatch succeeds", status="observed",
            evidence_ids=[evidence_id])

        storage.upsert_re_claim(
            project="srfn-client", binary_id="client:test", subject="00b72870",
            claim="collision dispatch succeeds", status="verified")
        rows = storage.query_re_claims(
            project="srfn-client", binary_id="client:test", subject="00b72870")
        assert rows[0]["status"] == "verified"
        assert rows[0]["evidence_ids"] == [evidence_id]

        storage.upsert_re_claim(
            project="srfn-client", binary_id="client:test", subject="00b72870",
            claim="collision dispatch succeeds", status="hypothesis")

        rows = storage.query_re_claims(
            project="srfn-client", binary_id="client:test", subject="00b72870")
        assert rows[0]["status"] == "hypothesis"
        assert rows[0]["evidence_ids"] == [evidence_id]

        storage.upsert_re_claim(
            project="srfn-client", binary_id="client:test", subject="00b72870",
            claim="collision dispatch succeeds", status="hypothesis",
            evidence_ids=[])
        rows = storage.query_re_claims(
            project="srfn-client", binary_id="client:test", subject="00b72870")
        assert rows[0]["evidence_ids"] == []
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


def test_replay_missing_after_conflict_is_a_clean_input_error():
    from pseudolife_memory.re_evidence import EvidenceInputError
    from pseudolife_memory.storage.re_evidence import ReEvidenceArchiveStorage

    class Result:
        def fetchone(self):
            return None

    class Connection:
        closed = False
        broken = False

        def execute(self, *_args, **_kwargs):
            return Result()

    storage = ReEvidenceArchiveStorage(Connection())
    storage._txn = lambda: nullcontext()
    artifact = {
        "project": "srfn-client", "kind": "ghidra-function",
        "locator": "00b72870", "source_path": "evidence/a.json",
        "content_hash": "8" * 64, "payload": {"address": "00b72870"},
        "summary": None, "binary_id": "client:test",
        "addresses": ["00b72870"], "raw_bytes": b"{}",
        "payload_keys": ["address"],
    }

    with pytest.raises(EvidenceInputError, match="concurrent replay"):
        storage.insert_re_evidence(artifact)


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

    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        with pg_conn.transaction():
            pg_conn.execute(
                "UPDATE re_evidence_artifacts SET binary_id = 'build:b' "
                "WHERE id = %s", (evidence_a,))


@pytest.mark.parametrize("assignment", [
    "raw_bytes = decode('00', 'hex')",
    "payload = '{\"forged\": true}'::jsonb",
    "content_hash = repeat('f', 64)",
    "locator = 'deadbeef'",
    "source_path = 'forged.json'",
    "kind = 'forged'",
    "summary = 'forged'",
    "addresses = ARRAY['deadbeef']::text[]",
    "payload_keys = ARRAY['forged']::text[]",
    "ingested_at = 2",
])
def test_database_rejects_every_artifact_update(pg_conn, assignment):
    import psycopg

    evidence_id = int(pg_conn.execute(
        "INSERT INTO re_evidence_artifacts "
        "(project, binary_id, kind, locator, source_path, content_hash, "
        "raw_bytes, payload, payload_keys, addresses, ingested_at) "
        "VALUES ('srfn-client', 'build:a', 'ghidra-function', '00b72870', "
        "'evidence.json', %s, '{}'::bytea, '{}'::jsonb, '{}', '{}', 1) "
        "RETURNING id", ("7" * 64,)).fetchone()[0])
    pg_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        with pg_conn.transaction():
            pg_conn.execute(
                f"UPDATE re_evidence_artifacts SET {assignment} WHERE id = %s",
                (evidence_id,))


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
            binary_id="client:test", archive_root=tmp_path)
        assert exported["sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
        from pseudolife_memory.re_evidence import EvidenceInputError
        with pytest.raises(
                EvidenceInputError,
                match="project and binary_id must be non-empty"):
            import_evidence_archive(
                storage, path=archive_path, project=" ",
                binary_id="client:test", archive_root=tmp_path)
        with pytest.raises(EvidenceInputError, match="empty project/build"):
            import_evidence_archive(
                storage, path=archive_path, project="srfn-client",
                binary_id="client:test", archive_root=tmp_path)

        with storage._txn():
            storage.conn.execute("DELETE FROM re_claims WHERE project = 'srfn-client'")
            storage.conn.execute(
                "DELETE FROM re_evidence_artifacts WHERE project = 'srfn-client'")

        # A different daemon may begin writing this scope while an import is
        # ready to commit. Hold the same transaction-scoped lock the importer
        # must take, add a row, and prove the second connection waits and then
        # rechecks emptiness instead of merging the archive into live state.
        from pseudolife_memory.re_evidence import _archive_scope_lock_key

        blocker = storage.re_evidence_archive_storage()
        importer = storage.re_evidence_archive_storage()
        started = threading.Event()
        result: list[object] = []

        def concurrent_import():
            started.set()
            try:
                result.append(import_evidence_archive(
                    importer, path=archive_path, project="srfn-client",
                    binary_id="client:test", archive_root=tmp_path))
            except Exception as exc:  # asserted below across the thread boundary
                result.append(exc)

        try:
            with blocker._txn():
                blocker.conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (_archive_scope_lock_key("srfn-client", "client:test"),))
                thread = threading.Thread(target=concurrent_import)
                thread.start()
                assert started.wait(timeout=1)
                thread.join(timeout=0.25)
                assert thread.is_alive(), "import did not wait for the scope lock"
                blocker.insert_re_evidence(artifact)
            thread.join(timeout=2)
            assert not thread.is_alive()
            assert len(result) == 1
            assert isinstance(result[0], EvidenceInputError)
            assert "empty project/build" in str(result[0])

            # Ordinary production writers must honor the same scope lock, not
            # only competing importers. Otherwise they can commit after the
            # import's empty check and merge live state into the restore.
            writer = storage.re_evidence_archive_storage()
            writer_result: list[object] = []
            try:
                with blocker._txn():
                    blocker.conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (_archive_scope_lock_key(
                            "srfn-client", "client:test"),))

                    def concurrent_writer():
                        try:
                            writer_result.append(
                                writer.insert_re_evidence(artifact))
                        except Exception as exc:
                            writer_result.append(exc)

                    writer_thread = threading.Thread(target=concurrent_writer)
                    writer_thread.start()
                    writer_thread.join(timeout=0.25)
                    assert writer_thread.is_alive(), (
                        "production evidence writer ignored the scope lock")
                writer_thread.join(timeout=2)
                assert not writer_thread.is_alive()
                assert len(writer_result) == 1
                assert isinstance(writer_result[0], int)
            finally:
                writer.close()
        finally:
            blocker.close()
            importer.close()

        with storage._txn():
            storage.conn.execute("DELETE FROM re_claims WHERE project = 'srfn-client'")
            storage.conn.execute(
                "DELETE FROM re_evidence_artifacts WHERE project = 'srfn-client'")
        imported = import_evidence_archive(
            storage, path=archive_path, project="srfn-client",
            binary_id="client:test", archive_root=tmp_path)
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
            object(), path=path, project="srfn-client", binary_id="client:test",
            archive_root=tmp_path)


def test_archive_paths_are_confined_to_the_configured_root(tmp_path):
    from pseudolife_memory.re_evidence import (
        EvidenceInputError, resolve_evidence_archive_path)

    root = tmp_path / "archives"
    assert resolve_evidence_archive_path(
        "proof.zip", archive_root=root) == (root / "proof.zip").resolve()

    with pytest.raises(EvidenceInputError, match="archive root"):
        resolve_evidence_archive_path(
            tmp_path / "outside.zip", archive_root=root)

    root.mkdir()
    outside = tmp_path / "outside-target.zip"
    outside.write_bytes(b"not an archive")
    link = root / "linked.zip"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(EvidenceInputError, match="archive root"):
        resolve_evidence_archive_path(link, archive_root=root)


@pytest.mark.parametrize("operation", ["export", "import"])
def test_archive_file_io_does_not_hold_the_service_lock(
        monkeypatch, tmp_path, operation):
    from pseudolife_memory import re_evidence
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.storage import postgres

    service = MemoryService.__new__(MemoryService)
    service._lock = threading.Lock()
    service.data_dir = tmp_path

    class PrimaryStorage:
        dsn = "postgresql://archive-test"

        def verify_writer_session(self):
            pass

        def re_evidence_archive_storage(self):
            return ArchiveStorage()

    class Connection:
        def transaction(self, **_kwargs):
            return nullcontext()

        def execute(self, statement):
            assert statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

    class ArchiveStorage:
        conn = Connection()

        def close(self):
            pass

    service._ensure_postgres_storage = lambda: PrimaryStorage()
    monkeypatch.setattr(postgres, "PostgresStorage", lambda _dsn: pytest.fail(
        "archive must not create another bank writer"))

    def file_io(_storage, **_kwargs):
        acquired = []

        def probe():
            got = service._lock.acquire(timeout=0.2)
            acquired.append(got)
            if got:
                service._lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        assert acquired == [True], "archive I/O held the global service lock"
        return {"operation": operation}

    monkeypatch.setattr(
        re_evidence, f"{operation}_evidence_archive", file_io)
    result = getattr(service, f"re_evidence_{operation}")(
        project="srfn-client", binary_id="client:test", path="proof.zip")

    assert result == {"operation": operation}


def test_archive_connection_does_not_initialize_or_reacquire_bank_lease(monkeypatch):
    from pseudolife_memory.storage.postgres import PostgresStorage

    primary = PostgresStorage.__new__(PostgresStorage)
    connection = object()
    monkeypatch.setattr(primary, "_connect", lambda: connection)
    monkeypatch.setattr(primary, "_acquire_writer_lease", lambda _conn: pytest.fail(
        "archive must reuse ownership rather than acquire another bank lease"))
    archive = primary.re_evidence_archive_storage()
    assert archive.conn is connection
    assert not hasattr(archive, "set_meta")
    assert not hasattr(archive, "insert_entry")


def test_archive_runs_while_primary_holds_writer_lease(pg_url, tmp_path, monkeypatch):
    from pseudolife_memory import re_evidence
    from pseudolife_memory.service import MemoryService

    service = MemoryService(data_dir=tmp_path, database_url=pg_url)
    monkeypatch.setenv("PSEUDOLIFE_RE_EVIDENCE_ARCHIVE_ROOT", str(tmp_path))
    primary = service._ensure_postgres_storage()
    try:
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(_sample_payload()), encoding="utf-8")
        ingested = service.re_evidence_ingest(
            path=str(path), project="archive-owner", binary_id="build:one")
        service.re_claim_record(
            project="archive-owner", binary_id="build:one", subject="00b72870",
            claim="calls the indexed function", status="verified",
            evidence_ids=[ingested["id"]])
        export = re_evidence.export_evidence_archive

        def assert_snapshot(storage, **kwargs):
            assert storage.conn.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
            assert storage.conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
            return export(storage, **kwargs)

        monkeypatch.setattr(re_evidence, "export_evidence_archive", assert_snapshot)
        result = service.re_evidence_export(
            project="archive-owner", binary_id="build:one", path="owned.zip")
        assert result["artifacts"] == 1
        with primary._txn():
            primary.conn.execute("DELETE FROM re_claims WHERE project = 'archive-owner'")
            primary.conn.execute("DELETE FROM re_evidence_artifacts WHERE project = 'archive-owner'")
        restored = service.re_evidence_import(
            project="archive-owner", binary_id="build:one", path="owned.zip")
        assert restored["artifacts"] == restored["claims"] == 1
        assert service.re_evidence_query(
            project="archive-owner", binary_id="build:one")["claims"][0]["status"] == "verified"
        # The archive connection must not take ownership away from the
        # resident primary, including its lease epoch and canonical state.
        primary.verify_writer_session()
        with service._re_evidence_archive_storage() as archive:
            with pytest.raises(RuntimeError, match="rollback probe"):
                with archive._txn():
                    archive.upsert_re_claim(
                        project="archive-rollback", binary_id="build:one",
                        subject="00b72870", claim="temporary", status="hypothesis")
                    raise RuntimeError("rollback probe")
        assert primary.re_evidence_stats("archive-rollback")["claims"] == {}
    finally:
        primary.close()
