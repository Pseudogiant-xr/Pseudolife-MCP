"""CPU-only startup hydration cells on independently cloned disposable banks.

Each case starts the native service's startup inputs dump on a bank cloned
from its seeded template and compares the snapshot (complete storage rows,
durable IDs, nullable fields, float32 vectors, resident timestamps and tags)
and the start's database changes with the committed golden (--check-goldens,
the default); --record-candidate re-records the golden from the candidate.
The frozen Python arm that recorded it first is retired. The zero-timestamp
cell fixes both entry and seating clocks to 1000 seconds.
Dimension fault fixtures remove column typmods before inserting legacy vectors.
Startup still accepts alternate world and lesson vector lengths.
This is startup evidence, not a claim that PG-HYDRATE is closed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_DATABASE_TURN_MARKER = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
import candidate_record
import daemons
import dbstate
import pgdisposable
import psycopg
from psycopg import sql
from schema_cases import clock_diffs
from startup_fixture import FIDELITY_CASES, LOAD_CLOCK, seed_bank
from rust.harness_support.bank_fixture import assert_fixture_database

pg = pgdisposable.Slice.from_env("pgs")
GOLDEN = HERE / "goldens" / "startup-hydration.json"
# Multi-band presets and retention policies other than balanced are retired
# (R11, maintainer 2026-11-02) and refuse Rust startup (run.py's retired-layout
# refusals), so the custom cases run one balanced band: a stored band name
# outside the preset is still renamed at hydration.
CUSTOM_CONFIG = """memory:
  embedding_dim: 1024
  miras:
    preset: custom
    bands:
      - {name: shallow, max_entries: 2, retention_policy: balanced}
"""
ZERO_TIME_CONFIG = """memory:
  embedding_dim: 1024
  miras:
    preset: custom
    bands:
      - {name: hot, max_entries: 1, retention_policy: balanced}
