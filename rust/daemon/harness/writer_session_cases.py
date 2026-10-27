"""Writer-session probe, reconnect and handover through the actual Python and
native services.

Each arm starts its service on a clone of one seeded bank, then: the writer
session is killed and the next call must reconnect and keep the resident
copy (``death``); it is killed again while a second writer takes and drops
the bank, so the reconnect must flag the handover and the service re-read the
bank (``handover``); it is killed a third time while a second writer holds
the lease, so the call and ``/health``'s database probe are refused
(``held``) until the holder leaves and the retry window passes
(``released``). Both arms use the same second-writer helpers (this script's
``--other-writer`` and ``--hold-lease``), and every phase dumps the bank.
Python's recovery and curation preamble other than the rehydrate under test
is inert, as in ``clock_retry_cases.py``. The native embedder loads the
existing four-dimensional offline ONNX fixture.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

PHASES = ("initial", "death", "handover", "held", "released")
CONFIG = """embedding:
  model_name: fixture-writer-session
  backend: onnx
  device: cpu
  cpu_dtype: fp32
memory:
  embedding_dim: 4
  miras:
    preset: flat
"""
NOOP_CALLBACKS = (
    "_recover_correction_locked", "_recover_entry_reinstatement_locked",
    "_recover_lesson_synthesis", "_refresh_source_retired_lessons_locked",
    "_carry_over_listing_spelling",
)
# Past _SESSION_PROBE_INTERVAL (1 s), so Python probes the killed session
# too; inside it Python serves without probing (see divergences.md).
PAST_PROBE = 1.1
# Past _LEASE_RETRY_SECONDS (5 s).
PAST_LEASE_RETRY = 5.1
MUTANTS = ("writer-no-reconnect", "writer-epoch-unchecked")


def _dsn() -> str:
    dsn = os.environ["PL_PGS_WRITER_DSN"]
    name = psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]
    pg._check(name)
    if not re.fullmatch(r"[0-9]+_[0-9a-f]{16}", name[len(pg.PREFIX):]):
        raise ValueError("writer fixture database must have a numeric owned name")
    return dsn


def dump_phase(phase):
    dsn = _dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert_disposable_database(conn)
    output = Path(os.environ["PL_PGS_WRITER_DB_OUT"])
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{phase}.json").write_text(json.dumps(dbstate.dump(dsn), allow_nan=False), encoding="utf-8")


def other_writer():
    """A second writer takes the bank's lease and leaves at once."""
    from pseudolife_memory.storage.postgres import PostgresStorage
    PostgresStorage(_dsn()).close()


def hold_lease():
    """A second writer holds the lease until the gate file is removed."""
    from pseudolife_memory.storage.postgres import PostgresStorage
    gate = Path(os.environ["PL_PGS_WRITER_GATE"])
    ready = gate.with_suffix(".ready")
    storage = PostgresStorage(_dsn())
    try:
        ready.write_text("held", encoding="utf-8")
        deadline = time.monotonic() + 120
        while gate.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("the lease gate was never released")
            time.sleep(0.05)
    finally:
        storage.close()
        ready.unlink(missing_ok=True)


def helper(*args):
    run = subprocess.run([sys.executable, str(Path(__file__).resolve()), *args],
                         cwd=REPO, env=os.environ.copy(), capture_output=True, text=True, timeout=120)
    if run.returncode:
        raise RuntimeError(f"writer helper {args} exit {run.returncode}: {run.stderr[-1800:]}")


def kill(storage):
    """End the writer session from a side connection, as a server restart
    would. psycopg learns of it only on the next use."""
    pid = storage._conn.info.backend_pid
    with psycopg.connect(_dsn(), autocommit=True) as side:
        side.execute("SELECT pg_terminate_backend(%s)", (pid,))


