"""Paired public CLI processes with one guarded bank and exact home-file bytes."""
from __future__ import annotations

import argparse
import base64
import copy
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import tomllib

from .cli_corpus import corpus
from .cli_public import public_summary
from .harness import Policy, capture_platform, compare, isolated_env, run_cli, write_new
from .phase1_receipts import candidate_identity, command_identity
from .provenance import require_import_root, require_instrument_binding, runtime_metadata
from .stdio_capture import require_phase1_source

STATE_POLICY = "cli-state-compared"
BYTE_POLICY = Policy(source_text_paths=(), ignored_values=())


def cli_binding(root, oracle_root, *, extra_helpers=None):
    from . import cli_version, harness, processes, stdio_daemon
    from evals import memory_policy_bench, memory_policy_daemon
    from evals.rust_baseline import daemon, daemon_child
    from evals.rust_baseline.common import child_environment, lease_gate
    from pseudolife_memory.storage import schema
    from tests.fake_embedder import FakeSentenceTransformer
    from tests.pg_defaults import default_admin_url
    helpers = {"evals/rust_port/cli_process.py": [Path(__file__), prepared_command, reset_home, snapshot, remove_root_link],
               "evals/rust_port/cli_corpus.py": [corpus],
               "evals/rust_port/cli_public.py": [public_summary],
               "evals/rust_port/cli_version.py": [cli_version.make_prepare, cli_version.seed_context, cli_version.cases],
               "evals/rust_port/harness.py": [capture_platform, compare, isolated_env, run_cli, write_new],
               "evals/rust_port/phase1_receipts.py": [candidate_identity, command_identity],
               "evals/rust_port/provenance.py": [require_import_root, runtime_metadata, require_instrument_binding],
               "evals/rust_port/stdio_capture.py": [require_phase1_source],
               "evals/rust_port/processes.py": [harness.owned_process, processes.execution_sources],
               "evals/rust_baseline/common.py": [lease_gate, child_environment, daemon.child_environment],
               "evals/memory_policy_bench.py": [memory_policy_bench.scrubbed_env, memory_policy_bench.production_database],
               "evals/memory_policy_daemon.py": [memory_policy_daemon.check_database, memory_policy_daemon.check_port,
                    memory_policy_daemon._server_check, daemon_child.check_database,
                    daemon_child.check_port, daemon_child._server_check],
               "evals/rust_baseline/daemon_child.py": [daemon_child.main, daemon_child.install_readiness_identity],
               "evals/rust_port/stdio_daemon.py": [stdio_daemon.main]}
    for name, loaded in (extra_helpers or {}).items():
        helpers.setdefault(name, []).extend(loaded)
    owners = processes.execution_sources()[1:]
    try:
        production = {path.resolve().relative_to(Path(oracle_root).resolve()).as_posix(): [path] for path in owners}
    except ValueError as error:
        raise RuntimeError("CLI ownership helper loaded from another production tree") from error
    production.update({"pseudolife_memory/storage/schema.py": [schema.dsn_database_name,
                          schema.refuse_production_database, schema.assert_disposable_database],
                       "tests/pg_defaults.py": [default_admin_url],
                       "tests/fake_embedder.py": [FakeSentenceTransformer]})
    return {"instrument": require_instrument_binding(root, helpers),
            "production_ownership": require_instrument_binding(oracle_root, production) if production else None}


def is_link(metadata):
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def checked_metadata(path):
    metadata = path.lstat()
    if is_link(metadata):
        raise ValueError("CLI home contains a link or junction")
    return metadata


def remove_root_link(home):
    """Remove only a replaced owned entry, never the directory it points at."""
    try:
        metadata = home.lstat()
    except FileNotFoundError:
        return False
    if not is_link(metadata):
        return False
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        home.rmdir()  # Windows directory junction; unlink handles directory symlinks.
    else:
        home.unlink()
    return True


