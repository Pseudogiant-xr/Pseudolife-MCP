"""Bind Phase 1 reports to actual pytest outcomes and executable bytes."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

from .stdio_judge import stderr_evidence_complete


def receipt_status(receipt):
    # Judge-sensitivity mutations are diagnostics with a source capture, not
    # process observations that can satisfy the genuine capture evidence gate.
    groups = [receipt["differences"], receipt.get("normalizations_applied", []),
              *(control["differences"] for control in receipt.get("process_controls", {}).get("controls", {}).values())]
    if not all(stderr_evidence_complete(differences) for differences in groups):
        return "incomplete"
    if receipt["differences"] or not receipt.get("process_tests", {"passed": True})["passed"]:
        return "failed"
    if any(not control["rejected"] for control in receipt.get("judge_sensitivity_controls", {}).values()):
        return "failed"
    return "passed" if receipt["coverage_complete"] else "incomplete"


def command_identity(command, root):
    executable = shutil.which(command[0]) if not Path(command[0]).is_absolute() else command[0]
    if executable is None:
        executable = Path(root) / command[0]
    path = Path(executable).resolve(strict=True)
    return {"executable_basename": path.name,
            "executable_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "executable_bytes": path.stat().st_size,
            "arguments": command[1:] if command[1:] == ["-m", "pseudolife_memory.cli"] else None,
            "public_cli_module": command[1:] == ["-m", "pseudolife_memory.cli"]}


def pytest_outcomes(report, nodes):
    """A zero exit with missing, duplicate, skipped or errored nodes is not a pass."""
    if not Path(report).is_file():
        return {"outcomes": [], "report_complete": False, "all_nodes_passed": False}
    try:
        cases = ET.parse(report).getroot().iter("testcase")
        outcomes = []
        for case in cases:
            node = case.attrib.get("classname", "").replace(".", "/") + ".py::" + case.attrib.get("name", "")
            children = {child.tag for child in case}
            outcome = ("error" if "error" in children else "failed" if "failure" in children
                       else "skipped" if "skipped" in children else "passed")
            outcomes.append({"nodeid": node, "outcome": outcome})
    except (ET.ParseError, OSError):
        return {"outcomes": [], "report_complete": False, "all_nodes_passed": False}
    actual = [item["nodeid"] for item in outcomes]
    complete = len(actual) == len(nodes) and len(set(actual)) == len(actual) and set(actual) == set(nodes)
    return {"outcomes": [item for item in outcomes if item["nodeid"] in nodes], "report_complete": complete,
            "unexpected_testcases": sum(item["nodeid"] not in nodes for item in outcomes),
            "all_nodes_passed": complete and all(item["outcome"] == "passed" for item in outcomes),
            "junit_sha256": hashlib.sha256(Path(report).read_bytes()).hexdigest()}


def _git(root, *arguments):
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, timeout=10)
    if result.returncode:
        raise RuntimeError("candidate source identity requires a native Git checkout; git " + arguments[0] + " failed")
    return result.stdout.decode("utf-8").strip()


def rust_source_identity(source_root):
    try:
        root = Path(source_root).resolve(strict=True)
    except OSError:
        raise RuntimeError("candidate source identity requires an existing native Git checkout") from None
    paths = _git(root, "ls-files", "--", "rust").splitlines()
    paths = [name for name in paths if Path(name).suffix in (".rs", ".toml") or Path(name).name == "Cargo.lock"]
    if not paths:
        raise RuntimeError("candidate source identity found no tracked Rust sources")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=all", "--", "rust")
    if dirty:
        raise RuntimeError("candidate source identity requires committed clean Rust source")
    return {"source_head": _git(root, "rev-parse", "HEAD"),
            "source_tree": _git(root, "rev-parse", "HEAD^{tree}"),
            "source_dirty": False,
            "source_files_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths},
            "source_binding": "clean native Git checkout; executable hash, not a build attestation"}


def candidate_identity(command, source_root):
    try:
        root = Path(source_root).resolve(strict=True)
    except OSError:
        raise RuntimeError("candidate source identity requires an existing native Git checkout") from None
    executable = command_identity(command, root)
    if executable["public_cli_module"]:
        return {**executable, "source_head": _git(root, "rev-parse", "HEAD"),
                "source_tree": _git(root, "rev-parse", "HEAD^{tree}"),
                "source_binding": "Python CLI at the checked production oracle pin"}
    return {**executable, **rust_source_identity(root)}


def candidate_bindings(receipt, identity):
    bindings = {"real_bank_arms": [], "eof_cells": [], "fault_cells": [],
                "startup_cells": [], "concurrent_cells": []}
    for key, cells in (("real_bank_arms", receipt["arms"]),
                       ("eof_cells", receipt["eof"]["cells"]),
                       ("fault_cells", receipt.get("faults", {}).get("cells", [])),
                       ("startup_cells", receipt.get("scenarios", {}).get("startup", {}).get("cells", [])),
                       ("concurrent_cells", receipt.get("scenarios", {}).get("concurrent", {}).get("cells", []))):
        for cell in cells:
            if key != "eof_cells" and cell.get("arm") != "candidate":
                continue
            observed = cell["command_identity"]
            if any(observed[field] != identity[field] for field in ("executable_basename", "executable_sha256")):
                raise RuntimeError("candidate executable identity changed in " + key)
            bindings[key].append({**{field: cell[field] for field in ("era", "case", "release_order") if field in cell},
                                  "case": cell.get("case", "non-eof-concurrent" if key == "concurrent_cells" else "real-bank"),
                                  "command_identity": observed})
    if "process_tests" in receipt:
        observed = receipt["process_tests"]["command_identity"]
        if observed != {key: identity[key] for key in observed}:
            raise RuntimeError("candidate process tests used a different executable identity")
        bindings["process_tests"] = observed
    bindings["checked_cell_count"] = sum(len(bindings[key]) for key in (
        "real_bank_arms", "eof_cells", "fault_cells", "startup_cells", "concurrent_cells"))
    return bindings


def reusable_python_process_receipt(old, current, nodes, identity):
    tests = old.get("process_tests", {})
    return (old.get("schema") == current["schema"]
            and old.get("oracle_head") == current["oracle_head"]
            and old.get("capture_platform") == current["capture_platform"]
            and old.get("instrument_sha256") == current["instrument_sha256"]
            and old.get("baseline_instrument_sha256") == current["baseline_instrument_sha256"]
            and tests.get("passed") is True and tests.get("exit_code") == 0
            and tests.get("report_complete") is True and tests.get("all_nodes_passed") is True
            and tests.get("nodes") == nodes
            and tests.get("outcomes") == [{"nodeid": node, "outcome": "passed"} for node in nodes]
            and tests.get("command_identity") == identity
            and bool(tests.get("junit_sha256"))
            and bool(old.get("arms"))
            and all(arm.get("command_identity", {}).get("public_cli_module") for arm in old["arms"]))