def oracle_cell(seed_only=False):
    from pseudolife_memory.storage.postgres import PostgresStorage
    storage = PostgresStorage(_dsn())
    if seed_only:
        try:
            startup.seed(storage, "clock-retry")
        finally:
            storage.close()
        return
    import torch
    from pseudolife_memory.memory.hlc import HybridLogicalClock
    from pseudolife_memory.service import MemoryService
    torch.set_num_threads(1)
    config = Path(os.environ["PL_PGS_WRITER_CONFIG"])
    service = MemoryService(data_dir=config.parent, config_path=config,
                            database_url=_dsn())
    service._storage = storage
    service._embedder = SimpleNamespace(embedding_dim=4)
    service._hlc = HybridLogicalClock(now_ms=lambda: 100)
    for name in NOOP_CALLBACKS:
        setattr(service, name, lambda: None)
    service._hydrate_resident_stores = lambda: startup.python_hydrate(
        service._storage, config, resident_service=service)
    phases = []
    try:
        with patch("pseudolife_memory.curation_safety.recover_slot_curation", return_value=None):
            service._ensure_init()
            first = (service._cms, service._world, service._cortex)

            def observe(phase, call, pid_before, dump=True):
                try:
                    call()
                except Exception as error:
                    ok, error_text, same, replaced = False, str(error), None, None
                else:
                    ok, error_text = True, None
                    same = {"bank": service._cms is first[0], "startup": service._world is first[1],
                            "cortex": service._cortex is first[2]}
                    replaced = service._storage._conn.info.backend_pid != pid_before
                try:
                    service._storage.ping()
                    ping = "ok"
                except Exception as error:
                    ping = f"error: {error}"
                phases.append({"phase": phase, "observed": {
                    "ok": ok, "error": error_text, "same_as_first": same, "session_replaced": replaced,
                    "epoch": service._storage._lease_epoch, "health_db": ping,
                    "resident_invalidated": service._storage.resident_invalidated}})
                if dump:
                    dump_phase(phase)

            pid = service._storage._conn.info.backend_pid
            observe("initial", lambda: None, pid)

            kill(service._storage)
            time.sleep(PAST_PROBE)
            observe("death", service._ensure_init, pid)

            pid = service._storage._conn.info.backend_pid
            kill(service._storage)
            helper("--other-writer")
            time.sleep(PAST_PROBE)
            observe("handover", service._ensure_init, pid)

            pid = service._storage._conn.info.backend_pid
            kill(service._storage)
            gate = Path(os.environ["PL_PGS_WRITER_GATE"])
            ready = gate.with_suffix(".ready")
            gate.write_text("hold", encoding="utf-8")
            holder = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--hold-lease"],
                                      cwd=REPO, env=os.environ.copy())
            deadline = time.monotonic() + 60
            while not ready.exists():
                if time.monotonic() > deadline or holder.poll() is not None:
                    raise RuntimeError("the lease holder never took the lease")
                time.sleep(0.05)
            time.sleep(PAST_PROBE)
            # The phase dump comes after the holder leaves, as in the Rust arm.
            observe("held", service._ensure_init, pid, dump=False)
            gate.unlink()
            if holder.wait(timeout=60):
                raise RuntimeError("lease holder failed")
            dump_phase("held")
            time.sleep(PAST_LEASE_RETRY)
            observe("released", service._ensure_init, pid)
        Path(os.environ["PL_PGS_WRITER_OUT"]).write_text(json.dumps({"phases": phases}, allow_nan=False),
                                                        encoding="utf-8")
    finally:
        service._storage.close()


LEASE_HOLDER = re.compile(r"backend pid [0-9]+ \(application_name '[^']*'\)")


def normalized(result):
    """Only the second writer's backend pid and application name vary."""
    text = LEASE_HOLDER.sub("backend pid <pid> (application_name <holder>)", json.dumps(result))
    return json.loads(text)


def compare_phases(python, rust):
    python, rust = normalized(python), normalized(rust)
    return dbstate.diff(python, rust)


def runtime_library():
    import onnxruntime
    root = Path(onnxruntime.__file__).parent / "capi"
    if os.name == "nt":
        return root / "onnxruntime.dll"
    return next(root.glob("libonnxruntime.so.*"))


def invoke(binary, kind, dsn, home, model, phases, output, gate, seed_only=False, mutant=None):
    env = daemons.base_env(home, {
        "PL_HARNESS_SLICE": "pgs", "PL_PGS_WRITER_DSN": dsn,
        "PL_PGS_WRITER_CONFIG": str(home / "data" / "config.yaml"),
        "PL_PGS_WRITER_OUT": str(output), "PL_PGS_WRITER_PYTHON": sys.executable,
        "PL_PGS_WRITER_SCRIPT": str(Path(__file__).resolve()),
        "PL_PGS_WRITER_DB_OUT": str(phases), "PL_PGS_WRITER_GATE": str(gate),
        "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "PSEUDOLIFE_DAEMON_ONNX_DIR": str(model), "ORT_DYLIB_PATH": str(runtime_library()),
    })
    for key in list(env):
        if key.startswith("PL_") and key != "PL_HARNESS_SLICE" and not key.startswith("PL_PGS_WRITER_"):
            env.pop(key)
    if kind == "python":
        command = [sys.executable, str(Path(__file__).resolve()), "--oracle-cell"]
        if seed_only:
            command.append("--seed-cell")
    else:
        command = [str(binary), "--exact", "service::tests::db_writer_session_phases", "--nocapture"]
        if mutant:
            env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    run = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    if run.returncode:
        raise RuntimeError(f"{kind} writer session cell exit {run.returncode}: "
                           f"{run.stdout[-1800:]} {run.stderr[-1800:]}")
    if kind == "rust" and "1 passed" not in run.stdout:
        raise RuntimeError("native writer session probe did not execute one passing test")
    return None if seed_only else json.loads(output.read_text(encoding="utf-8"))


