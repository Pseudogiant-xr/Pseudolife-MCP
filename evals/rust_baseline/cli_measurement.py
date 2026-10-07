"""Paired fresh-process CLI timing after equal warmups, using existing repeat floors."""
import argparse
import base64
import copy
import hashlib
import json
import os
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
    checked_metadata, cli_binding, file_path, fixture_env, prepared_command, remove_root_link, reset_home, snapshot,
)
from evals.rust_port.harness import _base_url, capture_platform, isolated_env, run_cli, write_new
from evals.rust_port.phase1_receipts import candidate_identity, command_identity
from evals.rust_port.provenance import require_import_root, require_instrument_binding, runtime_metadata
from evals.rust_port.stdio_capture import require_phase1_source


def doorbell_fixture():
    """Reuse the ordinary positive corpus cell, including its recorded seed."""
    from evals.rust_port.cli_doorbell_seen import THREAD, cases
    from pseudolife_memory.codex_doorbell_state import PendingNotice, notice_text
    original = next(row for row in cases(notice_text) if row["id"] == "version2-positive")
    key = hashlib.sha256(THREAD.encode()).hexdigest()
    text = json.dumps(original["pending"], ensure_ascii=False, separators=(",", ":"))
    seed = (text + "\n").encode()
    case = {"id": original["id"], "mode": "doorbell-prompt-seen",
            "argv": ["doorbell-prompt-seen", *original["argv"]], "stdin_b64": original["stdin_b64"],
            "environment_deltas": {"PSEUDOLIFE_DIGEST_DIR": "{home}/digests"},
            "pre_files_b64": {"digests/" + key + ".bell-pending": base64.b64encode(seed).decode()},
            "normalizations": [], "timeout_seconds": 20}

    def prepare(cell, home, env, command, commands):
        if cell != case or env.get("PSEUDOLIFE_DIGEST_DIR") != str(home / "digests"):
            raise ValueError("doorbell preparation requires the recorded positive case")
        pending = PendingNotice(home / "digests", THREAD)
        path = pending.path
        if path != home / next(iter(case["pre_files_b64"])) or path.read_bytes() != seed:
            raise ValueError("doorbell preparation requires the recorded pending bytes")
        path.unlink()
        pending._write_atomic(path, text)
        return command

    return case, prepare


def doorbell_private_metadata(home):
    """Opening each state file proves owner-only access and a single link."""
    from pseudolife_memory.private_state import open_private
    metadata = {}
    for relative in snapshot(home):
        descriptor = open_private(file_path(home, relative), os.O_RDONLY)
        try:
            info = os.fstat(descriptor)
            metadata[relative] = {"private": True, "nlink": info.st_nlink,
                                  "mode": oct(info.st_mode & 0o777)}
        finally:
            os.close(descriptor)
    return metadata


def doorbell_source_binding(root):
    from pseudolife_memory.codex_doorbell_state import PendingNotice, notice_text
    from pseudolife_memory.private_state import open_private
    return require_instrument_binding(root, {
        "pseudolife_memory/codex_doorbell_state.py": [PendingNotice, PendingNotice._write_atomic, notice_text],
        "pseudolife_memory/private_state.py": [open_private]})
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


def lease_check_case():
    """The retained c4fc owned-board case, including its explicit instance bytes."""
    from evals.rust_port.lease_corpus import cases
    case = next(row for row in cases() if row["id"] == "lease-check-absent-json")
    case["environment_deltas"] = {}
    case["timeout_seconds"] = 20
    return case


def lease_list_case():
    """The retained empty owned-board list case, with exact home and URL bytes."""
    from evals.rust_port.lease_corpus import cases
    case = next(row for row in cases() if row["id"] == "lease-list-empty-json")
    case["environment_deltas"] = {}
    case["timeout_seconds"] = 20
    return case


