"""Paired fresh-process CLI timing after equal warmups, using existing repeat floors."""
import argparse
import base64
import copy
import json
from pathlib import Path
import stat
import sys
import tempfile
import time
import tomllib

from .common import lease_gate, provenance
from .shim_measurement import artifact_identity, metric_cells
from evals.rust_port.cli_corpus import STREAM_CONTRACT
from evals.rust_port.cli_process import (
    checked_metadata, cli_binding, file_path, prepared_command, remove_root_link, reset_home, snapshot,
)
from evals.rust_port.harness import capture_platform, isolated_env, run_cli, write_new
from evals.rust_port.phase1_receipts import candidate_identity, command_identity
from evals.rust_port.provenance import require_import_root, runtime_metadata
from evals.rust_port.stdio_capture import require_phase1_source


def file_identity(command):
    path = Path(command[0]).resolve(strict=True)
    metadata = path.stat()
    return {"path": str(path), "device": metadata.st_dev, "inode": metadata.st_ino,
            "bytes": metadata.st_size, "modified_ns": metadata.st_mtime_ns}


def restore_state(home, files, directories):
    """Restore owned input state without replacing or rewriting unchanged images."""
    current = snapshot(home)  # Reject links/junctions before changing owned state.
    entries = list(home.rglob("*"))
    for path in entries:
        checked_metadata(path)
    for name in current.keys() - files.keys():
        file_path(home, name).unlink()
    for path in sorted(entries, key=lambda path: len(path.parts), reverse=True):
        if path.exists() and stat.S_ISDIR(checked_metadata(path).st_mode) \
                and path.relative_to(home).as_posix() not in directories:
            path.rmdir()
    for name in directories:
        file_path(home, name).mkdir(parents=True, exist_ok=True)
    for name, encoded in files.items():
        if current.get(name) != encoded:
            path = file_path(home, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(encoded, validate=True))


