"""Late clock reseed recovery through the actual Python and native services.

All loaders finish before a malformed coordination clock fails. Correcting
that clock and dropping entries.text proves the next initialization retries
only the clock; a later malformed clock proves healthy calls skip its read.
Python's unported recovery/curation preamble is explicitly inert in this cell.
The native embedder loads the existing four-dimensional offline ONNX fixture.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
os.environ.setdefault("PL_HARNESS_SLICE", "pgs")
import daemons
import dbstate
import pgdisposable as pg
import startup_cases as startup
import psycopg
from pseudolife_memory.storage.schema import assert_disposable_database

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
NOOP_CALLBACKS = (
    "_rehydrate_if_bank_changed_hands", "_recover_correction_locked",
    "_recover_entry_reinstatement_locked", "_recover_lesson_synthesis",
    "_refresh_source_retired_lessons_locked", "_carry_over_listing_spelling",
)


def dump_phase(phase):
    dsn = os.environ["PL_PGS_CLOCK_RETRY_DSN"]
    name = psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]
    pg._check(name)
    if not name[len(pg.PREFIX):].isdigit():
        raise ValueError("clock fixture database must have a numeric owned name")
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert_disposable_database(conn)
    output = Path(os.environ["PL_PGS_CLOCK_DB_OUT"])
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{phase}.json").write_text(json.dumps(dbstate.dump(dsn), allow_nan=False), encoding="utf-8")


def resident_snapshot(service):
    # The source loaded these exact storage row shapes before clock failure.
    # Reusing that resident capture avoids querying the dropped entries field.
    snapshot = json.loads(json.dumps(service._clock_retry_snapshot))
    snapshot["startup"]["hlc_highwater"] = [service._hlc._phys, service._hlc._logical]
    snapshot["startup"]["hlc_reseed_pending"] = service._hlc_reseed_pending
    return snapshot


def identities(service):
    return (service._cms, (service._cortex, service._world, service._lessons, service._cms.episodes),
            service._storage, service._embedder)


def same_as_first(service, original):
    current = identities(service)
    return {"bank": current[0] is original[0],
            "startup": all(a is b for a, b in zip(current[1], original[1])),
            "storage": current[2] is original[2], "embedder": current[3] is original[3]}


def oracle_cell(seed_only=False):
    from pseudolife_memory.storage.postgres import PostgresStorage
    storage = PostgresStorage(os.environ["PL_PGS_CLOCK_RETRY_DSN"])
    try:
        if seed_only:
            startup.seed(storage, "clock-retry")
            storage.meta_set("coordination_hlc_highwater", "malformed")
            return
        import torch
        from pseudolife_memory.memory.hlc import HybridLogicalClock
        from pseudolife_memory.service import MemoryService
        torch.set_num_threads(1)
        config = Path(os.environ["PL_PGS_CLOCK_CONFIG"])
        service = MemoryService(data_dir=config.parent, config_path=config,
                                database_url=os.environ["PL_PGS_CLOCK_RETRY_DSN"])
        service._storage = storage
        service._embedder = SimpleNamespace(embedding_dim=4)
        service._hlc = HybridLogicalClock(now_ms=lambda: 100)
        for name in NOOP_CALLBACKS:
            setattr(service, name, lambda: None)
        service._hydrate_resident_stores = lambda: startup.python_hydrate(
            storage, config, resident_service=service)
        attempts = []
        with patch("pseudolife_memory.curation_safety.recover_slot_curation", return_value=None):
            try:
                # Actual cold _ensure_init invokes actual _reseed_hlc after
                # resetting retry/backoff and publishing all resident stores.
                service._ensure_init()
            except Exception as error:
                attempts.append({"ok": False, "error": str(error),
                                 "retained": resident_snapshot(service), "same_as_first": None})
            else:
                attempts.append({"ok": True, "error": None,
                                 "retained": resident_snapshot(service), "same_as_first": None})
            original = identities(service)
            dump_phase("initial")
            storage.meta_set("coordination_hlc_highwater", [20000, 2])
            dump_phase("clock-corrected")
            assert_disposable_database(storage.conn)
            storage.conn.execute("ALTER TABLE entries DROP COLUMN text")
            dump_phase("column-dropped")
            for index, phase in ((1, "retry"), (2, "healthy")):
                if index == 2:
                    storage.meta_set("coordination_hlc_highwater", "malformed")
                    dump_phase("healthy-clock-broken")
                try:
                    # Both calls run the real _ensure_init fast path. The
                    # healthy call must not revisit malformed clock metadata.
                    service._ensure_init()
                except Exception as error:
                    attempts.append({"ok": False, "error": str(error),
                                     "retained": resident_snapshot(service), "same_as_first": None})
                else:
                    attempts.append({"ok": True, **resident_snapshot(service),
                                     "same_as_first": same_as_first(service, original)})
                dump_phase(phase)
        Path(os.environ["PL_PGS_CLOCK_OUT"]).write_text(json.dumps({"attempts": attempts}, allow_nan=False),
                                                       encoding="utf-8")
    finally:
        storage.close()


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


def invoke(binary, kind, dsn, home, model, phases, output, seed_only=False, mutant=None):
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
    if kind == "python":
        command = [sys.executable, str(Path(__file__).resolve()), "--oracle-cell"]
        if seed_only:
            command.append("--seed-cell")
    else:
        command = [str(binary), "--exact", "service::tests::db_clock_reseed_retry", "--nocapture"]
        if mutant:
            env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    run = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise RuntimeError(f"{kind} clock retry cell exit {run.returncode}: "
                           f"{run.stdout[-1800:]} {run.stderr[-1800:]}")
    if kind == "rust" and "1 passed" not in run.stdout:
        raise RuntimeError("native clock retry probe did not execute one passing test")
    return None if seed_only else json.loads(output.read_text(encoding="utf-8"))


def meta_value(state, value):
    state = json.loads(json.dumps(state))
    table = state["rows"]["public.meta"]
    key, column = table["columns"].index("key"), table["columns"].index("value")
    row = next(row for row in table["rows"] if row[key] == "coordination_hlc_highwater")
    row[column] = value
    return state


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


def cell(binary, number, result, mutant=None):
    names = [f"{pg.PREFIX}{number + index}" for index in range(3)]
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix="pl-pgs-clock-retry-") as scratch:
            root = Path(scratch)
            from embedding_fixture import create
            model = root / "model"
            create(model)
            homes = [daemons.make_home(root, name, CONFIG) for name in ("seed", "python", "rust")]
            template = pg.create(names[0])
            created.append(names[0])
            invoke(binary, "python", template, homes[0], model, root / "seed-db", root / "seed.json", seed_only=True)
            before = dbstate.dump(template)
            dsns = []
            for name in names[1:]:
                dsns.append(pg.create(name, template=names[0]))
                created.append(name)
            outputs, states = [], []
            for kind, dsn, home in zip(("python", "rust"), dsns, homes[1:]):
                phases = root / f"{kind}-db"
                outputs.append(invoke(binary, kind, dsn, home, model, phases, root / f"{kind}.json", mutant=mutant))
                states.append({phase: json.loads((phases / f"{phase}.json").read_text()) for phase in PHASES})
            result["snapshots"] = dict(zip(("python", "rust"), outputs))
            result["snapshot_diffs"] = compare_attempts(*outputs)
            result["expectation_diffs"] = [difference for output in outputs for difference in expectations(output)]
            result["db_diffs"] = {phase: dbstate.diff(states[0][phase], states[1][phase]) for phase in PHASES}
            result["write_diffs"] = []
            result["epoch_diffs"] = []
            for arm, state in enumerate(states):
                if startup.epoch(state["initial"]) != startup.epoch(before) + 1:
                    result["epoch_diffs"].append(f"arm {arm}: constructor epoch must increment once")
                result["write_diffs"] += dbstate.diff(startup.without_epoch(before), startup.without_epoch(state["initial"]))
                result["write_diffs"] += dbstate.diff(meta_value(state["initial"], [20000, 2]), state["clock-corrected"])
                result["write_diffs"] += dbstate.diff(state["column-dropped"], state["retry"])
                result["write_diffs"] += dbstate.diff(meta_value(state["retry"], "malformed"), state["healthy-clock-broken"])
                result["write_diffs"] += dbstate.diff(state["healthy-clock-broken"], state["healthy"])
            result["db_phases"] = dict(zip(("python", "rust"), states))
    finally:
        for name in reversed(created):
            try:
                pg.drop(name)
            except Exception as error:
                result.setdefault("cleanup_errors", []).append(f"{name}: {type(error).__name__}")


def failed(result):
    return bool(result.get("error") or result.get("cleanup_errors") or result.get("snapshot_diffs")
                or result.get("expectation_diffs") or result.get("write_diffs") or result.get("epoch_diffs")
                or any(result.get("db_diffs", {}).values()) or result.get("golden_diffs"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-test-bin", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--mutants", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record-goldens", action="store_true")
    mode.add_argument("--check-goldens", action="store_true")
    parser.add_argument("--dump-db", choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument("--oracle-cell", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seed-cell", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if pg.SLICE != "pgs":
        parser.error("clock retry cells require PL_HARNESS_SLICE=pgs")
    if args.dump_db:
        dump_phase(args.dump_db)
        return 0
    if args.oracle_cell:
        oracle_cell(args.seed_cell)
        return 0
    if args.rust_test_bin is None or args.out is None:
        parser.error("--rust-test-bin and --out are required")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    result = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
              "declared_noop_callbacks": [*NOOP_CALLBACKS, "curation_safety.recover_slot_curation"]}
    number = time.time_ns()
    try:
        cell(args.rust_test_bin.resolve(), number, result)
    except Exception as error:
        result["error"] = str(error)
    if args.check_goldens and not failed(result):
        expected = json.loads(GOLDEN.read_text())["oracle"]
        result["golden_diffs"] = dbstate.diff(expected, normalized(result["snapshots"]["rust"]))
    if args.mutants and not failed(result):
        mutant = {}
        try:
            cell(args.rust_test_bin.resolve(), number + 1000, mutant, "startup-clock-reloads")
        except Exception as error:
            mutant["error"] = str(error)
        infrastructure_failed = bool(mutant.get("error") or mutant.get("cleanup_errors")
                                     or mutant.get("epoch_diffs") or mutant.get("write_diffs")
                                     or any(mutant.get("db_diffs", {}).values()))
        native_attempts = mutant.get("snapshots", {}).get("rust", {}).get("attempts", [])
        infrastructure_failed |= len(native_attempts) != 3 or (
            normalized({"attempts": native_attempts})["attempts"][0].get("error") != "coordination-highwater"
            if native_attempts else True)
        caught = not infrastructure_failed and any(difference.startswith("/attempts/1/ok:")
                                                   for difference in mutant.get("snapshot_diffs", []))
        result["mutants"] = {"startup-clock-reloads": {"caught": caught,
            "snapshot_diffs": mutant.get("snapshot_diffs", []),
            "error": mutant.get("error"), "infrastructure_failed": infrastructure_failed}}
    if args.record_goldens and not failed(result) and all(value["caught"] for value in result.get("mutants", {}).values()):
        GOLDEN.write_text(json.dumps({"oracle_head": result["head"],
            "declared_noop_callbacks": result["declared_noop_callbacks"],
            "oracle": normalized(result["snapshots"]["python"])}, indent=1, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8", newline="\n")
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    bad = failed(result) or any(not value["caught"] for value in result.get("mutants", {}).values())
    print(f"clock retry: {'FAIL' if bad else '0 diffs'}", flush=True)
    return int(bad)


if __name__ == "__main__":
    raise SystemExit(main())