"""
CASES = (
    "empty", "seeded", "record-highwater", "preset-rename", "deep-overflow",
    "malformed-highwater-bool", "malformed-highwater-short",
    "malformed-highwater-negative", "malformed-highwater-component",
    "malformed-highwater-float", "required-loader-failure",
    "required-entry-loader-failure", "required-cortex-loader-failure",
    "required-world-loader-failure", "required-episode-loader-failure",
    "stale-entry-dims", "stale-cortex-dims", "stale-combined-dims",
    "world-lesson-dims-accepted", *FIDELITY_CASES,
)
# CI runs the matrix as two parity shards (rust.yml's cli-daemon and
# cli-daemon-2): serial cells cost about 29 s each on hosted Windows, 31 cells
# and 16 minutes in one shard (batch run 40047950017, 2026-10-24). Part 2 runs
# every mutant, the clock retry and the writer guard (startup_ci.py --part),
# so it also holds each case a mutant reruns (its control, which must pass in
# the same shard first) and the cases from FIRST_PART on: 11 cases each, part
# 1 beside schema parity's 4.7 minutes.
FIRST_PART = 12


def part_cases(part):
    """The cases one CI part runs, in CASES order; together they are CASES."""
    controls = {case for case, _ in MUTANTS.values()}
    second = tuple(case for index, case in enumerate(CASES) if case in controls or index >= FIRST_PART)
    return second if part == 2 else tuple(case for case in CASES if case not in second)
REQUIRED_FAILURES = {
    "required-loader-failure": ("lessons", "about", "lesson", "lesson-loader-about"),
    "required-entry-loader-failure": ("entries", "text", "entry", "entry-loader-text"),
    "required-cortex-loader-failure": ("facts", "value", "cortex", "cortex-loader-value"),
    "required-world-loader-failure": ("world_facts", "source_quote", "world cortex", "world-loader-source-quote"),
    # hydrate_cms loads episodes within service.py's entry-hydration wrapper.
    "required-episode-loader-failure": ("episodes", "title", "entry", "episode-loader-title"),
}
STALE_FAILURES = {
    "stale-entry-dims": {"count": 2, "dims": [384]},
    "stale-cortex-dims": {"count": 2, "dims": [768]},
    "stale-combined-dims": {"count": 4, "dims": [384, 768]},
}
NORMALIZERS = {
    "error": "named coordination-highwater, five loader/column failures, and stale-dimension wording; exact typed count/dims retained",
    "restart.meta.writer_lease_epoch": "removed only for idempotence; increment checked separately",
    "relations.created_at": "new constructor rows only, checked against each arm's invocation window",
    "refusal.entries.band": "cosmetic seating repair precedes a later required-loader/HLC refusal; still exact in cross-arm DB diff",
}
MUTANTS = {
    "startup-skip-world": ("preset-rename", "/startup/world:"),
    "startup-hlc-zero": ("preset-rename", "/startup/hlc_highwater:"),
    "startup-drop-tombstones": ("preset-rename", "/startup/metadata/episode_tombstones/"),
    "startup-skip-stamp": ("preset-rename", "/rows/public.entries/rows:"),
    "startup-skip-cortex-dims": ("stale-cortex-dims", "/ok:"),
    "startup-keep-zero-timestamp": ("zero-timestamp-seating", "/entries:"),
    "search-surprise-float32": ("surprise-real-text-rounding", "/search_entries:"),
    "startup-drop-non-string-tags": ("startup-tags-non-string-preservation", "/entries:"),
}


def invoke(binary, kind, dsn, home, output, mutant=None, case=None):
    env = daemons.base_env(home, {
        "PL_PGS_STARTUP_DSN": dsn, "PL_PGS_STARTUP_OUT": str(output),
        "PL_PGS_CONFIG": str(home / "data" / "config.yaml"),
        "PL_HARNESS_SLICE": "pgs", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "PL_PGS_CASE": case or "",
    })
    # Prevent an unrelated harness invocation leaking a bank or output path.
    for key in list(env):
        if key.startswith("PL_") and key not in {
            "PL_PGS_STARTUP_DSN", "PL_PGS_STARTUP_OUT", "PL_PGS_CONFIG", "PL_HARNESS_SLICE",
            "PL_PGS_CASE",
        }:
            env.pop(key)
    argv = [str(binary), "--exact", "startup::tests::db_startup_inputs_dump", "--nocapture"]
    if mutant:
        env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    start = time.time()
    run = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    window = [start, time.time()]
    if run.returncode:
        raise RuntimeError(f"{kind} startup cell exit {run.returncode}: "
                           f"{run.stdout[-1500:]} {run.stderr[-1500:]}")
    if kind == "rust" and "1 passed" not in run.stdout:
        raise RuntimeError("Rust startup probe did not run exactly one passing test")
    result = json.loads(output.read_text(encoding="utf-8"))
    if type(result.get("ok")) is not bool:
        raise RuntimeError(f"{kind} startup probe omitted its boolean ok field")
    if not result["ok"] and set(result) not in (
            {"ok", "error"}, {"ok", "error", "stale_dims"}, {"ok", "error", "retained"}):
        raise RuntimeError(f"{kind} failed startup exposed partial resident state")
    retained = result.get("retained")
    if not result["ok"] and retained is not None and (
            not isinstance(retained, dict) or set(retained) != {"startup", "entries"}
            or retained.get("startup", {}).get("hlc_reseed_pending") is not True):
        raise RuntimeError(f"{kind} retained an incomplete startup or a resolved clock failure")
    return result, window


def normalized_result(result):
    result = json.loads(json.dumps(result))
    if not result["ok"]:
        error = result["error"]
        details = result.get("stale_dims")
        if (isinstance(details, dict) and set(details) == {"count", "dims"}
                and type(details["count"]) is int and details["count"] > 0
                and type(details["dims"]) is list and details["dims"]
                and all(type(value) is int and value > 0 for value in details["dims"])
                and details["dims"] == sorted(set(details["dims"]))
                and f'{details["count"]} hydrated row(s)' in error):
            result["error"] = "stale-hydrated-dimensions"
        elif "invalid coordination clock high-water mark" in error:
            result["error"] = "coordination-highwater"
        else:
            for table, column, loader, category in REQUIRED_FAILURES.values():
                if f"{loader} hydration failed" in error and f'column "{column}"' in error:
                    result["error"] = category
                    break
    return result


def without_epoch(state):
    state = json.loads(json.dumps(state))
    table = state["rows"].get("public.meta")
    if table:
        index = table["columns"].index("key")
        table["rows"] = [row for row in table["rows"] if row[index] != "writer_lease_epoch"]
    return state


def without_refusal_bookkeeping(state):
    state = without_epoch(state)
    table = state["rows"].get("public.entries")
    if table:
        index = table["columns"].index("band")
        for row in table["rows"]:
            row[index] = "<pre-refusal seating>"
    return state


def start_delta(before, state, rules):
    """What one start changed in the seeded template, in a form a golden holds.

    The template's rows carry the seeders' own clocks, so its full post-state
    would not repeat between recordings. The delta keeps every row the start
    wrote, exactly (a new row's clock columns as tokens, per ``rules``), the
    first column (the key) of each template row it rewrote or removed, and
    each catalog row it added or removed. Rows are counted as multisets, so a
    duplicated row is a change. A table's rows sit under ``rows`` so that a
    difference reads like a full state comparison's
    (``/rows/public.entries/rows: ...``).
    """
    from collections import Counter
    from golden_values import golden_scrub

    def changed(old_rows, new_rows):
        old = Counter(json.dumps(row, sort_keys=True) for row in old_rows)
        new = Counter(json.dumps(row, sort_keys=True) for row in new_rows)
        return sorted((new - old).elements()), sorted((old - new).elements())

    after = dbstate.normalize(state, rules, before)
    written, replaced, catalog = {}, {}, {}
    for table in sorted(set(before["rows"]) | set(after["rows"])):
        added, removed = changed(before["rows"].get(table, {}).get("rows", []),
                                 after["rows"].get(table, {}).get("rows", []))
        if added:
            written[table] = {"rows": [json.loads(row) for row in added]}
        if removed:
            replaced[table] = {"rows": sorted(json.dumps(json.loads(row)[0]) for row in removed)}
    for name in sorted(set(before["catalog"]) | set(after["catalog"])):
        added, removed = changed(before["catalog"].get(name, []), after["catalog"].get(name, []))
        if added or removed:
            catalog[name] = {"added": [json.loads(row) for row in added],
                             "removed": [json.loads(row) for row in removed]}
    return golden_scrub({"rows": written, "replaced": replaced, "catalog_rows": catalog})


def reaches(difference, wanted):
    """Whether a difference is at a mutant's expected path. A start delta
    omits a table the start left alone, so a mutant that stops a start
    writing a table at all differs from the golden one level up: the
    table itself (``/rows/public.entries: only in python``)."""
    if difference.startswith(wanted):
        return True
    table = wanted[:-len("/rows:")] + ":" if wanted.startswith("/rows/") and wanted.endswith("/rows:") else None
    return table is not None and difference.startswith(table)


def epoch(state):
    table = state["rows"]["public.meta"]
    key, value = table["columns"].index("key"), table["columns"].index("value")
    return next(row[value] for row in table["rows"] if row[key] == "writer_lease_epoch")


def cell(binary, number, case, result, mutant=None, expected=None):
    """One case on a bank cloned from its seeded template: Rust starts twice,
    and ``expected`` (the case's committed snapshot and database stages, None
    when recording from the candidate) is its expectation."""
    arms = ("rust",)
    names = [pg.name(f"{pg.PREFIX}{number + index}") for index in range(1 + len(arms))]
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix="pl-pgs-startup-") as scratch:
            root = Path(scratch)
            config = (ZERO_TIME_CONFIG if case == "zero-timestamp-seating" else
                      CUSTOM_CONFIG if case in {"preset-rename", "deep-overflow"} else None)
            homes = [daemons.make_home(root, kind, config) for kind in arms]
            template = pg.create(names[0])
            created.append(names[0])
            seed_bank(template, case)
            if case in REQUIRED_FAILURES:
                table, column, _, _ = REQUIRED_FAILURES[case]
                with psycopg.connect(template, autocommit=True) as conn:
                    assert_fixture_database(conn)
                    conn.execute(sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(
                        sql.Identifier(table), sql.Identifier(column)))
            before = dbstate.dump(template)
            dsns = []
            for name in names[1:]:
                dsns.append(pg.create(name, template=names[0]))
                created.append(name)
            initial = None
            result["stages"] = []
            for restart in range(2):
                outputs, states, windows = [], [], []
                for kind, dsn, home in zip(arms, dsns, homes):
                    output, window = invoke(binary, kind, dsn, home, root / f"{kind}-{restart}.json", mutant=mutant, case=case)
                    outputs.append(output)
                    states.append(dbstate.dump(dsn))  # immediately after each arm
                    windows.append(window)
                expected_ok = not (case.startswith("malformed-highwater")
                                   or case in REQUIRED_FAILURES or case in STALE_FAILURES)
                expected_error = ("stale-hydrated-dimensions" if case in STALE_FAILURES
                                  else REQUIRED_FAILURES[case][3] if case in REQUIRED_FAILURES
                                  else "coordination-highwater")
                semantic_diffs = []
                for kind, output, state in zip(arms, outputs, states):
                    if output["ok"] != expected_ok:
                        semantic_diffs.append(f"{kind}: expected ok={expected_ok}")
                    elif not expected_ok and normalized_result(output)["error"] != expected_error:
                        semantic_diffs.append(f"{kind}: wrong refusal category")
                    if case in STALE_FAILURES:
                        semantic_diffs.extend(dbstate.diff(
                            STALE_FAILURES[case], output.get("stale_dims"), f"/{kind}/expected_stale_dims"))
                    if output["ok"] and case == "zero-timestamp-seating":
                        # One band (multi-band seating is retired, R11): both
                        # stay in hot; the zero timestamp is served as the load
                        # clock while the bank keeps 0.
                        semantic_diffs.extend(dbstate.diff(
                            [("hot", LOAD_CLOCK), ("hot", LOAD_CLOCK - 100)],
                            [(row["band"], row["ts"]) for row in output["entries"]],
                            f"/{kind}/zero_timestamp_seats"))
                        table = state["rows"]["public.entries"]
                        columns = table["columns"]
                        durable = sorted(table["rows"], key=lambda row: row[columns.index("id")])
                        semantic_diffs.extend(dbstate.diff(
                            [("hot", 0.0), ("hot", LOAD_CLOCK - 100)],
                            [(row[columns.index("band")], row[columns.index("ts")]) for row in durable],
                            f"/{kind}/durable_zero_timestamp_seats"))
                    if output["ok"] and case == "surprise-real-text-rounding":
                        semantic_diffs.extend(dbstate.diff(
                            [0.5, 0.0001, 0.1232, 0.875],
                            [row["surprise_score"] for row in output.get("search_entries", [])],
                            f"/{kind}/surprise_scores"))
                    if output["ok"] and case == "startup-tags-non-string-preservation":
                        semantic_diffs.extend(dbstate.diff(
                            json.dumps([["valid", 7, True]]),
                            json.dumps([row["tags"] for row in output["entries"]]),
                            f"/{kind}/resident_tags"))
                rules = {("relations", "created_at"): "clock"}
                stage = {
                    "restart": restart, "snapshots": dict(zip(arms, outputs)),
                    "snapshot_diffs": semantic_diffs,
                    "clock_diffs": [difference for state, window in zip(states, windows)
                                    for difference in clock_diffs(state, before, window)],
                    "windows": dict(zip(arms, windows)),
                    "database": {kind: start_delta(before, state, rules) for kind, state in zip(arms, states)},
                }
                if expected is not None:
                    # The golden is the left side, which dbstate.diff labels "python".
                    stage["golden_diffs"] = dbstate.diff(
                        expected["snapshot"][restart], normalized_result(stage["snapshots"]["rust"]))
                    stage["db_golden_diffs"] = dbstate.diff(
                        expected["database"][restart], stage["database"]["rust"])
                if initial is not None:
                    stage["idempotence_diffs"] = [
                        difference for old, new in zip(initial[1], states)
                        for difference in dbstate.diff(without_epoch(old), without_epoch(new))
                    ] + [difference for old, new in zip(initial[0], outputs)
                         for difference in dbstate.diff(normalized_result(old), normalized_result(new))]
                else:
                    initial = (outputs, states)
                for index, state in enumerate(states):
                    prior = before if restart == 0 else initial[1][index]
                    old_epoch, new_epoch = epoch(prior), epoch(state)
                    if type(old_epoch) is not int or type(new_epoch) is not int or new_epoch != old_epoch + 1:
                        stage.setdefault("epoch_diffs", []).append(f"arm {index}: constructor epoch did not increment once")
                    if not expected_ok:
                        unchanged = dbstate.diff(without_refusal_bookkeeping(prior), without_refusal_bookkeeping(state))
                        stage.setdefault("refusal_state_diffs", []).extend(unchanged)
                result["stages"].append(stage)
    finally:
        cleanup_errors = []
        for name in reversed(created):
            try:
                pg.drop(name)
            except Exception as error:
                cleanup_errors.append(f"{name}: {type(error).__name__}")
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors


def failed(result):
    return bool(result.get("error") or result.get("cleanup_errors") or any(
        value for stage in result.get("stages", []) for key, value in stage.items()
        if key.endswith("_diffs")))


def main(argv=None):
    # No abbreviations: a shortened flag must not select a recording mode.
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--rust-test-bin", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--only", nargs="+", choices=CASES)
    parser.add_argument("--part", type=int, choices=(1, 2), help="one CI part's cases (part_cases)")
    parser.add_argument("--mutants", action="store_true")
    # Outside the group, so its retirement message wins over "not allowed with".
    parser.add_argument("--record-goldens", action="store_true", help=argparse.SUPPRESS)
    golden_mode = parser.add_mutually_exclusive_group()
    golden_mode.add_argument("--check-goldens", action="store_true")
    golden_mode.add_argument("--record-candidate", action="store_true",
                             help="re-record the golden from the Rust candidate (candidate_record.py); a dry "
                                  "run unless --write")
    parser.add_argument("--write", action="store_true", help="with --record-candidate: write the changed and new cases")
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
        parser.error("startup cells require PL_HARNESS_SLICE=pgs")
    if args.rust_test_bin is None or args.out is None:
        parser.error("--rust-test-bin and --out are required")
    if args.only and args.part:
        parser.error("--only and --part select cases differently; give one")
    if args.write and not args.record_candidate:
        parser.error("--write needs --record-candidate")
    if args.record_candidate and (args.part or args.mutants):
        parser.error("--record-candidate takes the whole matrix or --only; check the result with "
                     "--check-goldens --mutants")
    binary = args.rust_test_bin.resolve()
    if not binary.is_file():
        parser.error("--rust-test-bin must name the built daemon Cargo test executable")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.record_candidate:
        return record_candidate(args, binary)
    payload = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
               "normalizers": NORMALIZERS, "cells": []}
    number = time.time_ns()
    golden_path = GOLDEN
    goldens = json.loads(golden_path.read_text())

    def expected(case):
        if case not in goldens["cases"] or case not in goldens.get("database", {}):
            raise RuntimeError(f"startup-hydration.json has no committed golden for {case}; "
                               + candidate_record.startup_recording("startup_cases.py"))
        return {"snapshot": goldens["cases"][case], "database": goldens["database"][case]}

    for index, case in enumerate(args.only or (part_cases(args.part) if args.part else CASES)):
        result = {"case": case}
        payload["cells"].append(result)
        try:
            cell(binary, number + index * 3, case, result, expected=expected(case))
        except Exception as error:
            result["error"] = str(error)
        args.out.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"startup {case}: {'FAIL' if failed(result) else '0 diffs'}", flush=True)
    bad = any(failed(result) for result in payload["cells"])
    if args.mutants and not bad:
        payload["mutants"] = {}
        for index, (mutant, (mutant_case, wanted_path)) in enumerate(MUTANTS.items()):
            result = {"case": mutant_case}
            try:
                cell(binary, number + 1000 + index * 3, mutant_case, result, mutant=mutant,
                     expected=expected(mutant_case))
            except Exception as error:
                result["error"] = str(error)
            stages = result.get("stages", [])
            # An infrastructure failure is never proof of a source mutant.
            infrastructure_failed = bool(result.get("error") or result.get("cleanup_errors") or any(
                stage.get("clock_diffs") or stage.get("epoch_diffs") for stage in stages))
            # A mutant shows as a difference from the committed stage.
            differences = [difference for stage in stages
                           for key in ("snapshot_diffs", "golden_diffs", "db_golden_diffs")
                           for difference in stage.get(key, [])]
            caught = not infrastructure_failed and len(stages) == 2 and any(
                reaches(difference, wanted_path) for difference in differences)
            payload["mutants"][mutant] = {
                "caught": caught, "expected_path": wanted_path,
                "differences": differences,
                **({"error": result["error"]} if result.get("error") else {}),
                **({"cleanup_errors": result["cleanup_errors"]} if result.get("cleanup_errors") else {}),
            }
            print(f"startup mutant {mutant}: {'caught' if caught else 'SURVIVED'}", flush=True)
            args.out.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        bad = any(not value["caught"] for value in payload["mutants"].values())
    return int(bad)


def record_candidate(args, binary):
    """--record-candidate: each selected case starts on the candidate alone
    and is classed against startup-hydration.json (candidate_record.py)."""
    why = candidate_record.refuse_mutant_environment()
    if why:
        print(f"startup record-candidate refused: {why}", flush=True)
        return 1
    golden_path = GOLDEN
    recording = candidate_record.Recording(golden_path, ("cases", "database"), binary, whole=not args.only,
                                           defined=list(CASES))
    recording.write = args.write
    payload = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO).decode().strip(),
               "normalizers": NORMALIZERS, "cells": []}
    number = time.time_ns()
    for index, case in enumerate(args.only or CASES):
        result = {"case": case}
        payload["cells"].append(result)
        try:
            cell(binary, number + index * 3, case, result)
        except Exception as error:
            result["error"] = str(error)
        stages = result.get("stages", [])
        failures = ([f"error: {result['error']}"] if result.get("error") else []) + [
            f"cleanup: {e}" for e in result.get("cleanup_errors", [])] + [
            d for stage in stages for d in candidate_record.stage_failures(stage)]
        observed = ({"cases": [normalized_result(stage["snapshots"]["rust"]) for stage in stages],
                     "database": [stage["database"]["rust"] for stage in stages]}
                    if len(stages) == 2 else None)
        recording.observe(case, observed, failures, candidate_record.round_trip)
    code = recording.finish(args.write, "python rust/daemon/harness/startup_ci.py --out <scratch>/startup.json")
    payload["record_candidate"] = recording.report
    args.out.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
