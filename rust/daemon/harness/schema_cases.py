"""Storage startup cells using the daemon harness's disposable/catalog helpers.

The candidate is Cargo's daemon unit-test executable: it calls the production
Storage::open and close boundary without loading an embedding model. The
oracle calls PostgresStorage on a separate cloned bank. Every open is followed
by a full catalog/row snapshot; restart changes only writer_lease_epoch.
Checking goldens (--check-goldens, what CI runs) opens only the candidate and
compares each stage with the committed snapshot; recording runs both.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
import dbstate
import pgdisposable
import psycopg
from pseudolife_memory.storage.schema import assert_disposable_database

pg = pgdisposable.Slice.from_env("pgs")
GOLDEN_FILE = HERE / "goldens" / "schema-startup.json"


def golden_state(state):
    from run import golden_scrub
    state = json.loads(json.dumps(state))
    # The schema installs the server's available extension version, so the
    # golden binds only the DDL-controlled names and namespaces. A recording
    # (both arms) still compares extversion exactly; checking does not.
    state["catalog"]["extensions"] = [[name, "<server-version>", namespace]
                                       for name, version, namespace in state["catalog"]["extensions"]]
    return golden_scrub(state)


def clock_diffs(state, before, window, resolution=None):
    start, end = window
    if not (math.isfinite(start) and math.isfinite(end) and start <= end):
        return ["constructor invocation window is invalid"]
    # Python 3.11's Windows wall clock can be coarser than Rust's system
    # clock. Each endpoint carries one tick of measurement uncertainty.
    resolution = time.get_clock_info("time").resolution if resolution is None else resolution
    start, end = start - resolution, end + resolution
    prior = {r[0] for r in before["rows"].get("public.relations", {}).get("rows", [])}
    table = state["rows"].get("public.relations")
    if not table:
        return []
    index = table["columns"].index("created_at")
    return [f"relations.{r[0]}.created_at is outside its constructor window" for r in table["rows"]
            if r[0] not in prior and (isinstance(r[index], bool) or not isinstance(r[index], (int, float))
                                     or not start <= r[index] <= end)]


def snapshot(dsn):
    return dbstate.dump(dsn)


def rust_open(binary, dsn, refusal=None):
    # Parent process checks the server identity before dispatching this cell.
    env = dict(os.environ, PL_W1A_OPEN_DSN=dsn)
    env.pop("PL_W1A_TEST_DSN", None)
    env.pop("PL_SCHEMA_EXPECT_REFUSAL", None)
    if refusal:
        env["PL_SCHEMA_EXPECT_REFUSAL"] = refusal
    r = subprocess.run([str(binary), "storage::tests::db_open_once_for_state_compare", "--exact", "--nocapture"],
                       env=env, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"Rust storage cell exit {r.returncode}: {r.stdout[-1500:]} {r.stderr[-1500:]}")
    if "1 passed" not in r.stdout:
        raise RuntimeError("Rust storage cell did not execute exactly one passing test")


def historical_schema(commit):
    source = subprocess.run(["git", "show", f"{commit}:pseudolife_memory/storage/schema.py"],
                            cwd=REPO, check=True, capture_output=True).stdout
    module = types.ModuleType("historical_schema")
    exec(compile(source, "historical_schema.py", "exec"), module.__dict__)
    return module


def refusal_cell(binary, number, version):
    name = pg.name(f"{pg.PREFIX}{number}")
    dsn = pg.create(name)
    try:
        from pseudolife_memory.storage.postgres import PostgresStorage
        PostgresStorage(dsn).close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert_disposable_database(conn)
            conn.execute("UPDATE meta SET value=%s::jsonb WHERE key='schema_version'", (version,))
        before = dbstate.dump(dsn)
        expected = "newer than" if version == "99" else "positive integer"
        rust_open(binary, dsn, expected)
        return {"version": version, "diffs": dbstate.diff(before, dbstate.dump(dsn))}
    finally:
        pg.drop(name)


def cell(binary, number, commit=None, dimension=1024, expected=None, oracle=True):
    """One source schema on cloned banks. With ``oracle`` PostgresStorage opens
    a clone beside Rust and each stage compares the two; without it only Rust
    opens, and ``expected`` (the committed stages) is its expectation."""
    arms = ("python", "rust") if oracle else ("rust",)
    names = [pg.name(f"{pg.PREFIX}{number + i}") for i in range(1 + len(arms))]
    created = []
    try:
        template = pg.create(names[0])
        created.append(names[0])
        if commit:
            with psycopg.connect(template, autocommit=True) as conn:
                assert_disposable_database(conn)
                historical_schema(commit).ensure_schema(conn)
        template_state = snapshot(template)
        dsns = {}
        for arm, name in zip(arms, names[1:]):
            dsns[arm] = pg.create(name, template=names[0])
            created.append(name)
        stages = []
        recorded = []
        observed = []   # the candidate's entry per open, for --record-candidate
        rules = {("relations", "created_at"): "clock"}
        for restart in range(2):
            for dsn in dsns.values():
                with psycopg.connect(dsn, autocommit=True) as conn:
                    assert_disposable_database(conn)
            before = {arm: snapshot(dsn) for arm, dsn in dsns.items()}
            windows = {}
            if oracle:
                from pseudolife_memory.storage.postgres import PostgresStorage
                python_start = time.time()
                if dimension != 1024:
                    try:
                        PostgresStorage(dsns["python"]).close()
                    except RuntimeError as error:
                        if f"vector({dimension})" not in str(error):
                            raise
                    else:
                        raise RuntimeError("Python unexpectedly upgraded a legacy embedding dimension")
                else:
                    PostgresStorage(dsns["python"]).close()
                windows["python"] = [python_start, time.time()]
            rust_start = time.time()
            rust_open(binary, dsns["rust"], f"vector({dimension})" if dimension != 1024 else None)
            windows["rust"] = [rust_start, time.time()]
            states = {arm: snapshot(dsn) for arm, dsn in dsns.items()}
            normalized = {arm: dbstate.normalize(state, rules, template_state) for arm, state in states.items()}
            stage = {"restart": restart, "windows": windows,
                     "clock_diffs": [d for arm in arms for d in clock_diffs(states[arm], before[arm], windows[arm])]}
            if oracle:
                stage["diffs"] = dbstate.diff(normalized["python"], normalized["rust"])
                recorded.append(golden_state(normalized["python"]))
            observed.append(golden_state(normalized["rust"]))
            if expected is not None:
                stage["golden_diffs"] = dbstate.diff(expected[restart], observed[-1])
            if dimension != 1024:
                stage["refusal_state_diffs"] = [d for arm in arms for d in dbstate.diff(before[arm], states[arm])]
            if restart == 0:
                initial = states
            else:
                # The lease epoch is explicitly incremented per successful open.
                strict = []
                for arm in arms:
                    prior, state = initial[arm], states[arm]
                    for value in (prior, state):
                        table = value["rows"]["public.meta"]
                        table["rows"] = [r for r in table["rows"] if r[0] != "writer_lease_epoch"]
                    strict.extend(dbstate.diff(prior, state))
                stage["idempotence_diffs"] = strict
            stages.append(stage)
        return {"source_commit": commit, "stages": stages, "_recorded": recorded, "_observed": observed}
    finally:
        for name in reversed(created):
            pg.drop(name)


def stage_diffs(stage):
    """Every difference a stage reports, whichever arms ran."""
    return [d for key in ("diffs", "clock_diffs", "idempotence_diffs", "refusal_state_diffs", "golden_diffs")
            for d in stage.get(key, [])]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust-test-bin", required=True, type=Path)
    ap.add_argument("--commit", action="append", default=[])
    ap.add_argument("--all-versions", action="store_true")
    ap.add_argument("--refusals", action="store_true")
    ap.add_argument("--mutants", action="store_true")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--record-goldens", action="store_true")
    mode.add_argument("--check-goldens", action="store_true")
    mode.add_argument("--record-candidate", action="store_true",
                      help="re-record the golden from the Rust candidate (candidate_record.py); a dry run "
                           "unless --write")
    ap.add_argument("--write", action="store_true", help="with --record-candidate: write the changed and new cases")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    if pg.SLICE != "pgs":
        ap.error("schema cells require PL_HARNESS_SLICE=pgs")
    if args.write and not args.record_candidate:
        ap.error("--write needs --record-candidate")
    if args.record_candidate:
        if args.refusals or args.mutants:
            ap.error("--record-candidate records the golden's cases only; check the result with "
                     "--check-goldens --refusals --mutants")
        return record_candidate(args)
    results = []
    recorded = {}
    goldens = json.loads(GOLDEN_FILE.read_text()) if args.check_goldens else None
    oracle = goldens is None
    number = time.time_ns() // 1000
    sources = [{"commit": c, "embedding_dimension": 1024} for c in args.commit]
    if args.all_versions:
        sources += json.loads((HERE / "schema_history.json").read_text())["versions"]
    for i, source in enumerate([{"commit": None, "embedding_dimension": 1024}, *sources]):
        commit = source["commit"]
        key = commit or "fresh"
        expected = goldens["cases"][key] if goldens else None
        result = cell(args.rust_test_bin.resolve(), number + i * 3, commit, source["embedding_dimension"], expected, oracle)
        recorded[key] = result.pop("_recorded")
        results.append(result)
        print(f"schema {commit or 'fresh'}: {sum(len(stage_diffs(s)) for s in result['stages'])} diffs", flush=True)
        args.out.write_text(json.dumps({"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
                                       "cells": results}, indent=2), encoding="utf-8")
    refusals = []
    if args.refusals:
        for i, version in enumerate(["99", '"55"', "null", "true", "1.5", "0", "-1"]):
            result = refusal_cell(args.rust_test_bin.resolve(), number + len(results) * 3 + i, version)
            refusals.append(result)
            print(f"schema refusal {version}: {len(result['diffs'])} state diffs", flush=True)
        output = json.loads(args.out.read_text())
        output["refusals"] = refusals
        args.out.write_text(json.dumps(output, indent=2), encoding="utf-8")
    failed = any(stage_diffs(s) for r in results for s in r["stages"]) or any(r["diffs"] for r in refusals)
    if args.mutants and not failed:
        mutations = {}
        try:
            for i, mutant in enumerate(["drop-alter-tail", "skip-relation-seed", "skip-lease-epoch",
                                        "schema-future-allowed", "seed-clock-zero"]):
                os.environ["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
                try:
                    if mutant == "schema-future-allowed":
                        result = refusal_cell(args.rust_test_bin.resolve(), number + 1000 + i * 3, "99")
                        caught = bool(result["diffs"])
                    else:
                        # Live, a mutant shows as a Python/Rust difference;
                        # checking goldens, as a difference from the fresh case.
                        result = cell(args.rust_test_bin.resolve(), number + 1000 + i * 3,
                                      expected=goldens["cases"]["fresh"] if goldens else None, oracle=oracle)
                        caught = any(stage_diffs(s) for s in result["stages"])
                except RuntimeError as error:
                    # Only the expected source mutation can satisfy this
                    # control; infrastructure errors remain harness failures.
                    if mutant != "schema-future-allowed" or "opened a refused schema" not in str(error):
                        raise
                    caught = True
                mutations[mutant] = {"caught": caught}
                print(f"schema mutant {mutant}: {'caught' if caught else 'SURVIVED'}", flush=True)
        finally:
            os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
        output = json.loads(args.out.read_text())
        output["mutants"] = mutations
        args.out.write_text(json.dumps(output, indent=2), encoding="utf-8")
        failed = any(not r["caught"] for r in mutations.values())
    if args.record_goldens and not failed:
        # Oracle capture only; the candidate's state never becomes its own
        # expectation. The existing golden scrub retains all row values and
        # hashes complete catalogs rather than truncating them.
        GOLDEN_FILE.write_text(json.dumps({"normalizers": {
            "relations.created_at": "newly seeded rows only, against the common template",
            "catalog": "golden_scrub: full catalog row counts and SHA-256 digests",
            "catalog.extensions.extversion": "server-provided version; exact only between the arms of a recording",
        }, "oracle_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
           "cases": recorded}, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return int(failed)


def record_candidate(args):
    """--record-candidate: each selected source schema runs on the candidate
    alone and is classed against schema-startup.json (candidate_record.py)."""
    import candidate_record
    why = candidate_record.refuse_mutant_environment()
    if why:
        print(f"schema record-candidate refused: {why}", flush=True)
        return 1
    binary = args.rust_test_bin.resolve()
    history = json.loads((HERE / "schema_history.json").read_text())["versions"]
    by_commit = {source["commit"]: source for source in history}
    unknown = [c for c in args.commit if c not in by_commit]
    if unknown:
        # The golden holds the fresh schema and the recorded history only.
        print("schema record-candidate refused: --commit " + ", ".join(unknown) + " is not in "
              "schema_history.json, so no golden case holds it", flush=True)
        return 1
    chosen = history if args.all_versions else [by_commit[c] for c in dict.fromkeys(args.commit)]
    sources = [{"commit": None, "embedding_dimension": 1024}, *chosen]
    # Only the whole matrix (fresh and every recorded version) may remove a case.
    whole = args.all_versions and not args.commit
    recording = candidate_record.Recording(
        GOLDEN_FILE, ("cases",), binary, whole=whole,
        defined=["fresh", *(source["commit"] for source in history)], dump=candidate_record.dump_sorted)
    number = time.time_ns() // 1000
    results = []
    for i, source in enumerate(sources):
        key = source["commit"] or "fresh"
        try:
            result = cell(binary, number + i * 3, source["commit"], source["embedding_dimension"], oracle=False)
        except Exception as error:   # a failed open is a refused case, not a crash
            result = {"source_commit": source["commit"], "stages": [], "error": str(error), "_observed": []}
        entry = result.pop("_observed")
        result.pop("_recorded", None)
        results.append(result)
        failures = ([f"error: {result['error']}"] if result.get("error") else []) + [
            d for stage in result["stages"] for d in stage_diffs(stage)]
        recording.observe(key, {"cases": entry} if len(entry) == 2 else None, failures,
                          candidate_record.round_trip)
    code = recording.finish(args.write, "python rust/daemon/harness/schema_ci.py --out <scratch>/schema.json")
    args.out.write_text(json.dumps({"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
                                    "cells": results, "record_candidate": recording.report}, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
