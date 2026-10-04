"""Additive Phase 1 audit; historical audit assertions are unchanged."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def inventory():
    path = Path(__file__).resolve().parents[2] / "rust/contract_inventory.py"
    spec = importlib.util.spec_from_file_location("phase1_contract_inventory", path)
    inventory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inventory)
    return inventory


def test_phase1_inventory_has_all_pinned_files_and_pending_function_ownership(inventory):
    assert inventory.PHASE1_ORACLE == "f709abb54f7912ae9cd767998d0926ca33df4bcd"
    result = inventory.validate_phase1()
    assert result["test_files"] == 419
    assert result["candidate_nodes"] == 13
    assert result["buckets"] == {"oracle": 68, "candidate": 2, "internal": 349}


def test_legacy_inventory_retains_its_real_pin(inventory):
    assert inventory.ORACLE == "3691f5cb75487d3fda54a6bde6fab35dcf32c681"
    result = inventory.validate()
    assert result["test_files"] == 406
    assert result["candidate_nodes"] == 5
    assert result["buckets"] == {"oracle": 68, "candidate": 1, "internal": 337}


@pytest.mark.parametrize("phase1,count", [(False, 406), (True, 419)])
def test_newer_checkout_test_cannot_shift_either_pin(inventory, monkeypatch, tmp_path, phase1, count):
    newer = tmp_path / "test_newer_master_surface.py"
    newer.write_text("def test_newer():\n    pass\n", encoding="utf-8")
    original = Path.glob
    def with_new_test(path, pattern):
        yield from original(path, pattern)
        if path == inventory.ROOT / "tests" and pattern == "test_*.py":
            yield newer
    monkeypatch.setattr(Path, "glob", with_new_test)
    validate = inventory.validate_phase1 if phase1 else inventory.validate
    assert validate()["test_files"] == count


@pytest.mark.parametrize("phase1", [False, True])
@pytest.mark.parametrize("fault,error", [
    ("missing-file", "missing or extra test-file bucket"),
    ("duplicate-file", "duplicate test-file bucket"),
    ("missing-node", "candidate node does not exist:"),
    ("duplicate-node", "duplicate candidate node"),
    ("missing-mapping", "external plugin cannot route:"),
])
def test_both_audits_reject_incomplete_candidates(inventory, monkeypatch, phase1, fault, error):
    original = inventory.source
    buckets_path = "rust/phase1-test-buckets.json" if phase1 else "rust/test-buckets.json"
    mapping_path = "evals/rust_port/oracle_tests.json" if phase1 else "rust/phase0b-oracle-tests.json"
    manifest = json.loads(original(buckets_path))
    mappings = json.loads(original(mapping_path))
    candidate = next(item for item in manifest["files"] if item["bucket"] == "candidate")
    if fault == "missing-file":
        manifest["files"].pop()
    elif fault == "duplicate-file":
        manifest["files"].append(manifest["files"][0])
    elif fault == "missing-node":
        candidate["candidate_nodes"][0] = candidate["path"] + "::test_absent"
    elif fault == "duplicate-node":
        candidate["candidate_nodes"].append(candidate["candidate_nodes"][0])
    else:
        del mappings["mapped"][candidate["candidate_nodes"][0]]
    def changed(path):
        if path == buckets_path:
            return json.dumps(manifest)
        if path == mapping_path:
            return json.dumps(mappings)
        return original(path)
    monkeypatch.setattr(inventory, "source", changed)
    validate = inventory.validate_phase1 if phase1 else inventory.validate
    with pytest.raises(AssertionError, match=error):
        validate()


def test_phase1_scoped_equivalents_preserve_complete_ownership(inventory):
    manifest = json.loads(inventory.source("rust/phase1-test-buckets.json"))
    functions = manifest["phase1_functions"]
    assert len(functions) == 189
    assert manifest["phase1_function_counts"] == {"candidate": 8, "internal": 181}
    scoped = [item for item in functions if item["bucket"] == "internal" and item["scope"] == "phase1"]
    assert len(scoped) == 125
    assert all(item["required_equivalent"] or item.get("equivalence_evidence") for item in scoped)
    assert all(item["acceptance"] == "pending" for item in functions)
    assert all(item["equivalent"] == "pending" for item in functions if item["required_equivalent"])


@pytest.mark.parametrize("fault,error", [
    ("missing-evidence", "missing targeted equivalence evidence"),
    ("missing-platform", "missing targeted equivalence evidence"),
    ("missing-target", "equivalent Rust function is absent"),
])
def test_phase1_rejects_incomplete_completed_equivalents(inventory, monkeypatch, fault, error):
    original = inventory.source
    manifest = json.loads(original("rust/phase1-test-buckets.json"))
    item = next(item for item in manifest["phase1_functions"] if item.get("equivalence_evidence"))
    if fault == "missing-evidence":
        item.pop("equivalence_evidence")
    elif fault == "missing-platform":
        item["equivalence_evidence"]["validation"].pop("linux")
    else:
        target = item["equivalence_evidence"]["rust_tests"][0]
        target = target.rsplit("::", 1)[0] + "::no_such_equivalent_assertion"
        item["equivalence_evidence"]["rust_tests"][0] = target
        item["equivalent"] = "rust-unit:" + target
    monkeypatch.setattr(inventory, "source", lambda path: json.dumps(manifest)
                        if path == "rust/phase1-test-buckets.json" else original(path))
    with pytest.raises(AssertionError, match=error):
        inventory.validate_phase1()


@pytest.mark.parametrize("fault,error", [
    ("missing-function", "missing per-function Phase 1 ownership"),
    ("claimed-acceptance", "Phase 1 acceptance requires execution evidence"),
    ("claimed-equivalent", "proposed target is not a passed equivalent"),
])
def test_phase1_rejects_missing_or_unverified_equivalents(inventory, monkeypatch, fault, error):
    original = inventory.source
    manifest = json.loads(original("rust/phase1-test-buckets.json"))
    functions = manifest["phase1_functions"]
    if fault == "missing-function":
        functions.pop()
    elif fault == "claimed-acceptance":
        functions[0]["acceptance"] = "passed"
    else:
        item = next(item for item in functions
                    if item["bucket"] == "internal" and item["scope"] == "phase1")
        item["required_equivalent"] = {"status": "required-not-created-not-validated"}
        item["equivalent"] = "rust-unit:unverified"
    monkeypatch.setattr(inventory, "source", lambda path: json.dumps(manifest)
                        if path == "rust/phase1-test-buckets.json" else original(path))
    with pytest.raises(AssertionError, match=error):
        inventory.validate_phase1()


@pytest.mark.parametrize("phase1", [False, True])
def test_shared_register_rejects_names_absent_from_both_documented_pins(inventory, monkeypatch, phase1):
    original = inventory.source
    def unknown_reference(path):
        text = original(path)
        return text + "\n`test_unknown_oracle_reference.py`\n" if path == "rust/PARITY.md" else text
    monkeypatch.setattr(inventory, "source", unknown_reference)
    with pytest.raises(AssertionError, match="obsolete test reference: test_unknown_oracle_reference.py"):
        inventory.validate(phase1=phase1)
