"""Regenerate Phase 1 per-function ownership from checked pinned evidence."""
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

from .stdio_capture import ORACLE_HEAD, require_phase1_source
from .provenance import ROOT


def regenerate(exploration, root=ROOT):
    require_phase1_source(root)
    evidence = json.loads(Path(exploration).read_text(encoding="utf-8"))
    candidate_nodes = []
    functions = []
    for file in evidence["files"]:
        path = file["file"]
        pinned = subprocess.check_output(["git", "show", f"{ORACLE_HEAD}:{path}"], cwd=root)
        current = (root / path).read_bytes()
        if pinned.replace(b"\r\n", b"\n") != current.replace(b"\r\n", b"\n"):
            raise RuntimeError("classified test source differs from pin: " + path)
        for item in file["functions"]:
            is_candidate = item["bucket"] == "candidate"
            if is_candidate:
                candidate_nodes.append(item["nodeid"])
            functions.append({"nodeid": item["nodeid"], "line": item["line"],
                "bucket": item["bucket"], "scope": item["scope"], "reason": item["reason"],
                "equivalent": "pending" if item.get("planned_equivalent") else None,
                "required_equivalent": item.get("planned_equivalent"),
                "acceptance": "pending", "source_sha256": hashlib.sha256(pinned).hexdigest()})
    if len(candidate_nodes) != 8 or len(functions) != 189:
        raise RuntimeError("unexpected Phase 1 classification cardinality")
    manifest_path = root / "evals/rust_port/oracle_tests.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["mapped"].update({node: "stdio-shim-process" for node in candidate_nodes})
    manifest["oracle_commit"] = ORACLE_HEAD
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    buckets_path = root / "rust/phase1-test-buckets.json"
    buckets = json.loads(buckets_path.read_text())
    buckets["oracle_commit"] = ORACLE_HEAD
    buckets["phase1_functions"] = functions
    buckets["phase1_function_counts"] = dict(Counter(item["bucket"] for item in functions))
    pinned_files = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", ORACLE_HEAD, "tests"],
                                           cwd=root, text=True).splitlines()
    pinned_files = {path for path in pinned_files if path.startswith("tests/test_") and path.endswith(".py")}
    existing = {file["path"] for file in buckets["files"]}
    for path in sorted(pinned_files - existing):
        buckets["files"].append({"path": path, "bucket": "internal", "equivalent": "pending",
                               "reason": "Pinned Python implementation tests; no public process adapter mapped."})
    buckets["files"] = sorted((file for file in buckets["files"] if file["path"] in pinned_files), key=lambda file: file["path"])
    for file in buckets["files"]:
        if file["path"] == "tests/test_shim.py":
            file.update(bucket="candidate", candidate_nodes=candidate_nodes, remaining_nodes="oracle",
                        reason="Eight public shim launch functions; each other function classified separately.")
    buckets["counts"] = dict(Counter(file["bucket"] for file in buckets["files"]))
    buckets["candidate_node_count"] = len(manifest["mapped"])
    buckets_path.write_text(json.dumps(buckets, indent=2) + "\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("phase1_pinned_inventory", root / "rust/contract_inventory.py")
    inventory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inventory)
    (root / "rust/phase1-contract-inventory.json").write_text(
        json.dumps(inventory.snapshot(oracle=ORACLE_HEAD), indent=2) + "\n", encoding="utf-8")
    return {"candidate_functions": len(candidate_nodes), "classified_functions": len(functions),
            "source_checks": "passed", "oracle_commit": ORACLE_HEAD}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exploration", type=Path)
    print(json.dumps(regenerate(parser.parse_args().exploration)))
