"""Dependency checks never promote claims or mutate historical evidence."""
import copy
import json
import pytest
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


def manifest():
    return {"kind": "recovery_dependency_manifest", "schema_version": 1,
            "project": "example", "binary_id": "sample:sha256:" + "a" * 64,
            "bindings": {"function": "b" * 64, "layout": "c" * 64},
            "claims": [
                {"id": "read", "requires": {"function": "b" * 64}, "depends_on": []},
                {"id": "ownership", "requires": {"layout": "c" * 64}, "depends_on": ["read"]}]}


def audit(data):
    from pseudolife_memory.re_dependencies import audit_dependencies
    return audit_dependencies(data, project=data["project"], binary_id=data["binary_id"])


def test_unchanged_dependencies_are_current_without_verifying_claims():
    data = manifest()
    before = copy.deepcopy(data)
    result = audit(data)
    assert result["recheck_queue"] == []
    assert [c["freshness"] for c in result["claims"]] == ["current", "current"]
    assert result["automatic_verification"] is False
    assert data == before


def test_change_propagates_and_gives_leaf_reason():
    data = manifest()
    data["bindings"]["function"] = "d" * 64
    result = audit(data)
    assert result["recheck_queue"] == ["read", "ownership"]
    assert result["claims"][0]["reasons"][0]["kind"] == "changed"
    assert result["claims"][1]["reasons"] == [{"kind": "dependency", "claim": "read"}]


def test_missing_binding_is_not_current():
    data = manifest()
    del data["bindings"]["layout"]
    assert audit(data)["recheck_queue"] == ["ownership"]


@pytest.mark.parametrize("change", ["cycle", "unknown", "duplicate", "hash", "scope", "empty"])
def test_invalid_graph_refuses(change):
    data = manifest()
    if change == "cycle": data["claims"][0]["depends_on"] = ["ownership"]
    if change == "unknown": data["claims"][0]["depends_on"] = ["absent"]
    if change == "duplicate": data["claims"].append(copy.deepcopy(data["claims"][0]))
    if change == "hash": data["bindings"]["function"] = "similar-function"
    if change == "scope": data["project"] = "wrong"
    if change == "empty": data["claims"][0]["requires"] = {}
    from pseudolife_memory.re_dependencies import audit_dependencies
    with pytest.raises(ValueError):
        audit_dependencies(data, project="example", binary_id=manifest()["binary_id"])


def test_new_build_is_refused_even_with_matching_hashes():
    from pseudolife_memory.re_dependencies import audit_dependencies
    with pytest.raises(ValueError, match="scope"):
        audit_dependencies(manifest(), project="example", binary_id="sample:other")


def test_service_uses_bounded_parser_and_does_not_write(tmp_path):
    import threading
    from pseudolife_memory.service import MemoryService
    path = tmp_path / "deps.json"
    path.write_text(json.dumps(manifest()), encoding="utf-8")
    service = MemoryService.__new__(MemoryService)
    service._lock = threading.RLock()
    service._ensure_postgres_storage = lambda: pytest.fail("audit must not write storage")
    out = service.re_evidence_dependencies(project="example", binary_id=manifest()["binary_id"], path=str(path))
    assert out["recheck_queue"] == []
    assert len(out["manifest_sha256"]) == 64


def test_mcp_dispatch_and_scope_required(monkeypatch):
    from pseudolife_memory import mcp_server
    monkeypatch.setattr(mcp_server.service, "re_evidence_dependencies", lambda **kw: kw)
    out = mcp_server.re_evidence(action="dependencies", project="example", binary_id="build", path="deps.json")
    assert out == {"project": "example", "binary_id": "build", "path": "deps.json"}
    from tests.test_mcp_server import _invoke
    assert _invoke("re_evidence", {"action": "dependencies", "project": "example", "binary_id": "build", "path": "deps.json"}) == out
    with pytest.raises(ValueError, match="binary_id"):
        mcp_server.re_evidence(action="dependencies", project="example", path="deps.json")


def test_audit_manifest_and_result_roundtrip_as_immutable_evidence(pg_url, pg_conn, tmp_path):
    import threading
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.storage.postgres import PostgresStorage
    storage = PostgresStorage(pg_url)
    service = MemoryService.__new__(MemoryService)
    service._lock = threading.RLock()
    service._ensure_postgres_storage = lambda: storage
    data = manifest()
    path = tmp_path / "dependencies.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        args = {"project": data["project"], "binary_id": data["binary_id"]}
        recorded = service.re_evidence_ingest(path=str(path), kind="recovery-dependencies", **args)
        result = service.re_evidence_dependencies(path=str(path), **args)
        result_path = tmp_path / "audit.json"
        result_path.write_text(json.dumps(result), encoding="utf-8")
        service.re_evidence_ingest(path=str(result_path), kind="recovery-audit", **args)
        data["bindings"]["function"] = "e" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        assert service.re_evidence_dependencies(path=str(path), **args)["recheck_queue"] == ["read", "ownership"]
        stored = storage.get_re_evidence_for_export(artifact_id=recorded["id"], **args)
        assert json.loads(stored["raw_bytes"])["bindings"]["function"] == "b" * 64
        assert storage.query_re_claims(**args) == []
    finally:
        storage.close()


def test_deep_chain_avoids_recursion_and_is_dependency_first():
    data = manifest()
    data["bindings"] = {}
    data["claims"] = [{"id": "node-0", "requires": {"function": "b" * 64}, "depends_on": []}]
    data["claims"].extend({"id": f"node-{i}", "requires": {}, "depends_on": [f"node-{i-1}"]} for i in range(1, 1500))
    result = audit(data)
    assert len(result["recheck_queue"]) == 1500
    assert result["recheck_queue"][-1] == "node-1499"