def measure(args, resource, *, prepare=None, case=None):
    mode = getattr(args, "mode", "help")
    warm_images = mode == "version" or getattr(args, "warm_images", False)
    argv = json.loads(args.argv_json) if getattr(args, "argv_json", None) else [mode]
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv) \
            or argv[0] not in {mode, "--" + mode}:
        raise ValueError("measurement argv must name the selected CLI mode")
    root = args.oracle_root.resolve()
    pin = require_phase1_source(root)
    require_import_root(root)
    runtime = runtime_metadata(root)
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if sys.version_info[:2] != (3, 11) or not runtime["source_origin_matches_selected_root"] \
            or runtime["package_runtime_version"] != version \
            or runtime["distribution_versions"]["pseudolife-mcp"] != version:
        raise RuntimeError("CLI measurement requires the genuine pinned installed package runtime")
    commands = {"python": [sys.executable, "-m", "pseudolife_memory.cli"],
                "rust": [str(args.candidate.resolve())]}
    identity = candidate_identity(commands["rust"], args.candidate_root)
    python_identity = command_identity(commands["python"], root)
    binary = artifact_identity(args.candidate, args.candidate_sha256)
    helpers = {"evals/rust_baseline/cli_measurement.py": [Path(__file__)],
               "evals/rust_baseline/common.py": [lease_gate, provenance],
               "evals/rust_baseline/shim_measurement.py": [artifact_identity, metric_cells],
               "evals/rust_port/cli_process.py": [checked_metadata, cli_binding, file_path, prepared_command,
                                                   reset_home, snapshot, remove_root_link],
               "evals/rust_port/harness.py": [capture_platform, isolated_env, run_cli, write_new],
               "evals/rust_port/phase1_receipts.py": [candidate_identity, command_identity],
               "evals/rust_port/provenance.py": [require_import_root, runtime_metadata],
               "evals/rust_port/stdio_capture.py": [require_phase1_source]}
    instrument_binding = cli_binding(args.candidate_root, root, extra_helpers=helpers)
    layout = getattr(args, "layout", "bare")
    if layout == "installed" and prepare is None:
        if mode != "version":
            raise ValueError("installed CLI measurement currently covers version only")
        from evals.rust_port.cli_version import cases, make_prepare
        case = next(row for row in cases() if row["id"] == "version-default-commit")
        case["argv"] = list(argv)
        prepare = make_prepare(root, pin["oracle_head"])
    if (prepare is None) != (case is None):
        raise ValueError("prepared measurement requires both a callback and a recorded case")
    if case is not None and (case["mode"] != mode or case["argv"] != argv or case.get("normalizations")
                             or case.get("environment_deltas") or case.get("pre_files_b64")):
        raise ValueError("prepared measurement needs exact mode/argv and no input normalization or deltas")
    prepared_controls = {}
    runs = {"python": [], "rust": []}
    resource_checks = []
    warmups = []
    with tempfile.TemporaryDirectory(prefix="rust-port-cli-measure-") as temporary:
        directory = Path(temporary)
        home = directory / "cli-home"
        installed = {}
        state_controls = {}
        identities = {}
        baseline_files = {}
        baseline_directories = set()

        def invoke(arm, label, *, timed=False):
            nonlocal baseline_files, baseline_directories
            control = label == "control-" + arm
            reuse = warm_images and not control
            # Keep the historical per-invocation reset for modes without opt-in.
            active_home = home if prepare is not None or warm_images else directory / label
            if reuse:
                for installed_arm, entry in installed.items():
                    if file_identity(entry["selected"]) != identities[installed_arm]:
                        raise RuntimeError("CLI warmed executable file identity changed")
                restore_state(active_home, baseline_files, baseline_directories)
                env = copy.deepcopy(installed[arm]["env"])
                selected = installed[arm]["selected"]
                selected_identity = installed[arm]["selected_identity"]
                original_identity = installed[arm]["original_identity"]
                binding = copy.deepcopy(installed[arm]["binding"])
            elif prepare is None:
                reset_home(active_home)
                env = isolated_env(active_home)
                selected = commands[arm]
                selected_identity = original_identity = command_identity(selected, root)
                binding = None
            else:
                reset_home(active_home)
                env = isolated_env(active_home)
                env.update({"XDG_DATA_HOME": str(active_home / "data"), "PSEUDOLIFE_MCP_NO_SPAWN": "1",
                            "PYTHONDONTWRITEBYTECODE": "1"})
                prefixes = {"oracle": commands["python"], "candidate": commands["rust"]}
                try:
                    selected, selected_identity, original_identity = prepared_command(
                        case, commands[arm], prefixes, root=root, home=active_home, env=env, prepare=prepare)
                    binding = {"environment": copy.deepcopy(env), "pre_files_b64": snapshot(active_home),
                               "execution": {"original_prefix": commands[arm], "selected_prefix": selected,
                                             "effective_argv": [*selected, *argv], "cwd": str(root),
                                             "command_identity": selected_identity}}
                except BaseException:
                    remove_root_link(active_home)
                    raise
            before = snapshot(active_home)
            if control:
                baseline_files = before
                baseline_directories = {path.relative_to(active_home).as_posix() for path in active_home.rglob("*")
                                        if stat.S_ISDIR(checked_metadata(path).st_mode)}
                installed[arm] = {"selected": selected, "env": copy.deepcopy(env), "binding": binding,
                                  "selected_identity": selected_identity, "original_identity": original_identity}
            elif reuse and before != baseline_files:
                raise RuntimeError("CLI input state changed during measurement")
            input_options = {"stdin": base64.b64decode(case.get("stdin_b64", ""), validate=True)} \
                if case is not None else {}
            try:
                started = time.perf_counter() if timed else None
                response = run_cli(selected, argv, cwd=root, env=env, timeout=10, **input_options)
                elapsed = (time.perf_counter() - started) * 1000 if timed else None
                after = snapshot(active_home)
                if reuse and file_identity(selected) != identities[arm]:
                    raise RuntimeError("CLI warmed executable file identity changed")
                if command_identity(selected, root) != selected_identity \
                        or command_identity(commands[arm], root) != original_identity:
                    raise RuntimeError("CLI executable changed during measurement capture")
                state = {"environment": copy.deepcopy(env), "pre_files_b64": before, "post_files_b64": after}
                if reuse and state != state_controls[arm]:
                    raise RuntimeError("CLI files or environment changed during measurement")
                if control:
                    state_controls[arm] = state
                if binding is not None:
                    binding["post_files_b64"] = after
                return response, elapsed, binding
            finally:
                remove_root_link(active_home)

        # The untimed exact-byte control must pass before any timing is recorded.
        controls = {}
        for arm in commands:
            controls[arm], _, binding = invoke(arm, f"control-{arm}")
            if binding is not None:
                prepared_controls[arm] = binding
        if controls["python"] != controls["rust"] or controls["python"]["exit_code"] != 0:
            raise RuntimeError("CLI byte control failed; measurement not comparable")

        def files_and_environment(binding):
            return {key: value for key, value in binding.items() if key != "execution"}

        if prepared_controls and files_and_environment(prepared_controls["python"]) != \
                files_and_environment(prepared_controls["rust"]):
            raise RuntimeError("CLI byte control failed for prepared files or environment")
        if warm_images and state_controls["python"] != state_controls["rust"]:
            raise RuntimeError("CLI byte control failed for files or environment")
        if warm_images:
            identities = {arm: file_identity(entry["selected"]) for arm, entry in installed.items()}
        for repeat in range(args.repeats):
            resource_checks.append(resource if repeat == 0 or args.smoke else lease_gate(
                args.board_checked_at, offline_resource_checked_at=args.offline_resource_checked_at))
            for arm in commands if warm_images else ():
                response, _, _ = invoke(arm, f"warm-{repeat}-{arm}")
                if response != controls[arm]:
                    raise RuntimeError("CLI bytes changed during measurement warmup")
                warmups.append({"repeat": repeat, "arm": arm, "file_identity": identities[arm],
                                "response": response, "files_and_environment_match_control": True})
            for sample in range(args.samples):
                order = ("python", "rust") if (repeat * args.samples + sample) % 2 == 0 else ("rust", "python")
                for arm in order:
                    response, elapsed, binding = invoke(arm, f"{repeat}-{sample}-{arm}", timed=True)
                    if response != controls[arm]:
                        raise RuntimeError("CLI bytes changed during measurement")
                    if binding is not None and files_and_environment(binding) != \
                            files_and_environment(prepared_controls[arm]):
                        raise RuntimeError("CLI files or environment changed during measurement")
                    runs[arm].append({"repeat": repeat, "sample": sample,
                                      "arm_order": list(order), "cold_start_to_exit_ms": elapsed,
                                      **({"warmed_file_identity": identities[arm], "warmed_file_identity_matches": True}
                                         if warm_images else {}),
                                      "executable_bytes": (python_identity if arm == "python" else identity)["executable_bytes"],
                                      **({"execution": binding["execution"], "files_and_environment_match_control": True}
                                         if binding is not None else {})})
    require_phase1_source(root)
    if candidate_identity(commands["rust"], args.candidate_root) != identity \
            or command_identity(commands["python"], root) != python_identity:
        raise RuntimeError("CLI source or executable changed during measurement")
    measured_provenance = provenance(source_root=root)
    if cli_binding(args.candidate_root, root, extra_helpers=helpers) != instrument_binding:
        raise RuntimeError("CLI instrument changed during measurement capture")
    return {"schema": 1, "status": "contaminated-plumbing-smoke" if args.smoke else "quiet-cli-pair-final",
            "capture_platform": capture_platform(), "provenance": measured_provenance,
            "cli_instrument_binding": instrument_binding,
            "python_oracle": pin, "candidate_identity": identity,
            "candidate_executable": binary, "python_executable": python_identity,
            "mode": mode, "argv": argv, "stream_contract": STREAM_CONTRACT, "capture_runtime": runtime,
            "prepared_case": copy.deepcopy(case), "prepared_controls": prepared_controls,
            "resource_check": resource,
            "repeat_resource_checks": resource_checks, "repeats": args.repeats,
            "samples_per_repeat": args.samples, "runs": runs,
            "warmups": warmups, "state_controls": state_controls,
            "start_protocol": "One untimed start per arm before each repeat; timed starts reuse unchanged images and identical restored input state."
                              if warm_images else "Historical per-invocation fixture reset; no explicit image warmup.",
            "metrics": {arm: metric_cells(rows, ("cold_start_to_exit_ms", "executable_bytes"))
                        for arm, rows in runs.items()},
            "byte_control": controls,
            "limitations": ["Fresh public CLI process after one untimed start of each unchanged executable per repeat; no daemon, database or models."
                             if warm_images else "Fresh public CLI process with warm OS filesystem cache; no daemon, database or models.",
                            "UTF-8 stdout/stderr and valid Unicode scalar argv only; Windows CRLF is preserved.",
                            "Locale/default and other output encodings, non-UTF-8 or surrogate argv remain deferred.",
                            "Timing includes owned-process setup, complete output collection and clean exit; preparation/reset/file checks are untimed.",
                            "The historical cold_start_to_exit_ms key measures fresh processes; image warmup is recorded separately in start_protocol.",
                            "CLI exit timing is not comparable to shim first-frame or initialize-return timing.",
                            "Executable size excludes interpreter dependencies; noise floors are descriptive repeat-block ranges.",
                            "Smoke is plumbing evidence only." if args.smoke else "Final samples require the caller's quiet CPU window."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--mode", choices=("help", "version"), default="help")
    parser.add_argument("--warm-images", action="store_true",
                        help="Reuse and warm images before each repeat; default for version, opt-in for other modes")
    parser.add_argument("--argv-json", help="Optional argv for the selected mode")
    parser.add_argument("--layout", choices=("bare", "installed"), default="bare",
                        help="Installed layout uses the installer-schema version case and stable shared home")
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