def checked_files(home):
    checked_metadata(home)
    pending = [home]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            paths = sorted((Path(entry.path) for entry in entries))
        for path in paths:
            metadata = checked_metadata(path)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                yield path


def file_path(home, relative):
    path = Path(relative)
    if not relative or path.anchor or ".." in path.parts or "\\" in relative:
        raise ValueError("fixture file requires a relative POSIX path")
    target = home / path
    checked_metadata(home)
    current = home
    for component in path.parts:
        current /= component
        if os.path.lexists(current):
            checked_metadata(current)
    return target


def snapshot(home):
    """Keep presence and every byte; reject links rather than follow host state."""
    result = {}
    for path in checked_files(home):
        result[path.relative_to(home).as_posix()] = base64.b64encode(path.read_bytes()).decode("ascii")
    return result


def fixture_env(home, commands, url):
    if os.path.lexists(home):
        checked_metadata(home)
    env = isolated_env(home)
    env.update({"XDG_DATA_HOME": str(home / "data"),
                "XDG_CACHE_HOME": str(home / "cache"),
                "XDG_STATE_HOME": str(home / "state"),
                "XDG_RUNTIME_DIR": str(home / "run"),
                "PSEUDOLIFE_DIGEST_DIR": str(home / ".pseudolife-mcp/digests"),
                "PSEUDOLIFE_LEASE_LOCK_DIR": str(home / ".pseudolife-mcp/locks"),
                "PSEUDOLIFE_SUITE_LOCK_DIR": str(home / ".pseudolife-mcp/locks"),
                "PSEUDOLIFE_AGENT_STATE_DIR": str(home / ".pseudolife-mcp/agent-state"),
                "PSEUDOLIFE_MCP_TOKEN_FILE": str(home / ".pseudolife-mcp/token"),
                "PSEUDOLIFE_MCP_DAEMON_URL": url,
                "PSEUDOLIFE_MCP_NO_SPAWN": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    # Absolute argv prefixes avoid an inherited developer PATH. Helpers needed by
    # later modes are added explicitly through the case's path directories.
    env["PATH"] = os.pathsep.join(dict.fromkeys(str(Path(command[0]).resolve().parent)
                                              for command in commands.values()))
    for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                 "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "PSEUDOLIFE_DIGEST_DIR",
                 "PSEUDOLIFE_LEASE_LOCK_DIR", "PSEUDOLIFE_AGENT_STATE_DIR"):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    base = Path(env["LOCALAPPDATA"] if os.name == "nt" else env["XDG_DATA_HOME"]) / "pseudolife-mcp"
    (base / "runtimes").mkdir(parents=True)
    (base / "bin").mkdir()
    if os.name != "nt":
        Path(env["XDG_RUNTIME_DIR"]).chmod(0o700)
    from evals.rust_baseline.transport import TOKEN
    token = Path(env["PSEUDOLIFE_MCP_TOKEN_FILE"])
    token.write_bytes(TOKEN.encode("ascii"))
    token.chmod(0o600)
    return env


def cases(modes=("help",)):
    result = []
    if "lease" in modes:
        from .lease_corpus import cases as lease_cases
        result.extend(lease_cases())
    if "help" in modes:
        for case in corpus()["cases"]:
            result.append({"id": case["id"], "mode": "help" if case["id"].startswith("help-")
                           else "unknown-dispatch", "argv": case["request"]["argv"],
                           "stdin_b64": "", "environment_deltas": {}, "pre_files_b64": {},
                           "normalizations": []})
    if "version" in modes:
        from .cli_version import cases as version_cases
        result.extend(version_cases())
    return result


def reset_home(home):
    """Reset the shared owned path after checking every entry before removal."""
    if remove_root_link(home):
        raise ValueError("CLI home contains a link or junction")
    if home.exists():
        # Validate entries before cleanup as well as before reading state.
        list(checked_files(home))
        shutil.rmtree(home)


def prepared_command(case, command, commands, *, root, home, env, prepare):
    """Bind relocated prefixes to the original arm and preserve owned policies."""
    original = list(command)
    original_identity = command_identity(original, root)
    original_path = Path(original[0])
    if not original_path.is_absolute():
        original_path = Path(shutil.which(original[0]) or root / original[0])
    original_path = Path(os.path.abspath(original_path))
    original_resolved = original_path.resolve(strict=True)
    before = dict(env)
    allowed = {"PYTHONPATH", "PSEUDOLIFE_SHIM_RUNTIMES", "PSEUDOLIFE_SHIM_LAUNCHER"} \
        if case["mode"] == "version" else set()
    selected = original if prepare is None else prepare(case, home, env, list(original), copy.deepcopy(commands))
    if any(not isinstance(name, str) or not isinstance(value, str) for name, value in env.items()) \
            or {name: value for name, value in env.items() if name not in allowed} != \
            {name: value for name, value in before.items() if name not in allowed}:
        raise ValueError("CLI preparation must retain owned daemon, isolation and CPU policies")
    if len({name.upper() for name in env}) != len(env) or any(
            name.upper() in allowed and name not in allowed for name in env):
        raise ValueError("CLI preparation cannot introduce environment case aliases")
    for name, value in env.items():
        if name.upper().endswith(("_DIR", "_FILE", "_HOME")) or name in {
                "PSEUDOLIFE_MCP_CONFIG", "PSEUDOLIFE_SHIM_RUNTIMES", "PSEUDOLIFE_SHIM_LAUNCHER"}:
            if not Path(value).is_absolute() or not Path(value).resolve().is_relative_to(home.resolve()):
                raise ValueError("CLI prepared file and directory sources must remain inside the home")
    if "PYTHONPATH" in env:
        from .cli_version import seed_context
        if case["mode"] != "version" or env["PYTHONPATH"] != seed_context(root)["pythonpath"]:
            raise ValueError("CLI preparation requires the verified oracle Python path")
    if not isinstance(selected, list) or not selected or not all(isinstance(arg, str) and arg for arg in selected):
        raise ValueError("prepared command requires a nonempty string prefix")
    invoked = Path(selected[0])
    if not invoked.is_absolute():
        invoked = Path(shutil.which(selected[0], path=env["PATH"]) or root / selected[0])
    invoked = Path(os.path.abspath(invoked))
    resolved = invoked.resolve(strict=True)
    # Invocation spelling selects a virtualenv even when its executable is a
    # symlink to the base interpreter. Resolve only for identity and ownership.
    selected = [str(invoked), *selected[1:]]
    identity = command_identity(selected, root)
    if selected[1:] != original[1:] or any(identity[key] != original_identity[key]
            for key in ("executable_sha256", "executable_bytes")):
        raise ValueError("prepared command must retain the original arm prefix and executable bytes")
    if original_path.resolve(strict=True) != original_resolved:
        raise ValueError("CLI preparation changed the original executable target")
    if invoked != original_path:
        checked_metadata(home)
        canonical_home = home.resolve(strict=True)
        if not resolved.is_relative_to(canonical_home):
            raise ValueError("CLI relocated executable must remain inside the home")
        checked_metadata(invoked)
        # Short names can spell the same directory; links cannot supply ownership.
        for parent in invoked.parents:
            checked_metadata(parent)
            if parent.resolve(strict=True) == canonical_home:
                break
        else:
            raise ValueError("CLI relocated executable must remain inside the home")
    return selected, identity, original_identity


def observe(case, command, commands, *, root, home, url, prepare=None, process_scope=None):
    # Both arms occupy the same disposable path, reset before each launch. Path
    # text remains contractual; no broad home/path replacement is permitted.
    reset_home(home)
    if "home_suffix" in case:
        suffix = Path(case["home_suffix"])
        if len(suffix.parts) != 1 or suffix.anchor or suffix.name in {".", ".."} or "\\" in str(suffix):
            raise ValueError("CLI home suffix must be one relative path component")
        home = home / suffix
    env = fixture_env(home, commands, url)
    if case.get("normalizations"):
        raise ValueError("no cli-state normalizations are authorized in this corpus yet")
    values = {"home": str(home), "url": url, "port": url.rsplit(":", 1)[1]}
    deltas = case.get("environment_deltas", {})
    protected = {"HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "CUDA_VISIBLE_DEVICES",
                 "OMP_NUM_THREADS", "MKL_NUM_THREADS", "PYTHONIOENCODING", "PATH", "CODEX_HOME",
                 "PYTHONPATH", "PYTHONHOME",
                 "TMP", "TEMP", "TMPDIR", "HF_HOME", "TORCH_HOME", "TORCHINDUCTOR_CACHE_DIR",
                 "PSEUDOLIFE_MCP_DAEMON_URL", "PSEUDOLIFE_MCP_NO_SPAWN"}
    if protected & {key.upper() for key in deltas} or any(key.upper().startswith("XDG_") for key in deltas):
        raise ValueError("case cannot override isolation, encoding or CPU policy")
    if any("DATABASE_URL" in key.upper() for key in deltas):
        raise ValueError("CLI cases use only the shared disposable daemon, never a database URL")
    for key, value in deltas.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value.format_map(values)
            if key.upper().endswith(("_DIR", "_FILE", "_HOME")) and not Path(env[key]).resolve().is_relative_to(home.resolve()):
                raise ValueError("case file and directory environment overrides must remain inside the home")
    from .harness import _base_url
    _base_url(url)
    admitted_env = copy.deepcopy(env)

    def admit_environment():
        for key, value in (("PSEUDOLIFE_MCP_DAEMON_URL", url), ("PSEUDOLIFE_MCP_NO_SPAWN", "1")):
            if {name: cell for name, cell in env.items() if name.upper() == key} != {key: value}:
                raise ValueError("CLI child must retain the owned daemon and no-spawn policy")
        if len({key.upper() for key in env}) != len(env):
            raise ValueError("CLI environment contains case aliases")
        if env != admitted_env:
            raise ValueError("CLI effective environment changed after admission")

    admit_environment()
    for relative, encoded in case.get("pre_files_b64", {}).items():
        path = file_path(home, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(encoded, validate=True))
    original = list(command)
    try:
        selected, identity, original_identity = prepared_command(
            case, original, commands, root=root, home=home, env=env, prepare=prepare)
        resolved_executable = Path(selected[0]).resolve(strict=True)
        original_executable = Path(original[0])
        if not original_executable.is_absolute():
            original_executable = Path(shutil.which(original[0]) or root / original[0])
        original_resolved_executable = original_executable.resolve(strict=True)
        for key, value in (("PSEUDOLIFE_MCP_DAEMON_URL", url), ("PSEUDOLIFE_MCP_NO_SPAWN", "1")):
            matching = {name: cell for name, cell in env.items() if name.upper() == key}
            if matching != {key: value}:
                raise ValueError("CLI child must retain the owned daemon and no-spawn policy")
        arguments = [arg.format_map(values) for arg in case["argv"]]
        before = snapshot(home)
        if command_identity(selected, root) != identity or command_identity(original, root) != original_identity \
                or Path(selected[0]).resolve(strict=True) != resolved_executable \
                or original_executable.resolve(strict=True) != original_resolved_executable:
            raise ValueError("CLI command changed before process launch")
        launch_env = copy.deepcopy(env)
        response = run_cli(selected, arguments, cwd=root, env=env,
                           timeout=case.get("timeout_seconds", 10),
                           stdin=base64.b64decode(case.get("stdin_b64", ""), validate=True))
        if "expected_stdout" in case:
            version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
            expected = case["expected_stdout"].format(version=version)
            if os.name == "nt":
                expected = expected.replace("\n", "\r\n")
            if response != {"exit_code": 0, "stdout_b64": base64.b64encode(expected.encode("utf-8")).decode("ascii"),
                            "stderr_b64": ""}:
                raise RuntimeError("CLI response does not match the case's exact expected fallback")
        if env != launch_env:
            raise ValueError("CLI effective environment changed during capture")
        response["post_files_b64"] = snapshot(home)
        if env != launch_env:
            raise ValueError("CLI effective environment changed during poststate collection")
        if command_identity(selected, root) != identity or command_identity(original, root) != original_identity \
                or Path(selected[0]).resolve(strict=True) != resolved_executable \
                or original_executable.resolve(strict=True) != original_resolved_executable:
            raise RuntimeError("CLI executable changed during capture")
        return {"request": copy.deepcopy(case), "environment": launch_env,
                "pre_files_b64": before, "response": response,
                "execution": {"original_prefix": original, "selected_prefix": selected,
                              "invoked_executable": selected[0], "resolved_executable": str(resolved_executable),
                              "effective_argv": [*selected, *arguments], "cwd": str(root),
                              "command_identity": identity}}
    finally:
        remove_root_link(home)


def byte_payload(observation):
    # Different arm executables are expected; their provenance is retained,
    # while exact equality still covers every declared input and captured byte.
    return {key: value for key, value in observation.items() if key != "execution"}


def candidate_controls(records):
    """Mutate captured candidate observations, retaining the oracle unchanged."""
    result = []
    for record in records:
        expected = record["oracle"]["response"]
        observed = record["candidate"]["response"]
        for field in ("exit_code", "stdout_b64", "stderr_b64", "post_files_b64"):
            mutated = copy.deepcopy(observed)
            if field == "exit_code":
                mutated[field] = (observed[field] + 1) % 256
                if mutated[field] == expected[field]:
                    mutated[field] = (mutated[field] + 1) % 256
            elif field == "post_files_b64":
                path = ".pseudolife-mcp/token"
                mutated[field][path] = base64.b64encode(
                    base64.b64decode(observed[field].get(path, "")) + b"\x00").decode("ascii")
            else:
                mutated[field] = base64.b64encode(base64.b64decode(observed[field]) + b"\x00").decode("ascii")
            differences = compare(expected, mutated, BYTE_POLICY)
            field_path = "/" + field
            rejected = any(cell["path"] == field_path or cell["path"].startswith(field_path + "/")
                           for cell in differences)
            result.append({"case": record["id"], "mode": record["mode"], "field": field,
                           "mutation_arm": "candidate", "rejected": rejected,
                           "differences": differences})
    return result


def paired_cases(spec, commands, *, root, home, url, prepare=None, process_scope=None):
    records = []
    if not spec or len({case["id"] for case in spec}) != len(spec):
        raise ValueError("CLI process corpus needs nonempty unique case ids")
    for case in spec:
        arms = {arm: observe(case, command, commands, root=root, home=home, url=url, prepare=prepare,
                             process_scope=process_scope)
                for arm, command in commands.items()}
        differences = compare(byte_payload(arms["oracle"]), byte_payload(arms["candidate"]), BYTE_POLICY)
        records.append({"id": case["id"], "mode": case["mode"], **arms,
                        "differences": differences, "passed": not differences})
    controls = candidate_controls(records)
    return {"records": records, "candidate_output_controls": controls,
            "passed": all(record["passed"] for record in records) and all(c["rejected"] for c in controls)}


def run(root, command, candidate_root, spec, resource, *, prepare=None, process_scope=None):
    from evals.rust_baseline import daemon, transport
    from evals.rust_baseline.daemon import disposable_database, launched_daemon, private_directory
    from .full_bank import private_home_overrides
    source = require_phase1_source(root)
    require_import_root(root)
    runtime = runtime_metadata(root)
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if sys.version_info[:2] != (3, 11) or not runtime["source_origin_matches_selected_root"] \
            or runtime["package_runtime_version"] != version \
            or runtime["distribution_versions"]["pseudolife-mcp"] != version:
        raise RuntimeError("CLI process fixture requires the pinned Python 3.11 runtime")
    commands = {"oracle": [sys.executable, "-m", "pseudolife_memory.cli"], "candidate": command}
    identities = {"oracle": command_identity(commands["oracle"], root),
                  "candidate": candidate_identity(command, candidate_root)}
    if identities["candidate"]["public_cli_module"] or identities["candidate"]["executable_sha256"] == \
            identities["oracle"]["executable_sha256"]:
        raise ValueError("CLI candidate must be a distinct native executable")
    helpers = {"evals/rust_baseline/daemon.py": [disposable_database, launched_daemon, private_directory],
               "evals/rust_baseline/daemon_child.py": [Path(daemon.__file__).with_name("daemon_child.py")],
               "evals/rust_baseline/transport.py": [Path(transport.__file__)],
               "evals/rust_port/full_bank.py": [private_home_overrides]}
    binding = cli_binding(candidate_root, root, extra_helpers=helpers)
    if prepare is None and any(case.get("mode") == "version" for case in spec):
        from .cli_version import make_prepare
        prepare = make_prepare(root, source["oracle_head"])
    with disposable_database() as dsn, private_directory() as private:
        with launched_daemon(dsn, private, source_root=root, env_extra=private_home_overrides(private),
                             child_module="evals.rust_port.stdio_daemon", startup_timeout=90) as (_, url, cleanup):
            result = paired_cases(spec, commands, root=root, home=Path(private) / "cli-home", url=url,
                                  prepare=prepare, process_scope=process_scope)
    cleanup["database_dropped"] = True
    if command_identity(commands["oracle"], root) != identities["oracle"] or \
            candidate_identity(command, candidate_root) != identities["candidate"]:
        raise RuntimeError("CLI executable or candidate source changed during capture")
    require_phase1_source(root)
    if cli_binding(candidate_root, root, extra_helpers=helpers) != binding:
        raise RuntimeError("CLI instrument changed during corpus capture")
    return {"schema": 1, **source, "capture_platform": capture_platform(),
            "capture_runtime": runtime, "resource_check": resource, "command_identities": identities,
            "cli_instrument_binding": binding,
            "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "policy": STATE_POLICY, "normalizations": [], "daemon_cleanup": cleanup, **result,
            "coverage": {"kind": "additive-process-corpus", "modes": sorted({c["mode"] for c in spec})},
            "limitations": ["Default corpus covers help and unknown dispatch; version installer-schema cases are opt-in.",
                            "Other modes require explicit recorded cases; no mode status is implied.",
                            "UTF-8 only; cp1252 is deferred.",
                            "Raw home snapshots and environment deltas are private evidence."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-json", required=True)
    parser.add_argument("--out", type=Path, required=True, help="private exact raw receipt path")
    parser.add_argument("--public-out", type=Path, help="allowlisted summary requiring complete help/version coverage")
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--modes", nargs="+", choices=("help", "version", "lease"), default=["help"])
    clearance = parser.add_mutually_exclusive_group(required=True)
    clearance.add_argument("--board-checked-at")
    clearance.add_argument("--offline-resource-checked-at")
    args = parser.parse_args()
    command = json.loads(args.candidate_json)
    if not isinstance(command, list) or not command or not all(isinstance(p, str) and p for p in command):
        parser.error("candidate command requires a nonempty JSON string array")
    spec = json.loads(args.corpus.read_text(encoding="utf-8")) if args.corpus else cases(args.modes)
    from evals.rust_baseline.common import lease_gate
    resource = lease_gate(args.board_checked_at, offline_resource_checked_at=args.offline_resource_checked_at)
    result = run(args.oracle_root.resolve(), command, args.candidate_root.resolve(), spec, resource)
    write_new(args.out, result)
    if args.public_out:
        write_new(args.public_out, public_summary(result))
    print(json.dumps({"passed": result["passed"], "cases": len(result["records"]), "receipt": args.out.name}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