def expectations(result):
    """What each phase must show, whatever the two arms agree on."""
    phases = {phase["phase"]: phase["observed"] for phase in result.get("phases", [])}
    if tuple(phases) != PHASES:
        return [f"phases {list(phases)} != {list(PHASES)}"]
    differences = []

    def expect(phase, key, value):
        differences.extend(dbstate.diff(value, phases[phase][key], f"/{phase}/{key}"))

    e0 = phases["initial"]["epoch"]
    kept = dict.fromkeys(("bank", "startup", "cortex"), True)
    dropped = dict.fromkeys(("bank", "startup", "cortex"), False)
    for phase in ("death", "handover", "released"):
        expect(phase, "ok", True)
        expect(phase, "session_replaced", True)
        expect(phase, "health_db", "ok")
        expect(phase, "resident_invalidated", None)
    expect("death", "same_as_first", kept)
    expect("death", "epoch", e0 + 1)
    expect("handover", "same_as_first", dropped)
    expect("handover", "epoch", e0 + 3)
    expect("held", "ok", False)
    expect("held", "epoch", e0 + 3)
    lease = "another process holds the writer lease on bank "
    if not str(phases["held"]["error"]).startswith(lease):
        differences.append("/held/error: expected the writer-lease refusal")
    if phases["held"]["health_db"] != f"error: {phases['held']['error']}":
        differences.append("/held/health_db: expected the same refusal while the holder is there")
    expect("released", "same_as_first", dropped)
    expect("released", "epoch", e0 + 5)
    return differences


def cell(binary, number, result, mutant=None):
    names = [pg.name(f"{pg.PREFIX}{number + index}") for index in range(3)]
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix="pl-pgs-writer-session-") as scratch:
            root = Path(scratch)
            from embedding_fixture import create
            model = root / "model"
            create(model)
            homes = [daemons.make_home(root, name, CONFIG) for name in ("seed", "python", "rust")]
            template = pg.create(names[0])
            created.append(names[0])
            invoke(binary, "python", template, homes[0], model, root / "seed-db", root / "seed.json",
                   root / "seed.gate", seed_only=True)
            dsns = []
            for name in names[1:]:
                dsns.append(pg.create(name, template=names[0]))
                created.append(name)
            outputs, states = [], []
            for kind, dsn, home in zip(("python", "rust"), dsns, homes[1:]):
                phases = root / f"{kind}-db"
                output = invoke(binary, kind, dsn, home, model, phases, root / f"{kind}.json",
                                root / f"{kind}.gate", mutant=mutant)
                # Each arm has its own bank, which the lease refusal names.
                bank = psycopg.conninfo.conninfo_to_dict(dsn)["dbname"]
                outputs.append(json.loads(json.dumps(output).replace(f"'{bank}'", "'<bank>'")))
                states.append({phase: json.loads((phases / f"{phase}.json").read_text()) for phase in PHASES})
            result["observed"] = dict(zip(("python", "rust"), (normalized(o) for o in outputs)))
            result["observed_diffs"] = compare_phases(*outputs)
            result["expectation_diffs"] = [f"{kind}{difference}" for kind, output in zip(("python", "rust"), outputs)
                                           for difference in expectations(output)]
            result["db_diffs"] = {phase: dbstate.diff(states[0][phase], states[1][phase]) for phase in PHASES}
    finally:
        for name in reversed(created):
            try:
                pg.drop(name)
            except Exception as error:
                result.setdefault("cleanup_errors", []).append(f"{name}: {type(error).__name__}")


def failed(result):
    return bool(result.get("error") or result.get("cleanup_errors") or result.get("observed_diffs")
                or result.get("expectation_diffs") or any(result.get("db_diffs", {}).values()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-test-bin", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--mutants", action="store_true")
    parser.add_argument("--dump-db", choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument("--oracle-cell", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seed-cell", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--other-writer", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hold-lease", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if pg.SLICE != "pgs":
        parser.error("writer session cells require PL_HARNESS_SLICE=pgs")
    if args.dump_db:
        dump_phase(args.dump_db)
        return 0
    if args.other_writer:
        other_writer()
        return 0
    if args.hold_lease:
        hold_lease()
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
    if args.mutants and not failed(result):
        result["mutants"] = {}
        for index, name in enumerate(MUTANTS, 1):
            mutant = {}
            try:
                cell(args.rust_test_bin.resolve(), number + 1000 * index, mutant, name)
            except Exception as error:
                mutant["error"] = str(error)
            caught = bool(mutant.get("observed_diffs") or mutant.get("expectation_diffs")
                          or any(mutant.get("db_diffs", {}).values()) or mutant.get("error"))
            result["mutants"][name] = {"caught": caught, "error": mutant.get("error"),
                                       "observed_diffs": mutant.get("observed_diffs", [])[:8],
                                       "expectation_diffs": mutant.get("expectation_diffs", [])[:8]}
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    mutants_ok = (not args.mutants or set(result.get("mutants", {})) == set(MUTANTS)) and all(
        m["caught"] for m in result.get("mutants", {}).values())
    ok = not failed(result) and mutants_ok
    print(f"writer session: {'0 diffs' if not failed(result) else 'FAIL'}"
          + (f"; mutants {'all caught' if mutants_ok else 'MISSED'}" if args.mutants else ""), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
