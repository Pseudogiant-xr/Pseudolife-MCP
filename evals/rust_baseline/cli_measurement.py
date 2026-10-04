"""Paired CLI cold-start-to-exit measurement using the existing repeat floors."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

from .common import lease_gate, provenance
from .shim_measurement import artifact_identity, metric_cells, require_pinned_source
from evals.rust_port.harness import capture_platform, isolated_env, run_cli, write_new
from evals.rust_port.phase1_receipts import candidate_identity, command_identity
from evals.rust_port.provenance import require_import_root, runtime_metadata


def measure(args, resource):
    root = args.oracle_root.resolve()
    pin = require_pinned_source(root)
    require_import_root(root)
    runtime = runtime_metadata(root)
    if sys.version_info[:2] != (3, 11) or not runtime["source_origin_matches_selected_root"] \
            or runtime["package_runtime_version"] != "0.16.1" \
            or runtime["distribution_versions"]["pseudolife-mcp"] != "0.16.1":
        raise RuntimeError("CLI measurement requires the genuine pinned installed package runtime")
    commands = {"python": [sys.executable, "-m", "pseudolife_memory.cli"],
                "rust": [str(args.candidate.resolve())]}
    identity = candidate_identity(commands["rust"], args.candidate_root)
    python_identity = command_identity(commands["python"], root)
    binary = artifact_identity(args.candidate, args.candidate_sha256)
    runs = {"python": [], "rust": []}
    resource_checks = []
    with tempfile.TemporaryDirectory(prefix="rust-port-cli-measure-") as temporary:
        directory = Path(temporary)
        # The untimed exact-byte control must pass before any timing is recorded.
        controls = {arm: run_cli(command, ["help"], cwd=root,
                                env=isolated_env(directory / f"control-{arm}"), timeout=10)
                    for arm, command in commands.items()}
        if controls["python"] != controls["rust"] or controls["python"]["exit_code"] != 0:
            raise RuntimeError("help byte control failed; measurement not comparable")
        for repeat in range(args.repeats):
            resource_checks.append(resource if repeat == 0 or args.smoke else lease_gate(
                args.board_checked_at, offline_resource_checked_at=args.offline_resource_checked_at))
            for sample in range(args.samples):
                order = ("python", "rust") if (repeat * args.samples + sample) % 2 == 0 else ("rust", "python")
                for arm in order:
                    env = isolated_env(directory / f"{repeat}-{sample}-{arm}")
                    started = time.perf_counter()
                    response = run_cli(commands[arm], ["help"], cwd=root, env=env, timeout=10)
                    elapsed = (time.perf_counter() - started) * 1000
                    if response != controls[arm]:
                        raise RuntimeError("CLI bytes changed during measurement")
                    runs[arm].append({"repeat": repeat, "sample": sample,
                                      "arm_order": list(order), "cold_start_to_exit_ms": elapsed,
                                      "executable_bytes": (python_identity if arm == "python" else identity)["executable_bytes"]})
    require_pinned_source(root)
    if candidate_identity(commands["rust"], args.candidate_root) != identity \
            or command_identity(commands["python"], root) != python_identity:
        raise RuntimeError("CLI source or executable changed during measurement")
    return {"schema": 1, "status": "contaminated-plumbing-smoke" if args.smoke else "quiet-cli-pair-final",
            "capture_platform": capture_platform(), "provenance": provenance(source_root=root),
            "python_oracle": pin, "candidate_identity": identity,
            "candidate_executable": binary, "python_executable": python_identity,
            "mode": "help", "argv": ["help"], "resource_check": resource,
            "repeat_resource_checks": resource_checks, "repeats": args.repeats,
            "samples_per_repeat": args.samples, "runs": runs,
            "metrics": {arm: metric_cells(rows, ("cold_start_to_exit_ms", "executable_bytes"))
                        for arm, rows in runs.items()},
            "byte_control": controls,
            "limitations": ["Fresh public CLI process with warm OS filesystem cache; no daemon, database or models.",
                            "Timing includes owned-process setup, complete output collection and clean exit.",
                            "CLI exit timing is not comparable to shim first-frame or initialize-return timing.",
                            "Executable size excludes interpreter dependencies; noise floors are descriptive repeat-block ranges.",
                            "Smoke is plumbing evidence only." if args.smoke else "Final samples require the caller's quiet CPU window."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--smoke", action="store_true")
    clearance = parser.add_mutually_exclusive_group(required=True)
    clearance.add_argument("--board-checked-at")
    clearance.add_argument("--offline-resource-checked-at")
    args = parser.parse_args()
    if args.repeats < 1 or args.samples < 1 or (not args.smoke and (args.repeats != 3 or args.samples != 10)):
        parser.error("final CLI measurement requires three repeats of ten samples; smoke needs positive counts")
    resource = lease_gate(args.board_checked_at, offline_resource_checked_at=args.offline_resource_checked_at)
    result = measure(args, resource)
    write_new(args.out, result)
    print(json.dumps({"receipt": args.out.name, "status": result["status"], "mode": result["mode"]}))


if __name__ == "__main__":
    main()
