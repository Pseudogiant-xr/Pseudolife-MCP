"""Storage startup cells using the daemon harness's disposable/catalog helpers.

The candidate is Cargo's daemon unit-test executable: it calls the production
Storage::open and close boundary without loading an embedding model. The
oracle calls PostgresStorage on a separate cloned bank. Every open is followed
by a full catalog/row snapshot; restart changes only writer_lease_epoch.
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
os.environ.setdefault("PL_HARNESS_SLICE", "pgs")
import dbstate
import pgdisposable as pg
import psycopg
from pseudolife_memory.storage.schema import assert_disposable_database
GOLDEN_FILE = HERE / "goldens" / "schema-startup.json"


def golden_state(state):
    from run import golden_scrub
    state = json.loads(json.dumps(state))
    # The schema installs the server's available extension version; the
    # native and oracle arms still compare extversion exactly in live diff.
    # Golden fixtures bind the DDL-controlled names and namespaces.
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
    name = f"{pg.PREFIX}{number}"
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


def cell(binary, number, commit=None, dimension=1024, expected=None):
    names = [f"{pg.PREFIX}{number + i}" for i in range(3)]
    created = []
    try:
        template = pg.create(names[0])
        created.append(names[0])
        if commit:
            with psycopg.connect(template, autocommit=True) as conn:
                assert_disposable_database(conn)
                historical_schema(commit).ensure_schema(conn)
        template_state = snapshot(template)
        dsns = []
        for name in names[1:]:
            dsns.append(pg.create(name, template=names[0]))
            created.append(name)
        from pseudolife_memory.storage.postgres import PostgresStorage
        stages = []
        recorded = []
        for restart in range(2):
            with psycopg.connect(dsns[0], autocommit=True) as conn:
                assert_disposable_database(conn)
            before = [snapshot(d) for d in dsns]
            python_start = time.time()
            if dimension != 1024:
                try:
                    PostgresStorage(dsns[0]).close()
                except RuntimeError as error:
                    if f"vector({dimension})" not in str(error):
                        raise
                else:
                    raise RuntimeError("Python unexpectedly upgraded a legacy embedding dimension")
            else:
                PostgresStorage(dsns[0]).close()
            python_window = [python_start, time.time()]
            rust_start = time.time()
            rust_open(binary, dsns[1], f"vector({dimension})" if dimension != 1024 else None)
            rust_window = [rust_start, time.time()]
            python, rust = (snapshot(d) for d in dsns)
            rules = {("relations", "created_at"): "clock"}
            diffs = dbstate.diff(dbstate.normalize(python, rules, template_state),
                                 dbstate.normalize(rust, rules, template_state))
            recorded.append(golden_state(dbstate.normalize(python, rules, template_state)))
            clock_errors = clock_diffs(python, before[0], python_window) + clock_diffs(rust, before[1], rust_window)
            stages.append({"restart": restart, "diffs": diffs, "clock_diffs": clock_errors,
                           "windows": {"python": python_window, "rust": rust_window}})
            if expected is not None:
                stages[-1]["golden_diffs"] = dbstate.diff(
                    expected[restart], golden_state(dbstate.normalize(rust, rules, template_state)))
            if dimension != 1024:
                stages[-1]["refusal_state_diffs"] = [d for old, new in zip(before, (python, rust))
                                                     for d in dbstate.diff(old, new)]
            if restart == 0:
                initial = (python, rust)
            else:
                # The lease epoch is explicitly incremented per successful open.
                strict = []
                for prior, state in zip(initial, (python, rust)):
                    for value in (prior, state):
                        table = value["rows"]["public.meta"]
                        table["rows"] = [r for r in table["rows"] if r[0] != "writer_lease_epoch"]
                    strict.extend(dbstate.diff(prior, state))
                stages[-1]["idempotence_diffs"] = strict
        return {"source_commit": commit, "stages": stages, "_recorded": recorded}
    finally:
        for name in reversed(created):
            pg.drop(name)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust-test-bin", required=True, type=Path)
    ap.add_argument("--commit", action="append", default=[])
    ap.add_argument("--all-versions", action="store_true")
    ap.add_argument("--refusals", action="store_true")
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--record-goldens", action="store_true")
    ap.add_argument("--check-goldens", action="store_true")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    if pg.SLICE != "pgs":
        ap.error("schema cells require PL_HARNESS_SLICE=pgs")
    results = []
    recorded = {}
    goldens = json.loads(GOLDEN_FILE.read_text()) if args.check_goldens else None
    number = time.time_ns() // 1000
    sources = [{"commit": c, "embedding_dimension": 1024} for c in args.commit]
    if args.all_versions:
        sources += json.loads((HERE / "schema_history.json").read_text())["versions"]
    for i, source in enumerate([{"commit": None, "embedding_dimension": 1024}, *sources]):
        commit = source["commit"]
        key = commit or "fresh"
        expected = goldens["cases"][key] if goldens else None
        result = cell(args.rust_test_bin.resolve(), number + i * 3, commit, source["embedding_dimension"], expected)
        recorded[key] = result.pop("_recorded")
        results.append(result)
        print(f"schema {commit or 'fresh'}: " + str(sum(len(s['diffs']) + len(s['clock_diffs']) + len(s.get('idempotence_diffs', [])) + len(s.get('refusal_state_diffs', [])) + len(s.get('golden_diffs', []))
                                                       for s in result['stages'])) + " diffs", flush=True)
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
    failed = any(s["diffs"] or s["clock_diffs"] or s.get("idempotence_diffs") or s.get("refusal_state_diffs") or s.get("golden_diffs")
                 for r in results for s in r["stages"]) or any(r["diffs"] for r in refusals)
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
                        result = cell(args.rust_test_bin.resolve(), number + 1000 + i * 3)
                        caught = any(s["diffs"] or s["clock_diffs"] or s.get("idempotence_diffs") for s in result["stages"])
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
            "catalog.extensions.extversion": "server-provided version; exact in live Python/Rust diff",
        }, "oracle_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
           "cases": recorded}, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