def measure(args, resource, *, prepare=None, case=None, fixture_url=None, verify=None, record_invocation=None):
    mode = getattr(args, "mode", "help")
    warm_images = mode == "version" or getattr(args, "warm_images", False)
    argv = json.loads(args.argv_json) if getattr(args, "argv_json", None) else [mode]
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv) \
            or (argv[0] not in {mode, "--" + mode}
                and not (fixture_url is not None and mode in {"lease-check", "lease-list"}
                         and argv[:2] == ["lease", mode.removeprefix("lease-")])):
        raise ValueError("measurement argv must name the selected CLI mode")
    episode_fixture = fixture_url is not None and mode == "episode-end"
    wait_delivery = fixture_url is not None and mode == "wait-mail"
    if episode_fixture:
        from .transport import TOKEN
        from evals.rust_port.episode_corpus import cases
        _base_url(fixture_url)
        accepted = next(row for row in cases() if row["id"] == "episode-key-7-episode-end")
        if case is not None and case.get("id") == "episode-hook-string42-episode-end":
            accepted["id"] = "episode-hook-string42-episode-end"
            accepted["stdin_b64"] = base64.b64encode(b'{"session_id": "42"}').decode("ascii")
        accepted["environment_deltas"] = {"PSEUDOLIFE_MCP_TOKEN": TOKEN}
        if mode != "episode-end" or argv != accepted["argv"] or case != accepted \
                or getattr(args, "layout", "bare") != "bare" \
                or not callable(prepare) or not callable(verify):
            raise ValueError("daemon measurement requires the recorded authenticated episode-end case and state callbacks")
    elif wait_delivery:
        from evals.rust_port.wait_mail_measurement import measurement_case, project_invocation
        _base_url(fixture_url)
        if mode != "wait-mail" or case != measurement_case() or argv != case["argv"] \
                or prepare is not None or not warm_images or getattr(args, "layout", "bare") != "bare":
            raise ValueError("daemon delivery measurement requires the retained wait-mail case and equal warmups")
    elif fixture_url is not None:
        _base_url(fixture_url)
        if mode == "briefing":
            from .transport import TOKEN
            if argv != ["briefing"] or case is None \
                    or case.get("environment_deltas") != {"PSEUDOLIFE_MCP_TOKEN": TOKEN} \
                    or case.get("stdin_b64", "") != "" or getattr(args, "layout", "bare") != "bare":
                raise ValueError("daemon measurement requires the authenticated plain briefing fixture")
        else:
            admitted_case = {"lease-check": lease_check_case, "lease-list": lease_list_case}.get(mode)
            if admitted_case is None or case != admitted_case() or argv != case["argv"] \
                    or prepare is not None or verify is not None or getattr(args, "layout", "bare") != "bare":
                raise ValueError("daemon measurement requires a retained TOKEN_FILE lease check/list fixture")
    if verify is not None and not episode_fixture:
        raise ValueError("state verification requires the owned episode-end fixture")
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
               "evals/rust_port/provenance.py": [require_import_root, require_instrument_binding, runtime_metadata],
               "evals/rust_port/stdio_capture.py": [require_phase1_source]}
    if wait_delivery:
        from evals.rust_port import wait_mail_policy, cli_wait_mail
        helpers.update({"evals/rust_port/wait_mail_measurement.py": [measurement_case, project_invocation],
                        "evals/rust_port/wait_mail_policy.py": [wait_mail_policy.delivery_projection],
                        "evals/rust_port/cli_wait_mail.py": [cli_wait_mail.cases]})
    if fixture_url is not None:
        from . import transport
        helpers["evals/rust_port/cli_process.py"].extend([fixture_env, file_path])
        helpers["evals/rust_port/harness.py"].append(_base_url)
        if episode_fixture:
            helpers["evals/rust_port/episode_corpus.py"] = [cases]
        helpers["evals/rust_baseline/transport.py"] = [Path(transport.__file__)]
    layout = getattr(args, "layout", "bare")
    doorbell = mode == "doorbell-prompt-seen"
    doorbell_binding = None
    if doorbell:
        if layout != "bare":
            raise ValueError("positive doorbell measurement requires the bare layout")
        from evals.rust_port.cli_doorbell_seen import THREAD, assert_effect, cases
        from pseudolife_memory.codex_doorbell_state import notice_text
        expected_case, default_prepare = doorbell_fixture()
        if case is None and prepare is None:
            case, prepare = copy.deepcopy(expected_case), default_prepare
        if case != expected_case or argv != expected_case["argv"]:
            raise ValueError("doorbell measurement requires the exact recorded positive case")
        original_case = next(row for row in cases(notice_text) if row["id"] == "version2-positive")
        key = hashlib.sha256(THREAD.encode()).hexdigest()
        helpers["evals/rust_port/cli_doorbell_seen.py"] = [cases, assert_effect]
        doorbell_binding = doorbell_source_binding(root)
    instrument_binding = cli_binding(args.candidate_root, root, extra_helpers=helpers)
    if layout == "installed" and prepare is None:
        if mode != "version":
            raise ValueError("installed CLI measurement currently covers version only")
        from evals.rust_port.cli_version import cases, make_prepare
        case = next(row for row in cases() if row["id"] == "version-default-commit")
        case["argv"] = list(argv)
        prepare = make_prepare(root, pin["oracle_head"])
    if fixture_url is None and (prepare is None) != (case is None):
        raise ValueError("prepared measurement requires both a callback and a recorded case")
    if case is not None and (case["mode"] != mode or case["argv"] != argv or case.get("normalizations")
                             or (not doorbell and fixture_url is None and case.get("environment_deltas"))
                             or (not doorbell and (fixture_url is None or mode == "briefing")
                                 and case.get("pre_files_b64"))):
        raise ValueError("prepared measurement needs exact mode/argv and no input normalization or deltas")
    case = copy.deepcopy(case)
    prepared = prepare is not None or fixture_url is not None
    prepared_controls = {}
    runs = {"python": [], "rust": []}
    resource_checks = []
    warmups = []
    state_checks = []
    raw_invocations = []
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
            active_home = home if prepared or warm_images else directory / label
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
                if episode_fixture:
                    # Warm images still need a fresh owned episode before every CLI.
                    prepared_case = copy.deepcopy(case)
                    prefixes = {"oracle": commands["python"], "candidate": commands["rust"]}
                    prepared_image, prepared_identity, prepared_original = prepared_command(
                        prepared_case, commands[arm], prefixes, root=root,
                        home=active_home, env=env, prepare=prepare)
                    if prepared_case != case or prepared_image != selected \
                            or prepared_identity != selected_identity or prepared_original != original_identity:
                        raise ValueError("CLI warmup preparation must retain recorded inputs and images")
            elif wait_delivery:
                reset_home(active_home)
                env = fixture_env(active_home, {"oracle": commands["python"], "candidate": commands["rust"]}, fixture_url)
                for relative, value in case["pre_files_b64"].items():
                    path = file_path(active_home, relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(base64.b64decode(value, validate=True))
                selected = commands[arm]
                selected_identity = original_identity = command_identity(selected, root)
                binding = {"environment": copy.deepcopy(env), "pre_files_b64": snapshot(active_home),
                           "execution": {"original_prefix": commands[arm], "selected_prefix": selected,
                                         "cwd": str(root), "command_identity": selected_identity}}
            elif not prepared:
                reset_home(active_home)
                env = isolated_env(active_home)
                selected = commands[arm]
                selected_identity = original_identity = command_identity(selected, root)
                binding = None
            else:
                reset_home(active_home)
                prefixes = {"oracle": commands["python"], "candidate": commands["rust"]}
                if fixture_url is not None:
                    env = fixture_env(active_home, prefixes, fixture_url)
                    env.update(case["environment_deltas"])
                    for relative, encoded in case.get("pre_files_b64", {}).items():
                        target = file_path(active_home, relative)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(base64.b64decode(encoded, validate=True))
                else:
                    env = isolated_env(active_home)
                    env.update({"XDG_DATA_HOME": str(active_home / "data"), "PSEUDOLIFE_MCP_NO_SPAWN": "1",
                                "PYTHONDONTWRITEBYTECODE": "1"})
                    if doorbell:
                        if case != expected_case:
                            raise ValueError("doorbell recorded case changed during measurement")
                        env["PSEUDOLIFE_DIGEST_DIR"] = str(home / "digests")
                        for relative, encoded in case["pre_files_b64"].items():
                            path = file_path(home, relative)
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(base64.b64decode(encoded, validate=True))
                try:
                    prepared_case = copy.deepcopy(case) if fixture_url is not None else case
                    selected, selected_identity, original_identity = prepared_command(
                        prepared_case, commands[arm], prefixes, root=root, home=active_home, env=env, prepare=prepare)
                    if fixture_url is not None and prepared_case != case:
                        raise ValueError("CLI preparation must retain the recorded inputs")
                    binding = {"environment": copy.deepcopy(env), "pre_files_b64": snapshot(active_home),
                               "execution": {"original_prefix": commands[arm], "selected_prefix": selected,
                                             "effective_argv": [*selected, *argv], "cwd": str(root),
                                             "command_identity": selected_identity}}
                    if doorbell:
                        if case != expected_case or selected != commands[arm] \
                                or binding["pre_files_b64"] != expected_case["pre_files_b64"]:
                            raise ValueError("doorbell preparation changed recorded case, prefix or pending bytes")
                        binding["private_metadata"] = {"before": doorbell_private_metadata(home)}
                        home_identity = (home.stat().st_dev, home.stat().st_ino)
                except BaseException:
                    remove_root_link(active_home)
                    raise
            if doorbell and reuse:
                home_identity = (home.stat().st_dev, home.stat().st_ino)
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
                arguments = [argument.format_map({"home": str(active_home)}) for argument in argv] if wait_delivery else argv
                wall_started = time.time() if wait_delivery else None
                started = time.perf_counter() if timed else None
                response = run_cli(selected, arguments, cwd=root, env=env,
                                   timeout=case.get("timeout_seconds", 10) if fixture_url is not None else 10,
                                   **input_options)
                elapsed = (time.perf_counter() - started) * 1000 if timed else None
                wall_finished = time.time() if wait_delivery else None
                after = snapshot(active_home)
                compared_after = after
                if wait_delivery:
                    raw_invocations.append({"arm": arm, "label": label, "timed": timed,
                                            "argv": arguments, "environment": copy.deepcopy(env),
                                            "pre_files_b64": before, "response": copy.deepcopy(response),
                                            "post_files_b64": after, "wall_window": [wall_started, wall_finished]})
                    if record_invocation is not None:
                        record_invocation(copy.deepcopy(raw_invocations[-1]))
                    response, policy = project_invocation(case, response, before, after,
                                                         [wall_started, wall_finished],
                                                         "windows" if sys.platform == "win32" else "linux")
                    raw_invocations[-1]["policy_instance"] = policy
                    compared_after = response["post_files_b64"]
                if reuse and file_identity(selected) != identities[arm]:
                    raise RuntimeError("CLI warmed executable file identity changed")
                if command_identity(selected, root) != selected_identity \
                        or command_identity(commands[arm], root) != original_identity:
                    raise RuntimeError("CLI executable changed during measurement capture")
                state = {"environment": copy.deepcopy(env), "pre_files_b64": before, "post_files_b64": compared_after}
                if reuse and state != state_controls[arm]:
                    raise RuntimeError("CLI files or environment changed during measurement")
                if control:
                    state_controls[arm] = state
                if binding is not None:
                    if env != binding["environment"]:
                        raise RuntimeError("CLI effective environment changed during measurement")
                    binding["post_files_b64"] = compared_after
                    if episode_fixture:
                        state = verify(copy.deepcopy(case), active_home, env, list(selected), copy.deepcopy(prefixes),
                                       copy.deepcopy(response))
                        state_checks.append({"arm": arm, "label": label, "timed": timed,
                                             "readback": copy.deepcopy(state)})
                        if env != binding["environment"] or snapshot(active_home) != binding["post_files_b64"]:
                            raise RuntimeError("CLI state verification changed the captured home or environment")
                        if response != {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""} \
                                or binding["post_files_b64"] != binding["pre_files_b64"]:
                            raise RuntimeError("CLI episode-end requires the accepted empty streams and unchanged home")
                    if doorbell:
                        if case != expected_case or env != binding["environment"] \
                                or (home.stat().st_dev, home.stat().st_ino) != home_identity:
                            raise RuntimeError("doorbell case, home or environment changed during measurement")
                        binding["private_metadata"]["after"] = doorbell_private_metadata(home)
                        try:
                            assert_effect(original_case, {**response, "post_files_b64": binding["post_files_b64"]},
                                          key, expected_case["pre_files_b64"])
                        except AssertionError as error:
                            raise RuntimeError("doorbell positive receipt byte control failed") from error
                    if command_identity(selected, root) != selected_identity \
                            or command_identity(commands[arm], root) != original_identity:
                        raise RuntimeError("CLI executable changed during measurement capture")
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
        if fixture_url is not None and mode == "briefing" and (not base64.b64decode(controls["python"]["stdout_b64"], validate=True)
                                        or controls["python"]["stderr_b64"]):
            raise RuntimeError("CLI briefing control must have nonempty stdout and empty stderr")

        if fixture_url is not None and mode in {"lease-check", "lease-list"}:
            if mode == "lease-check":
                expected = {"name": "sample", "held": False,
                            "local": {"file": "lease-sample.lock", "state": None},
                            "board": {"available": True, "reason": None, "holder": None,
                                      "expected_end": None, "stale": False, "queued": 0, "queue": []}}
            else:
                home = Path(prepared_controls["python"]["environment"]["HOME"])
                expected = {"board": {"url": fixture_url, "available": True, "reason": None,
                                      "truncated": False, "leases": []},
                            "lock_dir": str(home / ".pseudolife-mcp/locks"),
                            "local": [], "test_suite_lock": None}
            newline = "\r\n" if sys.platform == "win32" else "\n"
            expected_bytes = (newline.join(json.dumps(expected, indent=2).split("\n")) + newline).encode("utf-8")
            if controls["python"]["stderr_b64"] or base64.b64decode(
                    controls["python"]["stdout_b64"], validate=True) != expected_bytes:
                raise RuntimeError("CLI lease control must observe the available empty owned board")
            if any(binding["pre_files_b64"] != binding["post_files_b64"] for binding in prepared_controls.values()):
                raise RuntimeError("CLI lease control must preserve the recorded home bytes")


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
    if doorbell and doorbell_source_binding(root) != doorbell_binding:
        raise RuntimeError("doorbell preparation source changed during measurement capture")
    return {"schema": 1, "status": "contaminated-plumbing-smoke" if args.smoke else "quiet-cli-pair-final",
            "capture_platform": capture_platform(), "provenance": measured_provenance,
            "cli_instrument_binding": instrument_binding,
            "python_oracle": pin, "candidate_identity": identity,
            "candidate_executable": binary, "python_executable": python_identity,
            "mode": mode, "argv": argv, "stream_contract": STREAM_CONTRACT, "capture_runtime": runtime,
            "prepared_case": copy.deepcopy(case), "prepared_controls": prepared_controls,
            **({"doorbell_preparation_binding": doorbell_binding} if doorbell else {}),
            **({"fixture_url": fixture_url} if fixture_url is not None else {}),
            **({"independent_state_checks": state_checks,
                "database_byte_equality_claimed": False} if episode_fixture else {}),
            "resource_check": resource,
            "repeat_resource_checks": resource_checks, "repeats": args.repeats,
            "samples_per_repeat": args.samples, "runs": runs,
            "warmups": warmups, "state_controls": state_controls,
            **({"fixture_url": fixture_url, "raw_invocations": raw_invocations,
                "named_policy": "nondeterministic-bytes-semantic",
                "delivery_scope": "Exact retained seeded coordination records in an owned daemon fixture"}
               if wait_delivery else {}),
            "start_protocol": "One untimed start per arm before each repeat; timed starts reuse unchanged images and identical restored input state."
                              if warm_images else "Historical per-invocation fixture reset; no explicit image warmup.",
            "metrics": {arm: metric_cells(rows, ("cold_start_to_exit_ms", "executable_bytes"))
                        for arm, rows in runs.items()},
            "byte_control": controls,
            "limitations": ["Fresh public CLI process delivering recorded mail in an owned daemon fixture; no model execution."
                             if wait_delivery else "Fresh public CLI process against an already running owned daemon; image warmup follows start_protocol."
                             if fixture_url is not None else "Fresh public CLI process after one untimed start of each unchanged executable per repeat; no daemon, database or models."
                             if warm_images else "Fresh public CLI process with warm OS filesystem cache; no daemon, database or models.",
                            "UTF-8 stdout/stderr and valid Unicode scalar argv only; Windows CRLF is preserved.",
                            "Locale/default and other output encodings, non-UTF-8 or surrogate argv remain deferred.",
                            "Timing includes owned-process setup, complete output collection and clean exit; preparation/reset/file checks are untimed.",
                            "The historical cold_start_to_exit_ms key measures fresh processes; image warmup is recorded separately in start_protocol.",
                            *(["Each seeded empty root must close independently; generated IDs/times are retained, not compared across arms."]
                              if episode_fixture else []),
                            "CLI exit timing is not comparable to shim first-frame or initialize-return timing.",
                            "Executable size excludes interpreter dependencies; noise floors are descriptive repeat-block ranges.",
                            "Smoke is plumbing evidence only." if args.smoke else "Final samples require the caller's quiet CPU window."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--mode", choices=("help", "version", "doorbell-prompt-seen"), default="help")
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
