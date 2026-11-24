"""Late clock reseed recovery through the native service.

All loaders finish before a malformed coordination clock fails. Correcting
that clock and dropping entries.text proves the next initialization retries
only the clock; a later malformed clock proves healthy calls skip its read.
The native embedder loads the existing four-dimensional offline ONNX fixture.
Checking goldens (--check-goldens, what CI runs and the default) starts the
native service and compares its attempts and each phase's database with the
committed golden; --record-candidate re-records the golden from the candidate.
The frozen Python arm that recorded it first is retired.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

_DATABASE_TURN_MARKER = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
import candidate_record
import daemons
import dbstate
import pgdisposable
import startup_cases as startup
import psycopg
from session_fixture import HLC_META_KEY, seed_bank, with_meta
from rust.harness_support.database_guard import assert_disposable_database

pg = pgdisposable.Slice.from_env("pgs")
PHASES = ("initial", "clock-corrected", "column-dropped", "retry", "healthy-clock-broken", "healthy")
GOLDEN = HERE / "goldens" / "clock-retry.json"
CONFIG = """embedding:
  model_name: fixture-clock-retry
  backend: onnx
  device: cpu
  cpu_dtype: fp32
memory:
  embedding_dim: 4
  miras:
    preset: flat
"""


def dump_phase(phase):
    dsn = os.environ["PL_PGS_CLOCK_RETRY_DSN"]
    name = psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]
    pg._check(name)
    if not re.fullmatch(r"[0-9]+_[0-9a-f]{16}", name[len(pg.PREFIX):]):
        raise ValueError("clock fixture database must have a numeric owned name")
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert_disposable_database(conn)
    output = Path(os.environ["PL_PGS_CLOCK_DB_OUT"])
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{phase}.json").write_text(json.dumps(dbstate.dump(dsn), allow_nan=False), encoding="utf-8")


def normalized(result):
    result = json.loads(json.dumps(result))
    for attempt in result["attempts"]:
        if not attempt["ok"] and attempt.get("error") == "invalid coordination clock high-water mark":
            attempt["error"] = "coordination-highwater"
    return result


def compare_attempts(python, rust):
    python, rust = normalized(python), normalized(rust)
    differences = dbstate.diff({key: value for key, value in python.items() if key != "attempts"},
                               {key: value for key, value in rust.items() if key != "attempts"})
    if len(python["attempts"]) != len(rust["attempts"]):
        return differences + dbstate.diff(python["attempts"], rust["attempts"], "/attempts")
    return differences + [difference for index, (left, right) in enumerate(zip(python["attempts"], rust["attempts"]))
                          for difference in dbstate.diff(left, right, f"/attempts/{index}")]


def runtime_library():
    import onnxruntime
    root = Path(onnxruntime.__file__).parent / "capi"
    if os.name == "nt":
        return root / "onnxruntime.dll"
    return next(root.glob("libonnxruntime.so.*"))


def invoke(binary, kind, dsn, home, model, phases, output, mutant=None):
    env = daemons.base_env(home, {
        "PL_HARNESS_SLICE": "pgs", "PL_PGS_CLOCK_RETRY_DSN": dsn,
        "PL_PGS_CLOCK_CONFIG": str(home / "data" / "config.yaml"),
        "PL_PGS_CLOCK_OUT": str(output), "PL_PGS_CLOCK_PYTHON": sys.executable,
        "PL_PGS_CLOCK_DB_DUMP_SCRIPT": str(Path(__file__).resolve()),
        "PL_PGS_CLOCK_DB_OUT": str(phases), "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "PSEUDOLIFE_DAEMON_ONNX_DIR": str(model), "ORT_DYLIB_PATH": str(runtime_library()),
    })
    for key in list(env):
        if key.startswith("PL_") and key != "PL_HARNESS_SLICE" and not key.startswith("PL_PGS_CLOCK_"):
            env.pop(key)
    command = [str(binary), "--exact", "service::tests::db_clock_reseed_retry", "--nocapture"]
    if mutant:
        env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    run = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise RuntimeError(f"{kind} clock retry cell exit {run.returncode}: "
                           f"{run.stdout[-1800:]} {run.stderr[-1800:]}")
    if kind == "rust" and "1 passed" not in run.stdout:
        raise RuntimeError("native clock retry probe did not execute one passing test")
    return json.loads(output.read_text(encoding="utf-8"))


def expectations(result):
    differences = []
    attempts = result.get("attempts", [])
    if len(attempts) != 3:
        return ["clock retry must expose exactly three initialization attempts"]
    for index, attempt in enumerate(attempts):
        if attempt["ok"] != (index != 0):
            differences.append(f"/attempts/{index}/ok: expected {index != 0}")
        snapshot = attempt.get("retained") if index == 0 else attempt
        if not isinstance(snapshot, dict) or "startup" not in snapshot or "entries" not in snapshot:
            differences.append(f"/attempts/{index}: complete resident snapshot missing")
            continue
        differences.extend(dbstate.diff([], snapshot["entries"], f"/attempts/{index}/entries"))
        differences.extend(dbstate.diff(index == 0, snapshot["startup"]["hlc_reseed_pending"],
                                        f"/attempts/{index}/hlc_reseed_pending"))
        differences.extend(dbstate.diff([0, 0] if index == 0 else [20000, 2],
                                        snapshot["startup"]["hlc_highwater"], f"/attempts/{index}/hlc_highwater"))
        if index:
            differences.extend(dbstate.diff(dict.fromkeys(("bank", "startup", "storage", "embedder"), True),
                                            attempt.get("same_as_first"), f"/attempts/{index}/same_as_first"))
    return differences


def cell(binary, number, result, mutant=None, expected=None):
    """The cell on a bank cloned from the seeded template: the native service
    runs, and ``expected`` (the committed golden, None when recording from the
    candidate) is its expectation."""
    arms = ("rust",)
    names = [pg.name(f"{pg.PREFIX}{number + index}") for index in range(1 + len(arms))]
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix="pl-pgs-clock-retry-") as scratch:
            root = Path(scratch)
            from embedding_fixture import create
            model = root / "model"
            create(model)
            homes = [daemons.make_home(root, name, CONFIG) for name in arms]
            template = pg.create(names[0])
            created.append(names[0])
            seed_bank(template, highwater="malformed")
            before = dbstate.dump(template)
            dsns = []
            for name in names[1:]:
                dsns.append(pg.create(name, template=names[0]))
                created.append(name)
            outputs, states = [], []
            for kind, dsn, home in zip(arms, dsns, homes):
                phases = root / f"{kind}-db"
                outputs.append(invoke(binary, kind, dsn, home, model, phases, root / f"{kind}.json", mutant=mutant))
                states.append({phase: json.loads((phases / f"{phase}.json").read_text()) for phase in PHASES})
            result["snapshots"] = dict(zip(arms, outputs))
            result["expectation_diffs"] = [difference for output in outputs for difference in expectations(output)]
            rules = {("relations", "created_at"): "clock"}
            result["database"] = {kind: {phase: startup.start_delta(before, state[phase], rules) for phase in PHASES}
                                  for kind, state in zip(arms, states)}
            if expected is not None:
                # The golden is the left side, which dbstate.diff labels "python".
                result["golden_diffs"] = compare_attempts(expected["oracle"], outputs[-1])
                result["db_golden_diffs"] = {phase: dbstate.diff(expected["database"][phase],
                                                                 result["database"]["rust"][phase])
                                             for phase in PHASES}
            result["write_diffs"] = []
            result["epoch_diffs"] = []
            for arm, state in enumerate(states):
                if startup.epoch(state["initial"]) != startup.epoch(before) + 1:
                    result["epoch_diffs"].append(f"arm {arm}: constructor epoch must increment once")
                result["write_diffs"] += dbstate.diff(startup.without_epoch(before), startup.without_epoch(state["initial"]))
                result["write_diffs"] += dbstate.diff(with_meta(state["initial"], **{HLC_META_KEY: [20000, 2]}),
                                                     state["clock-corrected"])
                result["write_diffs"] += dbstate.diff(state["column-dropped"], state["retry"])
                result["write_diffs"] += dbstate.diff(with_meta(state["retry"], **{HLC_META_KEY: "malformed"}),
                                                     state["healthy-clock-broken"])
                result["write_diffs"] += dbstate.diff(state["healthy-clock-broken"], state["healthy"])
            result["db_phases"] = dict(zip(arms, states))
    finally:
        for name in reversed(created):
            try:
                pg.drop(name)
            except Exception as error:
                result.setdefault("cleanup_errors", []).append(f"{name}: {type(error).__name__}")


def failed(result):
    return bool(result.get("error") or result.get("cleanup_errors") or result.get("snapshot_diffs")
                or result.get("expectation_diffs") or result.get("write_diffs") or result.get("epoch_diffs")
                or any(result.get("db_diffs", {}).values()) or result.get("golden_diffs")
                or any(result.get("db_golden_diffs", {}).values()))


def main(argv=None):
    # No abbreviations: a shortened flag must not select a recording mode.
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--rust-test-bin", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--mutants", action="store_true")
    # Outside the group, so its retirement message wins over "not allowed with".
    parser.add_argument("--record-goldens", action="store_true", help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-goldens", action="store_true")
    mode.add_argument("--record-candidate", action="store_true",
                      help="re-record the golden from the Rust candidate (candidate_record.py); a dry run "
                           "unless --write")
    parser.add_argument("--write", action="store_true", help="with --record-candidate: write the changed golden")
    parser.add_argument("--dump-db", choices=PHASES, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # Retired modes refuse first: before the database turn and any check.
    if args.record_goldens:
        parser.error(candidate_record.RECORD_GOLDENS_RETIRED)
    if _DATABASE_TURN_MARKER.exists():
        _turn_sys = __import__("sys")
        _turn_root = str(__import__("pathlib").Path(__file__).resolve().parents[3])
        _turn_sys.path.insert(0, _turn_root)
        try:
            from rust.harness_support.database_turn import require_turn

            require_turn(holder="standalone database harness", contain=True)
        finally:
            _turn_sys.path.remove(_turn_root)
    if pg.SLICE != "pgs":
        parser.error("clock retry cells require PL_HARNESS_SLICE=pgs")
    if args.dump_db:
        dump_phase(args.dump_db)
        return 0
    if args.rust_test_bin is None or args.out is None:
        parser.error("--rust-test-bin and --out are required")
    if args.write and not args.record_candidate:
        parser.error("--write needs --record-candidate")
    if args.record_candidate and args.mutants:
        parser.error("--record-candidate records the cell only; check the result with --check-goldens --mutants")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.record_candidate:
        return record_candidate(args)
    result = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip()}
    number = time.time_ns()
    expected = json.loads(GOLDEN.read_text())
    if "database" not in expected:
        parser.error("clock-retry.json has no database section; "
                     + candidate_record.startup_recording("clock_retry_cases.py"))
    try:
        cell(args.rust_test_bin.resolve(), number, result, expected=expected)
    except Exception as error:
        result["error"] = str(error)
    if args.mutants and not failed(result):
        mutant = {}
        try:
            cell(args.rust_test_bin.resolve(), number + 1000, mutant, "startup-clock-reloads",
                 expected=expected)
        except Exception as error:
            mutant["error"] = str(error)
        # The mutant shows as a difference from the committed attempts; a
        # database difference means the cell itself broke.
        infrastructure_failed = bool(mutant.get("error") or mutant.get("cleanup_errors")
                                     or mutant.get("epoch_diffs") or mutant.get("write_diffs")
                                     or any(mutant.get("db_diffs", {}).values())
                                     or any(mutant.get("db_golden_diffs", {}).values()))
        native_attempts = mutant.get("snapshots", {}).get("rust", {}).get("attempts", [])
        infrastructure_failed |= len(native_attempts) != 3 or (
            normalized({"attempts": native_attempts})["attempts"][0].get("error") != "coordination-highwater"
            if native_attempts else True)
        differences = mutant.get("golden_diffs", [])
        caught = not infrastructure_failed and any(difference.startswith("/attempts/1/ok:")
                                                   for difference in differences)
        result["mutants"] = {"startup-clock-reloads": {"caught": caught,
            "snapshot_diffs": differences,
            "error": mutant.get("error"), "infrastructure_failed": infrastructure_failed}}
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    bad = failed(result) or any(not value["caught"] for value in result.get("mutants", {}).values())
    print(f"clock retry: {'FAIL' if bad else '0 diffs'}", flush=True)
    return int(bad)


def record_candidate(args):
    """--record-candidate: the cell runs on the candidate alone and is
    classed against clock-retry.json (candidate_record.py)."""
    why = candidate_record.refuse_mutant_environment()
    if why:
        print(f"clock retry record-candidate refused: {why}", flush=True)
        return 1
    binary = args.rust_test_bin.resolve()
    recording = candidate_record.Recording(GOLDEN, ("oracle", "database"), binary, whole=False,
                                           defined=["clock-retry"], single=True)
    recording.write = args.write
    result = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip()}
    try:
        cell(binary, time.time_ns(), result)
    except Exception as error:
        result["error"] = str(error)
    failures = ([f"error: {result['error']}"] if result.get("error") else []) + [
        f"cleanup: {e}" for e in result.get("cleanup_errors", [])] + candidate_record.stage_failures(result)
    snapshot = result.get("snapshots", {}).get("rust")
    observed = ({"oracle": normalized(snapshot), "database": result["database"]["rust"]}
                if snapshot is not None and "database" in result else None)
    recording.observe("clock-retry", observed, failures, candidate_record.round_trip)
    code = recording.finish(args.write, "python rust/daemon/harness/startup_ci.py --out <scratch>/startup.json")
    result["record_candidate"] = recording.report
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
