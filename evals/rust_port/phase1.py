"""Bounded Phase 1 public-process tests and disposable stdio differential judge."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import hashlib
import time

from .harness import capture_platform, write_new
from .provenance import ROOT, runtime_metadata, module_command
from .stdio_capture import require_phase1_source
from .stdio_corpus import ERAS, observe
from .stdio_judge import (StdioPolicy, judge_with_evidence, eof_policy, judge_sensitivity_controls,
                          retain_stderr, verifies_frozen_capture, READINESS_WAIT_RULE, readiness_wait_evidence)
from .processes import owned_process
from .phase1_receipts import command_identity, pytest_outcomes, candidate_identity, candidate_bindings
from .phase1_receipts import reusable_python_process_receipt
from .phase1_receipts import receipt_status


def selected_nodes():
    manifest = json.loads(Path(__file__).with_name("oracle_tests.json").read_text())
    return [node for node, boundary in manifest["mapped"].items() if boundary == "stdio-shim-process"]


def process_tests(command, root, output):
    nodes = selected_nodes()
    identity = command_identity(command, root)
    report = output.with_suffix(".junit.xml")
    if output.exists() or report.exists():
        raise FileExistsError("process-test evidence paths must be new")
    argv = module_command("pytest", root, ["-p", "evals.rust_port.pytest_plugin", "-q",
            "--junitxml", str(report.resolve()), "-o", "junit_logging=no",
            "--port-stdio-json", json.dumps(command), *nodes])
    import os
    with owned_process(argv, cwd=root, env=dict(os.environ), stdin=subprocess.DEVNULL) as process:
        stdout, stderr = process.communicate(timeout=240)
    # Keep raw test output private. The public receipt includes only command
    # identity, selected nodes and pytest's per-node outcomes. JUnit messages
    # and captured streams stay private: they can include local paths or URLs.
    output.write_bytes(stdout + stderr)
    if command_identity(command, root) != identity:
        raise RuntimeError("process-test executable changed during pytest")
    outcomes = pytest_outcomes(report, nodes)
    return {"nodes": nodes, "exit_code": process.returncode, **outcomes,
            "command_identity": identity,
            "passed": process.returncode == 0 and outcomes["all_nodes_passed"],
            "cleanup": dict(process.owned_cleanup)}


def corpus(root, command, clearance):
    from evals.rust_baseline.common import lease_gate
    from evals.rust_baseline.daemon import disposable_database, private_directory, launched_daemon
    from evals.rust_baseline.transport import TOKEN
    from .full_bank import private_home_overrides
    resource = lease_gate(offline_resource_checked_at=clearance)
    arms = []
    for name, prefix in (("oracle", [sys.executable, "-m", "pseudolife_memory.cli"]),
                         ("candidate", command)):
        # Each era and each arm gets a fresh bank, preserving integer identities
        # and toolset state rather than comparing two runs over mutated state.
        for era in ERAS:
            with disposable_database() as dsn, private_directory() as private:
                overrides = private_home_overrides(private)
                with launched_daemon(dsn, private, source_root=root, env_extra=overrides,
                        child_module="evals.rust_port.stdio_minimal_daemon", startup_timeout=90) as (_, url, cleanup):
                    result = observe(root, Path(private) / "shim", url, TOKEN, era, prefix, candidate=name == "candidate")
                    result.update(arm=name, daemon_cleanup=cleanup)
                    arms.append(result)
            cleanup["database_dropped"] = True
            print(json.dumps({"arm": name, "era": era, "frames": len(result["stdout"]),
                              "exit": result["exit_code"], "database_dropped": True}), flush=True)
    policy = StdioPolicy(readiness_wait_notice=True)
    differences = [{"era": expected["era"], **difference}
        for expected, actual in zip(arms[:2], arms[2:]) for difference in judge_with_evidence(expected, actual, policy)]
    return {"arms": arms, "differences": differences, "resource_check": resource,
            "policy": policy.name, "named_normalizations": ["source-text-lf", READINESS_WAIT_RULE],
            "normalizations_applied": [{"era": expected["era"], **event}
                for expected, actual in zip(arms[:2], arms[2:])
                for event in readiness_wait_evidence(expected, actual, policy)],
            "judge_sensitivity_controls": judge_sensitivity_controls(arms[0], policy)}


def eof(root, command):
    from evals.rust_baseline.daemon import private_directory
    from .stdio_eof import observe as eof_observe
    evidence = json.loads(Path(__file__).with_name("stdio_eof_orders.json").read_text())
    cells, differences, oracle_captures, frozen_verifications = [], [], {}, []
    same_platform = capture_platform() == evidence.get("capture_platform")
    for historical in evidence["cells"]:
        key = historical["era"], historical["case"]
        # Keep same-platform frozen bytes authoritative. A matching live
        # process can supply provenance; an unchecked one cannot rebaseline it.
        if key not in oracle_captures:
            with private_directory() as private:
                expected = eof_observe(root, Path(private) / "oracle", *key)
            expected["arm"] = "oracle"
            oracle_captures[key] = expected
        live = oracle_captures[key]
        expected = historical if same_platform else live
        with private_directory() as private:
            actual = eof_observe(root, Path(private) / "shim", expected["era"], expected["case"],
                                 command=command, validate_oracle=False)
        actual["arm"] = "candidate"
        cells.append(actual)
        policy = eof_policy(evidence, expected["era"], expected["case"])
        compared = judge_with_evidence(expected, actual, policy)
        verified = same_platform and verifies_frozen_capture(historical, live, policy)
        if verified:
            compared = retain_stderr(compared, live, actual)
        if same_platform:
            frozen_verifications.append({"era": expected["era"], "case": expected["case"],
                                         "live_matches_frozen": verified})
        differences.extend({"era": expected["era"], "case": expected["case"], **difference}
                           for difference in compared)
    return {"cells": cells, "live_oracle_captures": list(oracle_captures.values()),
            "frozen_verifications": frozen_verifications,
            "differences": differences, "frozen_oracle_observations": evidence["observations"],
            "evidence_sha256": evidence["evidence_sha256"],
            "normalization_rule": "eof-observed-final-pair-orders",
            "byte_oracle": "same-platform; frozen order evidence does not normalize line framing"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-json", help="JSON argv prefix; defaults to installed checkout Python CLI")
    parser.add_argument("--candidate-root", type=Path,
                        help="native Git checkout of the committed Rust candidate source (required for Rust)")
    parser.add_argument("--out", type=Path, required=True, help="private raw receipt path")
    parser.add_argument("--public-out", type=Path, help="sanitized public summary path")
    parser.add_argument("--offline-resource-checked-at")
    parser.add_argument("--skip-process-tests", action="store_true")
    parser.add_argument("--reuse-python-process-receipt", type=Path,
                        help="reuse same-platform successful Python default 8-node evidence")
    parser.add_argument("--skip-faults", action="store_true")
    parser.add_argument("--skip-generic-controls", action="store_true")
    parser.add_argument("--oracle-root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.oracle_root.resolve()
    source = require_phase1_source(root)
    command = json.loads(args.candidate_json) if args.candidate_json else [sys.executable, "-m", "pseudolife_memory.cli"]
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        parser.error("--candidate-json requires nonempty string argv")
    python_replay = command == [sys.executable, "-m", "pseudolife_memory.cli"]
    if not python_replay and args.candidate_root is None:
        parser.error("Rust candidate requires --candidate-root naming its native Git checkout")
    if not python_replay and not Path(command[0]).is_absolute():
        parser.error("Rust candidate executable requires a native absolute path")
    candidate_root = root if python_replay else args.candidate_root.resolve()
    identity = candidate_identity(command, candidate_root)
    receipt = {"schema": 2, **source, "capture_platform": capture_platform(),
               "replay_kind": "python-self" if python_replay else "rust-candidate",
               "candidate_identity": identity,
               "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "capture_runtime": runtime_metadata(root),
               "instrument_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in sorted([*Path(__file__).parent.glob("*.py"),
                                                        *Path(__file__).parent.glob("*.json")])},
               "baseline_instrument_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted([*(ROOT / "evals/rust_baseline").glob("*.py"),
                                        *(ROOT / "evals/rust_baseline").glob("*.json")])}}
    if args.reuse_python_process_receipt:
        old = json.loads(args.reuse_python_process_receipt.read_text())
        if args.candidate_json or not reusable_python_process_receipt(
                old, receipt, selected_nodes(), command_identity(command, root)):
            parser.error("process-test reuse requires complete same-instrument same-platform Python evidence")
        receipt["process_tests"] = {**old["process_tests"],
            "reused_receipt_sha256": hashlib.sha256(args.reuse_python_process_receipt.read_bytes()).hexdigest()}
    elif not args.skip_process_tests:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        receipt["process_tests"] = process_tests(command, root, args.out.with_suffix(".pytest.log"))
    receipt.update(corpus(root, command, args.offline_resource_checked_at))
    receipt["eof"] = eof(root, command)
    receipt["differences"].extend(receipt["eof"]["differences"])
    if not args.skip_faults:
        from .stdio_faults import run as fault_run
        receipt["faults"] = fault_run(root, command)
        receipt["differences"].extend(receipt["faults"]["differences"])
    from .stdio_scenarios import run as scenario_run
    receipt["scenarios"] = scenario_run(root, command)
    receipt["differences"].extend(receipt["scenarios"]["differences"])
    from .stdio_process_controls import run as process_controls
    receipt["process_controls"] = process_controls(root)
    for control in receipt["process_controls"]["controls"].values():
        control["differences"] = retain_stderr(control["differences"],
            receipt["process_controls"]["oracle_observed"], control["observed"])
    if not args.skip_generic_controls:
        from .full_bank import full_corpus, run as generic_run
        _, generic = generic_run(full_corpus(), oracle_root=root,
            offline_resource_checked_at=args.offline_resource_checked_at,
            validate_controls=True, phase1_protocol_fixture=True)
        receipt["generic_controls"] = generic
        receipt["differences"].extend({"surface": "generic", **difference} for difference in generic["differences"])
    if candidate_identity(command, candidate_root) != identity:
        raise RuntimeError("candidate source or executable changed during the judge")
    receipt["candidate_bindings"] = candidate_bindings(receipt, identity)
    receipt["coverage_complete"] = all(key in receipt for key in ("process_tests", "faults", "scenarios", "generic_controls"))
    receipt["status"] = receipt_status(receipt)
    write_new(args.out, receipt)
    if args.public_out:
        safe = {key: value for key, value in receipt.items() if key not in {
            "arms", "eof", "faults", "scenarios", "capture_runtime", "process_controls", "generic_controls"}}
        safe["eof"] = {key: value for key, value in receipt["eof"].items()
                       if key not in {"cells", "live_oracle_captures"}}
        if "faults" in receipt:
            safe["faults"] = {key: value for key, value in receipt["faults"].items() if key != "cells"}
        safe["scenarios"] = {key: value for key, value in receipt["scenarios"].items()
                             if key not in {"startup", "concurrent"}}
        safe["scenarios"]["startup"] = {key: value for key, value in receipt["scenarios"]["startup"].items()
                                        if key != "cells"}
        safe["process_controls"] = {name: {key: value for key, value in result.items() if key != "observed"}
                                    for name, result in receipt["process_controls"]["controls"].items()}
        if "generic_controls" in receipt:
            generic = receipt["generic_controls"]
            safe["generic_controls"] = {key: generic[key] for key in (
                "status", "cases", "differences", "identity_proxy_validation", "oracle_source_check")}
            safe["generic_controls"]["policy_instances"] = generic.get("policy_instances", [])
            safe["generic_controls"]["graded_controls"] = {name: {key: result[key] for key in (
                "rejected", "differences", "expected_difference", "correct_status")} for name, result in generic["graded_controls"].items()}
        write_new(args.public_out, safe)
    print(json.dumps({"status": receipt["status"], "receipt": args.out.name,
                      "differences": len(receipt["differences"])}))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
