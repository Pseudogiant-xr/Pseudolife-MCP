"""Regenerate Phase 1 per-function ownership from checked pinned evidence."""
import ast
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

from .stdio_capture import BEHAVIOR_TESTS, ORACLE_HEAD, ORACLE_SCHEMA, require_pinned_phase1_source as require_phase1_source
from .provenance import ROOT


def regenerate(exploration, root=ROOT):
    # A new oracle needs reviewed counts; incomplete exploration cannot set them.
    expected = {
        "686b3f95c4a4d4e6c2d81e1b76be9901945a8da1": {
            "schema": 55, "functions": 191, "test_files": 425,
            "buckets": {"candidate": 10, "oracle": 1, "internal": 180},
        },
        "3c01bb31abd60178e15dea99adda369b4bbf92fc": {
            "schema": 55, "functions": 191, "test_files": 423,
            "buckets": {"candidate": 10, "oracle": 1, "internal": 180},
        },
    }.get(ORACLE_HEAD)
    if expected is None:
        raise RuntimeError("unsupported Phase 1 inventory pin")
    if ORACLE_SCHEMA != expected["schema"]:
        raise RuntimeError("Phase 1 inventory schema differs from selected pin")
    require_phase1_source(root)
    evidence = json.loads(Path(exploration).read_text(encoding="utf-8"))
    scoped_files = {path for path in BEHAVIOR_TESTS if Path(path).name.startswith("test_")}
    classified_files = [file["file"] for file in evidence["files"]]
    if len(classified_files) != len(scoped_files) or set(classified_files) != scoped_files:
        raise RuntimeError("unexpected Phase 1 classification file coverage")
    buckets_path = root / "rust/phase1-test-buckets.json"
    buckets = json.loads(buckets_path.read_text())
    previous = {item["nodeid"]: item for item in buckets["phase1_functions"]}
    candidate_nodes = []
    functions = []
    for file in evidence["files"]:
        path = file["file"]
        pinned = subprocess.check_output(["git", "show", f"{ORACLE_HEAD}:{path}"], cwd=root)
        current = (root / path).read_bytes()
        if pinned.replace(b"\r\n", b"\n") != current.replace(b"\r\n", b"\n"):
            raise RuntimeError("classified test source differs from pin: " + path)
        pinned_functions = {f"{path}::{node.name}": node.lineno for node in ast.parse(pinned).body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name.startswith("test_")}
        items = file["functions"]
        if (len(items) != len(pinned_functions)
                or {item["nodeid"]: item["line"] for item in items} != pinned_functions):
            raise RuntimeError("unexpected Phase 1 function coverage: " + path)
        for item in file["functions"]:
            is_candidate = item["bucket"] == "candidate"
            if is_candidate:
                candidate_nodes.append(item["nodeid"])
            # Refresh source ownership without replacing recorded equivalence evidence.
            function = dict(previous.get(item["nodeid"], {
                "equivalent": "pending" if item.get("planned_equivalent") else None,
                "required_equivalent": item.get("planned_equivalent"), "acceptance": "pending"}))
            function.update({"nodeid": item["nodeid"], "line": item["line"],
                "bucket": item["bucket"], "scope": item["scope"], "reason": item["reason"],
                "source_sha256": hashlib.sha256(pinned).hexdigest()})
            functions.append(function)
    if (len(functions) != expected["functions"]
            or dict(Counter(item["bucket"] for item in functions)) != expected["buckets"]):
        raise RuntimeError("unexpected Phase 1 classification cardinality")
    manifest_path = root / "evals/rust_port/oracle_tests.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["mapped"].update({node: "stdio-shim-process" for node in candidate_nodes})
    manifest["oracle_commit"] = ORACLE_HEAD
    buckets["oracle_commit"] = ORACLE_HEAD
    buckets["database_schema"] = ORACLE_SCHEMA
    buckets["phase1_functions"] = functions
    buckets["phase1_function_counts"] = dict(Counter(item["bucket"] for item in functions))
    pinned_files = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", ORACLE_HEAD, "tests"],
                                           cwd=root, text=True).splitlines()
    pinned_files = {path for path in pinned_files if path.startswith("tests/test_") and path.endswith(".py")}
    if len(pinned_files) != expected["test_files"]:
        raise RuntimeError("unexpected Phase 1 pinned test-file cardinality")
    existing = {file["path"] for file in buckets["files"]}
    for path in sorted(pinned_files - existing):
        buckets["files"].append({"path": path, "bucket": "internal", "equivalent": "pending",
                               "reason": "Pinned Python implementation tests; no public process adapter mapped."})
    buckets["files"] = sorted((file for file in buckets["files"] if file["path"] in pinned_files), key=lambda file: file["path"])
    for file in buckets["files"]:
        if file["path"] == "tests/test_shim.py":
            file.update(bucket="candidate", candidate_nodes=candidate_nodes, remaining_nodes="oracle",
                        reason="Ten public shim launch functions; each other function classified separately. Runtime acceptance remains pending.")
    buckets["counts"] = dict(Counter(file["bucket"] for file in buckets["files"]))
    buckets["candidate_node_count"] = len(manifest["mapped"])
    spec = importlib.util.spec_from_file_location("phase1_pinned_inventory", root / "rust/contract_inventory.py")
    inventory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inventory)
    snapshot = inventory.snapshot(oracle=ORACLE_HEAD)
    if snapshot["database_schema"] != expected["schema"]:
        raise RuntimeError("Phase 1 inventory schema differs from selected pin")
    inventory.validate_phase1_functions(functions, oracle=ORACLE_HEAD, root=root)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    buckets_path.write_text(json.dumps(buckets, indent=2) + "\n", encoding="utf-8")
    (root / "rust/phase1-contract-inventory.json").write_text(
        json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return {"candidate_functions": len(candidate_nodes), "classified_functions": len(functions),
            "source_checks": "passed", "oracle_commit": ORACLE_HEAD}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exploration", type=Path)
    print(json.dumps(regenerate(parser.parse_args().exploration)))
