"""Regeneration keeps the pinned classification and historical evidence intact."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from . import phase1_inventory
from .provenance import ROOT


OUTPUTS = ("evals/rust_port/oracle_tests.json", "rust/phase1-test-buckets.json",
           "rust/phase1-contract-inventory.json")


def raw_hashes(root, paths):
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths}


@pytest.fixture(scope="module")
def disposable_inventory(tmp_path_factory):
    root = tmp_path_factory.mktemp("phase1-regeneration") / "source"
    subprocess.run(["git", "clone", "--shared", "--quiet", str(ROOT), str(root)], check=True)
    buckets = json.loads((root / OUTPUTS[1]).read_text(encoding="utf-8"))
    files = {}
    for row in buckets["phase1_functions"]:
        path = row["nodeid"].split("::", 1)[0]
        item = {key: row[key] for key in ("nodeid", "line", "bucket", "scope", "reason")}
        # Historical exploration proposed replacements before completed evidence existed.
        if row["scope"] == "phase1" and row["bucket"] == "internal":
            item["planned_equivalent"] = {"status": "required-not-created-not-validated"}
        files.setdefault(path, {"file": path, "functions": []})["functions"].append(item)
    return root, {"files": list(files.values())}


def test_current_pin_regeneration_matches_canonical_artifacts_and_preserves_raw_receipts(
        disposable_inventory, tmp_path):
    root, evidence = disposable_inventory
    exploration = tmp_path / "exploration.json"
    exploration.write_text(json.dumps(evidence), encoding="utf-8")
    expected = {path: json.loads((root / path).read_text(encoding="utf-8")) for path in OUTPUTS}
    historical = [path.relative_to(root).as_posix()
                  for path in (root / "evals/results").rglob("*") if path.is_file()]
    historical += ["evals/rust_port/stdio_startup_contract.f709.json",
                   "rust/test-buckets.json", "rust/contract-inventory.json",
                   "rust/phase0b-oracle-tests.json"]
    before = raw_hashes(root, historical)
    first = phase1_inventory.regenerate(exploration, root)
    assert first == {"candidate_functions": 10, "classified_functions": 191,
                     "source_checks": "passed", "oracle_commit": phase1_inventory.ORACLE_HEAD}
    assert {path: json.loads((root / path).read_text(encoding="utf-8")) for path in OUTPUTS} == expected
    after_first = raw_hashes(root, OUTPUTS)
    assert raw_hashes(root, historical) == before
    assert phase1_inventory.regenerate(exploration, root) == first
    assert raw_hashes(root, OUTPUTS) == after_first
    assert raw_hashes(root, historical) == before


@pytest.mark.parametrize("fault,error", [
    ("historical-189", "function coverage"),
    ("missing-file", "classification file coverage"),
    ("duplicate-node", "function coverage"),
    ("wrong-line", "function coverage"),
    ("wrong-bucket", "classification cardinality"),
    ("unknown-pin", "unsupported Phase 1 inventory pin"),
    ("wrong-schema", "inventory schema differs"),
])
def test_incomplete_or_unpinned_exploration_cannot_overwrite_artifacts(
        disposable_inventory, tmp_path, monkeypatch, fault, error):
    root, original = disposable_inventory
    evidence = copy.deepcopy(original)
    functions = evidence["files"][0]["functions"]
    if fault == "historical-189":
        functions[:] = [item for item in functions if item["nodeid"] not in {
            "tests/test_shim.py::test_shim_passes_a_tool_refusal_through_unchanged",
            "tests/test_shim.py::test_shim_forwards_the_daemons_unknown_parameter_refusal"}]
    elif fault == "missing-file":
        evidence["files"].pop()
    elif fault == "duplicate-node":
        functions[3] = copy.deepcopy(functions[0])
    elif fault == "wrong-line":
        functions[0]["line"] += 1
    elif fault == "wrong-bucket":
        functions[0]["bucket"] = "internal"
    elif fault == "unknown-pin":
        monkeypatch.setattr(phase1_inventory, "ORACLE_HEAD", "f709abb54f7912ae9cd767998d0926ca33df4bcd")
    else:
        monkeypatch.setattr(phase1_inventory, "ORACLE_SCHEMA", 54)
    exploration = tmp_path / "exploration.json"
    exploration.write_text(json.dumps(evidence), encoding="utf-8")
    before = raw_hashes(root, OUTPUTS)
    with pytest.raises(RuntimeError, match=error):
        phase1_inventory.regenerate(exploration, root)
    assert raw_hashes(root, OUTPUTS) == before
