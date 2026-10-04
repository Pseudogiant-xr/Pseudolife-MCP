"""Pinned CLI self-replay, strict candidate bytes and unchanged process assertions."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .cli_corpus import corpus
from .harness import (Policy, capture_platform, compare, execute, isolated_env,
                      replay, write_new)
from .phase1_receipts import candidate_identity, command_identity, pytest_outcomes
from .processes import owned_process
from .provenance import module_command, require_import_root, runtime_metadata
from .stdio_capture import require_phase1_source


def selected_nodes():
    manifest = json.loads(Path(__file__).with_name("oracle_tests.json").read_text(encoding="utf-8"))
    return [node for node, boundary in manifest["mapped"].items() if boundary == "cli-main-process"]


def process_tests(command, root, directory, label):
    nodes = selected_nodes()
    identity = command_identity(command, root)
    report = directory / f"{label}.junit.xml"
    log = directory / f"{label}.log"
    if report.exists() or log.exists():
        raise FileExistsError("CLI process-test evidence paths must be new")
    env = isolated_env(directory / f"{label}-home")
    env["PSEUDOLIFE_PORT_CLI_JSON"] = json.dumps(command)
    argv = module_command("pytest", root, ["-p", "evals.rust_port.pytest_plugin", "-q",
            "--junitxml", str(report.resolve()), "-o", "junit_logging=no", *nodes])
    with owned_process(argv, cwd=root, env=env, stdin=subprocess.DEVNULL) as process:
        stdout, stderr = process.communicate(timeout=120)
    log.write_bytes(stdout + stderr)
    if command_identity(command, root) != identity:
        raise RuntimeError("CLI process-test executable changed during pytest")
    outcomes = pytest_outcomes(report, nodes)
    return {"nodes": nodes, "exit_code": process.returncode, **outcomes,
            "command_identity": identity,
            "selector": "PSEUDOLIFE_PORT_CLI_JSON",
            "passed": process.returncode == 0 and outcomes["all_nodes_passed"],
            "cleanup": dict(process.owned_cleanup)}


def graded_controls(records):
    """Change only actual observations; each CLI field must be load-bearing."""
    controls = []
    for record in records:
        expected = record["response"]
        for field in ("exit_code", "stdout_b64", "stderr_b64"):
            actual = dict(expected)
            actual[field] = ((expected[field] + 1) % 256 if field == "exit_code"
                             else base64.b64encode(base64.b64decode(expected[field]) + b"\n").decode("ascii"))
            differences = compare(expected, actual, Policy(**record["policy"]))
            controls.append({"case": record["id"], "field": field,
                             "rejected": bool(differences), "differences": differences})
    return controls


def run(root, command, candidate_root, evidence_directory, resource):
    source = require_phase1_source(root)
    require_import_root(root)
    runtime = runtime_metadata(root)
    if sys.version_info[:2] != (3, 11) or not runtime["source_origin_matches_selected_root"] \
            or runtime["package_runtime_version"] != "0.16.1" \
            or runtime["distribution_versions"]["pseudolife-mcp"] != "0.16.1":
        raise RuntimeError("CLI capture requires the genuine pinned installed package runtime")
    python = [sys.executable, "-m", "pseudolife_memory.cli"]
    identity = candidate_identity(command, candidate_root)
    python_identity = command_identity(python, root)
    spec = corpus()
    watched = ("tests/test_cli_dispatch.py", "tests/conftest.py")
    hashes = lambda: {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in watched}
    before = hashes()
    with tempfile.TemporaryDirectory(prefix="rust-port-cli-") as temporary:
        directory = Path(temporary)
        records = execute(spec["cases"], cli_prefix=python, base_url=None,
                          cwd=root, home=directory / "capture")
        transcript = {"schema": 1, "capture_platform": capture_platform(),
                      "fixture": spec["fixture"], "records": records}
        control = replay(transcript, cli_prefix=python, base_url=None,
                         cwd=root, home=directory / "self-replay")
        candidate = replay(transcript, cli_prefix=command, base_url=None,
                           cwd=root, home=directory / "candidate")
    controls = graded_controls(records)
    evidence_directory.mkdir(parents=True, exist_ok=True)
    tests = {name: process_tests(prefix, root, evidence_directory, name) for name, prefix in (
        ("python", python), ("candidate", command))}
    if before != hashes():
        raise RuntimeError("immutable Python dispatcher or conftest source changed")
    if command_identity(python, root) != python_identity or candidate_identity(command, candidate_root) != identity:
        raise RuntimeError("CLI executable or candidate source changed during capture")
    return {"schema": 1, "capture_platform": capture_platform(), **source,
            "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "resource_check": resource, "capture_runtime": runtime,
            "candidate_identity": identity, "oracle_command_identity": python_identity,
            "policy": "ordinary exit and exact stdout/stderr bytes; no normalization",
            "transcript": transcript, "python_self_replay": control,
            "candidate_replay": candidate, "graded_controls": controls,
            "process_tests": tests, "unchanged_sources_sha256": before,
            "passed": control["passed"] and candidate["passed"]
                      and all(cell["rejected"] for cell in controls)
                      and all(result["passed"] for result in tests.values()),
            "limitation": "Help and Unicode-scalar unknown dispatch only; version/runtime identity, "
                          "non-UTF-8 argv and other mode contracts remain deferred."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-json", required=True)
    parser.add_argument("--evidence-directory", type=Path, required=True, help="private pytest logs and JUnit")
    parser.add_argument("--out", type=Path, required=True)
    clearance = parser.add_mutually_exclusive_group(required=True)
    clearance.add_argument("--board-checked-at")
    clearance.add_argument("--offline-resource-checked-at")
    args = parser.parse_args()
    command = json.loads(args.candidate_json)
    if not isinstance(command, list) or not command or not all(isinstance(value, str) and value for value in command):
        parser.error("--candidate-json must be a nonempty JSON string array")
    from evals.rust_baseline.common import lease_gate
    resource = lease_gate(args.board_checked_at, offline_resource_checked_at=args.offline_resource_checked_at)
    result = run(args.oracle_root.resolve(), command, args.candidate_root.resolve(),
                 args.evidence_directory.resolve(), resource)
    write_new(args.out, result)
    print(json.dumps({"passed": result["passed"], "cases": result["candidate_replay"]["cases"],
                      "receipt": args.out.name}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
