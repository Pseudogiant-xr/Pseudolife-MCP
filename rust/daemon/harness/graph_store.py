"""Graph regression replay: all bank state after every operation, plus goldens.

Uses the daemon harness's disposable-bank guard, environment isolation and
state comparator. The candidate is a feature-gated native fixture executable;
fixed SQL fixtures initialize its bank without importing the retired package.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path

_DATABASE_TURN_MARKER = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
import dbstate
import pgdisposable
from daemons import base_env
from graph_fixtures import ReadFixture, seed

pg = pgdisposable.Slice("w3h")

GOLDEN = Path(__file__).parent / "goldens" / "graph-store.json"
MUTANTS = ["graph-confidence", "graph-origin", "graph-revive", "graph-bless",
           "graph-alias", "graph-relink", "graph-merge", "graph-normalize"]
CLOCKS = {("entities", "created_at"), ("relations", "created_at"),
          ("edges", "asserted_at"), ("edges", "superseded_at")}


def same_locale(recorded, actual):
    def canonical(value):
        return value.lower().replace(".utf-8", ".utf8")
    return all(canonical(recorded[key]) == canonical(actual[key])
               for key in ("lc_collate", "lc_ctype"))


def golden_collation_skips(golden, locale):
    if same_locale(golden["database_locale"], locale):
        return [], None
    reason = (f"graph-read golden requires recorded locale {golden['database_locale']}; "
              f"disposable database has {locale}; independent SQL fixture comparison required")
    if os.environ.get("PL_GRAPH_REQUIRE_RECORDED_LOCALE") == "1":
        raise AssertionError(reason)
    indices = golden["collation_cases"]
    assert indices == list(range(155, 164)), "collation skip inventory is stale"
    return indices, reason


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    _precise_time = ctypes.WinDLL("kernel32").GetSystemTimePreciseAsFileTime
    _precise_time.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    _precise_time.restype = None


def wall_time():
    """Use the candidate's precise clock, not Python 3.11's coarse Windows clock.

    API: learn.microsoft.com/windows/win32/api/sysinfoapi/
    nf-sysinfoapi-getsystemtimepreciseasfiletime (VOID, output FILETIME).
    """
    if os.name == "nt":
        value = wintypes.FILETIME()
        _precise_time(ctypes.byref(value))
        ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
        return (ticks - 116444736000000000) / 10000000
    return time.time_ns() / 1000000000


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate response key")
            result[key] = value
        return result
    def nonfinite(_value):
        raise ValueError("non-finite response number")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)


def response_diff(expected, actual):
    differences = dbstate.diff(expected, actual)
    if not differences and json.dumps(expected, sort_keys=True) != json.dumps(actual, sort_keys=True):
        differences.append("JSON scalar types differ")
    return differences


def operations(row="store"):
    if row == "read":
        from graph_read_profile import operations as read_operations
        return read_operations()
    def op(operation, **kwargs):
        return {"op": operation, **kwargs}
    def edge(name="upsert_edge", src=1, dst=2, **kwargs):
        return op(name, src_id=src, relation="uses", dst_id=dst, **kwargs)
    result = [op("load_graph"), op("load_relations"),
        op("find_entity", name_norm="missing"), op("find_entity", name_norm="first"),
        op("ensure_entity", canonical="node-a", display="Changed", etype="service"),
        op("ensure_entity", canonical="node-a", etype="person"),
        op("add_alias", alias_norm="node-b", entity_id=1),
        op("find_entity", name_norm="node-b"), op("entity_id_map"),
        op("add_alias", alias_norm="z-last", entity_id=1),
        op("add_alias", alias_norm="first", entity_id=2),
        op("find_entity", name_norm="first"), op("find_entity", name_norm="node-a"),
        op("upsert_relation", name="custom", description="new", transitive=True, inverse_of="uses"),
        op("upsert_relation", name="custom", description="updated", src_type="service"),
        op("upsert_relation", name="uses", description="updated builtin", dst_type="runtime"),
        op("load_relations"),
        edge(confidence=0.65, origin="user"), edge(confidence=0.1, origin="agent"),
        edge(confidence=0.95, origin="action"), edge(confidence=0.1),
        edge("supersede_edge"), edge("supersede_edge"),
        edge(confidence=0.1, origin="agent", revive=False), op("load_graph"),
        edge("bless_edge"), edge(origin="action"), edge("bless_edge"), op("load_graph"),
        edge("bless_edge", src=3), edge("supersede_edge", src=3),
        edge(src=3, source_entry_ids=[2, 999], revive=False),
        edge(src=3, source_entry_ids=[1, 2, 1], origin="agent", revive=False),
        edge(src=3, source_entry_ids=[1], origin="action"),
        edge(src=3, origin="other"), edge(src=3, origin="new-other"),
        edge(src=3, origin="user"), edge(src=3, origin="action"),
        # Both incoming and outgoing merge collisions; explicit self-loops.
        edge(src=2, dst=3), edge(src=1, dst=3), edge(src=3, dst=1),
        edge(src=1, dst=1), edge(src=2, dst=2), edge(src=2, dst=1),
        op("merge_entity", from_id=1, into_id=2), op("load_graph"),
        op("find_entity", name_norm="node-a"), op("entity_id_map"),
        op("merge_entity", from_id=2, into_id=2),
        op("merge_entity", from_id=1, into_id=3),
        op("merge_entity", from_id=2, into_id=999),
        op("delete_entity", entity_id=2), op("delete_entity", entity_id=2),
        op("ensure_entity", canonical="node-a", display="Node A"),
        op("ensure_entity", canonical="node-b", display="Node B"),
        op("ensure_entity", canonical="g", display="G:"),
        op("ensure_entity", canonical="alphabeta"),
        op("ensure_entity", canonical="ος", display="ΟΣ"),
        op("ensure_entity", canonical="100%-done", display="100%_done"),
        op("ensure_entity", canonical="", display=""),
        op("ensure_entity", canonical="aa-i\u0307"),
        op("ensure_entity", canonical="prefix-\uA7CB"),
        op("ensure_entity", canonical="prefix-\u1C89"),
        op("ensure_entity", canonical="prefix-\uA7CC"),
        op("load_graph"), op("find_entity", name_norm="g"),
        # FK errors must roll back and permit the next ordinary operation.
        op("add_alias", alias_norm="bad", entity_id=999),
        op("upsert_relation", name="bad-relation", description="invalid", inverse_of="missing"),
        edge(src=999), op("load_graph"), op("load_relations")]
    result += [op("norm_name", raw=raw) for raw in [
        " -- Depends///__On:::  ", "\t/A_B\\C.D:E/\u00a0",
        "\u001cAlpha\u001d\u001eBeta\u001f", "-./_: ",
        "\u00a0ΓΣ.AΣ:\u0301B.. --", "  İ / Ö -- .. CAFÉ : ",
        "A\u0085B\u2028C\u2029D", "a\u2007\u202fB", "  --___",
        "A...//\\\\:::__B---C", "AA_İ", "Σ\u0345", "AΣ\u0345 A", "",
    ]]
    return result


class ClockState:
    """Clock changes retain their operation identity and each arm's bounds.

    Unchanged values remain exact, including their equality to seeded values;
    a missing change on either arm therefore differs. No global clock erasure.
    """
    def __init__(self, arm="observer"):
        self.seen = {}
        self.arm = arm

    def state(self, raw, index, window):
        normalized = json.loads(json.dumps(raw))
        for qualified, table in normalized["rows"].items():
            name = qualified.split(".", 1)[1]
            for row in table["rows"]:
                for offset, col in enumerate(table["columns"]):
                    if (name, col) not in CLOCKS:
                        continue
                    key = (name, row[0], col)
                    value = row[offset]
                    previous, token = self.seen.get(key, (None, None))
                    if value != previous:
                        if value is not None:
                            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                                    or not math.isfinite(value) or not window[0] <= value <= window[1]:
                                raise AssertionError(f"clock outside operation window: {name}.{col}; "
                                    f"arm={self.arm}, op={index}, value={value!r}, window={window!r}")
                            token = f"<clock:{index}>"
                        else:
                            token = None
                        self.seen[key] = (value, token)
                    row[offset] = token
        return normalized

    def initialize(self, state):
        for qualified, table in state["rows"].items():
            name = qualified.split(".", 1)[1]
            for row in table["rows"]:
                for offset, col in enumerate(table["columns"]):
                    if (name, col) in CLOCKS:
                        value = row[offset]
                        self.seen[name, row[0], col] = (value, value)

    def response(self, value):
        # Read clocks resolve through the corresponding durable entity/edge,
        # rather than merely accepting any timestamp-shaped response value.
        value = json.loads(json.dumps(value))
        def walk(item):
            if isinstance(item, dict):
                for col, table in [("created_at", "entities"), ("asserted_at", "edges")]:
                    if col in item and "id" in item:
                        raw, token = self.seen[table, item["id"], col]
                        if type(item[col]) is not type(raw) or item[col] != raw:
                            raise AssertionError(f"response disagrees with stored {col}")
                        item[col] = token
                for child in item.values():
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)
        walk(value)
        return value


def digest(state):
    # Rows and sequence state are portable across PostgreSQL versions and
    # fixture owners. Catalog immutability is checked separately, per arm.
    portable = {"rows": state["rows"], "sequences": state["catalog"]["sequences"]}
    return hashlib.sha256(json.dumps(portable, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def catalog_shape(state):
    catalog = json.loads(json.dumps(state["catalog"]))
    catalog["sequences"] = [row[:-1] for row in catalog["sequences"]]
    return catalog


def run(candidate, mode, mutant=None, row="store", metadata=None):
    if mode not in {"golden", "record"}:
        raise ValueError("live Python comparison is retired; use golden or --record-candidate")
    import psutil
    names = [pg.name(pg.PREFIX + str(os.getpid() * 10 + n)) for n in range(3)]
    proc = reference = None
    cases = []
    skip_reason = None
    skipped_indices = []
    try:
        template = pg.create(names[0])
        golden_path = GOLDEN if row == "store" else GOLDEN.with_name("graph-read.json")
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        if mode == "golden" and golden["operations"] != operations(row):
            raise AssertionError("golden input inventory is stale")
        if row == "read":
            import psycopg
            with psycopg.connect(template) as conn:
                collate, ctype = conn.execute("SELECT datcollate, datctype FROM pg_database "
                                             "WHERE datname = current_database()").fetchone()
            locale = {"lc_collate": collate, "lc_ctype": ctype}
            print("graph-read database locale: " + json.dumps(locale), flush=True)
            if metadata is not None:
                metadata["database_locale"] = locale
            # template1 carries vector for the restricted test login. Pinning
            # template0's locale loses that untrusted extension; retain isolation
            # and compare locale-dependent cells with an independent SQL
            # fixture under the runner's actual collation.
            if mode == "golden":
                skipped_indices, skip_reason = golden_collation_skips(golden, locale)
                if skipped_indices:
                    print(f"SQL fixture comparisons for cases {skipped_indices}: {skip_reason}", flush=True)
        seed(template)
        urls = [pg.create(name, template=names[0]) for name in names[1:]]
        initial = dbstate.dump(urls[0])
        clocks = [ClockState("sql fixture"), ClockState("rust")]
        for clock in clocks:
            clock.initialize(initial)
        with tempfile.TemporaryDirectory(prefix="pl-w3h-") as home, ThreadPoolExecutor(max_workers=1) as reader:
            env = {"PSEUDOLIFE_MCP_DATABASE_URL": urls[1]}
            if mutant:
                env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
            log_path = Path(home) / "native.log"
            with log_path.open("w", encoding="utf-8") as log, ExitStack() as lifetime:
                proc = subprocess.Popen([str(candidate.resolve())], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                    env=base_env(Path(home), env))
                started = psutil.Process(proc.pid).create_time()
                def stop_fixture():
                    if proc.poll() is None:
                        if psutil.Process(proc.pid).create_time() != started:
                            raise RuntimeError("fixture process identity changed")
                        proc.terminate()
                        proc.wait(timeout=10)
                lifetime.callback(stop_fixture)
                if skipped_indices:
                    reference = ReadFixture(urls[0])
                for index, request in enumerate(operations(row)):
                    context = request
                    if request["op"] == "subgraph":
                        if mode == "record":
                            from graph_fixtures import read_context
                            context = read_context(urls[1], request)
                        else:
                            context = golden["cases"][index]["comparison_context"]
                    expected = golden["cases"][index]["response"] if mode == "golden" else None
                    pstart = wall_time()
                    fixture_response = reference.apply(request, pstart) if reference else None
                    pend = wall_time()
                    if reference and (fixture_response is not None or index in skipped_indices):
                        pstate = clocks[0].state(dbstate.dump(urls[0]), index, (pstart, pend))
                        if catalog_shape(pstate) != catalog_shape(initial):
                            raise AssertionError("SQL fixture unexpectedly changed the bank catalog")
                        if index in skipped_indices:
                            expected = fixture_response if fixture_response is not None else reference.subgraph(request, expected)
                    sstart = wall_time()
                    proc.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
                    proc.stdin.flush()
                    line = reader.submit(proc.stdout.readline).result(timeout=30)
                    send = wall_time()
                    if not line:
                        raise RuntimeError("candidate exited: " + log_path.read_text(encoding="utf-8")[-1000:])
                    actual = strict_json(line)
                    sstate = clocks[1].state(dbstate.dump(urls[1]), index, (sstart, send))
                    if catalog_shape(sstate) != catalog_shape(initial):
                        raise AssertionError("candidate unexpectedly changed the bank catalog: " +
                                             str(dbstate.diff(catalog_shape(initial), catalog_shape(sstate))[:3]))
                    actual = clocks[1].response(actual)
                    if index in skipped_indices:
                        state_diffs = dbstate.diff(pstate, sstate)
                        expected_hash = digest(pstate)
                    else:
                        state_diffs = []
                    if mode == "golden" and index not in skipped_indices:
                        expected = golden["cases"][index]["response"]
                        expected_hash = golden["cases"][index]["state_sha256"]
                        if digest(sstate) != expected_hash:
                            state_diffs.append("golden bank-state digest differs")
                    if mode == "record":
                        expected, expected_hash = actual, digest(sstate)
                    if context["op"] in {"derive_edges", "build_subgraph", "shortest_path"}:
                        from graph_read_compare import compare
                        diffs = compare(context, expected, actual, response_diff) + state_diffs
                    else:
                        diffs = response_diff(expected, actual) + state_diffs
                    cases.append(dict(index=index, op=request["op"], response=expected,
                                      state_sha256=expected_hash, diffs=diffs))
                    if index in skipped_indices:
                        cases[-1]["skip_reason"] = skip_reason
                    if request["op"] == "subgraph":
                        cases[-1]["comparison_context"] = context
                    if diffs:
                        print(f"case {index} {request['op']}: {diffs[:3]}", flush=True)
                proc.stdin.close()
                if proc.wait(timeout=30):
                    raise RuntimeError("candidate exit failure")
        return cases
    finally:
        if reference:
            reference.close()
        if proc and proc.poll() is None:
            # This Popen child has not been reaped; verify its recorded start
            # time as well, then stop only this fixture (it has no children).
            if psutil.Process(proc.pid).create_time() != started:
                raise RuntimeError("fixture process identity changed")
            proc.terminate()
            proc.wait(timeout=10)
        for name in reversed(names):
            pg.drop(name)


def record_candidate(candidate, row, write, results):
    import candidate_record
    why = candidate_record.refuse_mutant_environment()
    if why:
        results["failure"] = why
        print(why, flush=True)
        return 1
    golden_path = GOLDEN if row == "store" else GOLDEN.with_name("graph-read.json")
    cases = run(candidate, "record", row=row, metadata=results)
    observed = {"operations": operations(row), "cases": cases}
    if row == "read":
        observed.update(database_locale=results["database_locale"], collation_cases=list(range(155, 164)))
    failures = [d for case in cases for d in case["diffs"]]
    failures += [mark for case in cases if (mark := candidate_record.deferral(case["response"]))]
    if not cases or len(cases) != len(observed["operations"]):
        failures.append("candidate did not answer every operation")
    recording = candidate_record.Recording(golden_path, tuple(observed), candidate,
                                           whole=False, defined=[row], single=True)
    previous = recording.entry(row)
    if previous:
        for index, (request, case) in enumerate(zip(observed["operations"], cases)):
            if (index >= len(previous["operations"]) or index >= len(previous["cases"])
                    or previous["operations"][index] != request):
                continue
            context = case.get("comparison_context", request)
            if context["op"] in {"derive_edges", "build_subgraph", "shortest_path"}:
                from graph_read_compare import compare
                recorded = previous["cases"][index]["response"]
                # Keep the existing representation of equivalent edge/path
                # orderings, exactly as replay's structural comparator does.
                if not compare(context, recorded, case["response"], response_diff):
                    case["response"] = recorded
    recording.write = write
    recording.observe(row, observed, failures, candidate_record.round_trip)
    results["recording"] = recording.report
    return recording.finish(write, f"graph_store.py golden --row {row} --candidate <binary>")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["golden", "mutants"])
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--record-candidate", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--row", choices=["store", "read"], default="store")
    args = parser.parse_args()
    if args.write and not args.record_candidate:
        parser.error("--write needs --record-candidate")
    if args.record_candidate and args.mode != "golden":
        parser.error("--record-candidate belongs to golden mode")
    if _DATABASE_TURN_MARKER.exists():
        _turn_sys = __import__("sys")
        _turn_root = str(__import__("pathlib").Path(__file__).resolve().parents[3])
        _turn_sys.path.insert(0, _turn_root)
        try:
            from rust.harness_support.database_turn import require_turn

            require_turn(holder="standalone database harness", contain=True)
        finally:
            _turn_sys.path.remove(_turn_root)
    results = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO,
                                               text=True).strip(),
               "platform": sys.platform,
               "mode": args.mode,
               "row": args.row,
               "candidate_sha256": hashlib.sha256(args.candidate.read_bytes()).hexdigest()}
    try:
        if args.record_candidate:
            return record_candidate(args.candidate, args.row, args.write, results)
        control = run(args.candidate, "golden",
                      row=args.row, metadata=results)
        failed = sum(bool(c["diffs"]) for c in control)
        skipped = [c["index"] for c in control if "skip_reason" in c]
        results["summary"] = {"cases": len(control) - len(skipped), "diff_cases": failed}
        if skipped:
            results["summary"].update(executed_cases=len(control), skipped_indices=skipped,
                                      sql_fixture_cases=len(skipped),
                                      skip_reason=next(c["skip_reason"] for c in control if "skip_reason" in c))
        print(json.dumps(results["summary"]), flush=True)
        if failed:
            return 1
        if args.mode == "mutants":
            outcomes = {}
            failures = {}
            mutants = MUTANTS
            if args.row == "read":
                from graph_read_profile import MUTANTS as mutants
            for mutant in mutants:
                try:
                    cases = run(args.candidate, "golden", mutant, row=args.row)
                    outcomes[mutant] = sum(bool(c["diffs"]) for c in cases)
                except Exception as error:
                    # Only a completed comparison can catch a source mutant.
                    outcomes[mutant] = None
                    failures[mutant] = f"{type(error).__name__}: {error}"
                print(f"mutant {mutant}: {outcomes[mutant]}", flush=True)
            results["mutants"] = outcomes
            results["mutant_failures"] = failures
            return int(bool(failures) or any(not count for count in outcomes.values()))
        return 0
    except Exception as error:
        results["failure"] = f"{type(error).__name__}: {error}"
        print(results["failure"], flush=True)
        return 1
    finally:
        if args.out:
            args.out.write_text(json.dumps(results, indent=1), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
