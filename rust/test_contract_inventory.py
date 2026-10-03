"""Inventory assertions; existing oracle tests remain byte-identical."""
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("contract_inventory", Path(__file__).with_name("contract_inventory.py"))
inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inventory)


def test_every_pinned_test_file_has_one_bucket_and_candidate_nodes_exist():
    result = inventory.validate()
    assert result["missing_surfaces"] == []


def test_snapshot_ignores_newer_checkout_sources(monkeypatch):
    original = inventory.source
    expected = json.loads(original("rust/contract-inventory.json"))
    def current_source(path):
        assert not path.startswith(("pseudolife_memory/", "ops/", "plugin/")), "read newer checkout source"
        return original(path)
    monkeypatch.setattr(inventory, "source", current_source)
    assert inventory.snapshot() == expected


def test_audit_ignores_newer_checkout_test_files(monkeypatch):
    expected = json.loads(inventory.source("rust/contract-inventory.json"))
    monkeypatch.setattr(inventory, "snapshot", lambda: expected)
    original = Path.glob
    def with_new_test(path, pattern):
        yield from original(path, pattern)
        if path == inventory.ROOT / "tests" and pattern == "test_*.py":
            yield path / "test_newer_master_surface.py"
    monkeypatch.setattr(Path, "glob", with_new_test)
    assert inventory.validate()["test_files"] == 406


def test_missing_candidate_node_is_not_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    path = tmp_path / "tests/test_example.py"
    path.parent.mkdir()
    path.write_text('def test_present():\n    pass\n', encoding="utf-8")
    assert "tests/test_example.py::test_absent" not in inventory.literal_node_ids("tests/test_example.py")


def test_literal_parameterization_keeps_concrete_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    path = tmp_path / "test_example.py"
    path.write_text('@pytest.mark.parametrize("flag", ["--help", "help"])\ndef test_help(flag):\n    pass\n', encoding="utf-8")
    assert inventory.literal_node_ids("test_example.py") == {
        "test_example.py::test_help[--help]", "test_example.py::test_help[help]"}


@pytest.mark.parametrize("mode", ["invite", "pair", "expose", "move"])
@pytest.mark.parametrize("status", ["deferred", "complete"])
def test_audit_rejects_detached_cli_rows(monkeypatch, mode, status):
    original = inventory.source
    register = original("rust/PARITY.md").replace("\n\n| invite |", "\n| invite |")
    row = f"| {mode} | 2 | deferred |"
    register = register.replace(row, f"\n| {mode} | 2 | {status} |")
    monkeypatch.setattr(inventory, "source", lambda path: register if path == "rust/PARITY.md" else original(path))
    with pytest.raises(AssertionError, match="CLI mode outside status table"):
        inventory.validate()


@pytest.mark.parametrize("fault,error", [
    ("missing-file", "^missing or extra test-file bucket$"),
    ("duplicate-file", "^duplicate test-file bucket$"),
    ("missing-node", "^candidate node does not exist:"),
    ("unsupported-node", "^external plugin cannot route:"),
    ("missing-surface", "^missing required surface row: MCP-MOUNT$"),
    ("invalid-status", "^invalid parity status:"),
])
def test_audit_rejects_incomplete_or_invalid_inventory(monkeypatch, fault, error):
    original = inventory.source
    manifest = json.loads(original("rust/test-buckets.json"))
    register = original("rust/PARITY.md")
    candidate = next(item for item in manifest["files"] if item["bucket"] == "candidate")
    if fault == "missing-file":
        manifest["files"].pop()
    elif fault == "duplicate-file":
        manifest["files"].append(manifest["files"][0])
    elif fault == "missing-node":
        candidate["candidate_nodes"][0] = candidate["path"] + "::test_absent"
    elif fault == "unsupported-node":
        candidate["candidate_nodes"][0] = candidate["path"] + "::test_version_from_a_runtime_names_its_directory_and_commit"
    elif fault == "missing-surface":
        register = register.replace("| MCP-MOUNT |", "| OMITTED |")
    elif fault == "invalid-status":
        register = register.replace("| deferred |", "| complete |", 1)
    def changed(path):
        if path == "rust/test-buckets.json":
            return json.dumps(manifest)
        if path == "rust/PARITY.md":
            return register
        return original(path)
    monkeypatch.setattr(inventory, "source", changed)
    with pytest.raises(AssertionError, match=error):
        inventory.validate()
