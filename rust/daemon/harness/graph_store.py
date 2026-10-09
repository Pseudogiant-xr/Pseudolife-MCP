"""Graph-store differential: all bank state after every operation, plus goldens.

Uses the daemon harness's disposable-bank guard, environment isolation and
state comparator. The candidate is a feature-gated native fixture executable;
the oracle is the checkout's actual PostgresStorage methods, without models.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import lru_cache
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

os.environ["PL_HARNESS_SLICE"] = "w3h"
import dbstate
import pgdisposable as pg
from daemons import base_env

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
GOLDEN = Path(__file__).parent / "goldens" / "graph-store.json"
MUTANTS = ["graph-confidence", "graph-origin", "graph-revive", "graph-bless",
           "graph-alias", "graph-relink", "graph-merge", "graph-normalize"]
CLOCKS = {("entities", "created_at"), ("relations", "created_at"),
          ("edges", "asserted_at"), ("edges", "superseded_at")}


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


def seed(url):
    from pseudolife_memory.storage.postgres import PostgresStorage
    # Seed via the oracle's own persistence APIs; no hand-written rows.
    with patch("pseudolife_memory.storage.postgres.time.time", return_value=1.0):
        st = PostgresStorage(url)
        try:
            a = st.ensure_entity("node-a", "Node A")
            b = st.ensure_entity("node-b", "Node B", "runtime")
            c = st.ensure_entity("node-c", "Node C")
            st.add_alias("first", a)
            st.upsert_entity_source(a, "fixture-source", "manual", 1.0)
            st.upsert_entity_source(b, "fixture-source", "derived", 1.0)
            st.upsert_entity_source(a, "carried-source", "derived", 1.0)
            for index, (name, value, subject, obj) in enumerate([
                ("Node A", "Node B", a, b), ("node_a", "Node C", None, c),
                ("G:", "capacity", None, None), ("alpha beta", "x", None, None),
                ("ΟΣ", "Greek", None, None), ("100%_done", "percent", None, None),
                ("AA_İ", "tied-segment", None, None),
                ("prefix_\uA7CB", "unicode-14-a", None, None),
                ("prefix_\u1C89", "unicode-14-b", None, None),
                ("prefix_\uA7CC", "unicode-14-c", None, None),
            ]):
                st.upsert_fact(dict(entity=name, attribute=f"attr-{index}",
                    entity_norm=name.lower(), attribute_norm=f"attr-{index}",
                    value=value, status="current", polarity="+", confidence=0.7,
                    asserted_at=1.0, last_confirmed=1.0, entity_id=subject,
                    object_entity_id=obj))
            st.replace_lessons([dict(entity="Node A", attribute="approach",
                entity_norm="node a", attribute_norm="approach", value="keep",
                about="Node B", outcome="success", polarity="+", status="current",
                confidence=0.7, asserted_at=1.0, last_confirmed=1.0,
                entity_id=a, object_entity_id=b)])
            # A small deterministic vector is sufficient for edge-evidence FKs.
            for index in range(2):
                st.insert_entry(dict(band="flat", text=f"fixture support {index}",
                    embedding=[0.0] * 1024, surprise=0.0, ts=1.0,
                    access_count=0, source="fixture", superseded_at=1.0 if index else None))
        finally:
            st.close()


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


def oracle_call(st, request):
    kwargs = {k: v for k, v in request.items() if k != "op"}
    try:
        if request["op"] == "subgraph":
            value = oracle_graph_store()(st).subgraph(request["root"],
                depth=request.get("depth", 1), to_id=request.get("to"))
            value["nodes"] = sorted(value["nodes"])
            return {"value": value}
        if request["op"] in {"degree_counts", "degrees_by_name", "shortest_path", "resolve_relation",
                             "derive_edges", "build_subgraph"}:
            from pseudolife_memory import graph
            value = getattr(graph, request["op"])(**kwargs)
            if request["op"] == "build_subgraph":
                value["nodes"] = sorted(value["nodes"])
            return {"value": value}
        if request["op"] == "alias_canonical_map":
            from pseudolife_memory.graph import alias_canonical_map
            return {"value": alias_canonical_map(request["entities"],
                {int(k): v for k, v in request["aliases"].items()})}
        if request["op"] == "norm_name":
            from pseudolife_memory.graph import norm_name
            return {"value": norm_name(request["raw"])}
        return {"value": getattr(st, request["op"])(**kwargs)}
    except Exception as error:
        if not getattr(error, "sqlstate", None):
            raise
        return {"sqlstate": error.sqlstate}


@lru_cache(maxsize=1)
def oracle_graph_store():
    # Execute the actual checkout source without importing memory/__init__,
    # whose unrelated eager imports require torch. This wrapper uses graph.py
    # and the real PostgresStorage methods; no model or substitute oracle.
    source = REPO / "pseudolife_memory/memory/graph_store.py"
    spec = importlib.util.spec_from_file_location("graph_store_oracle", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PostgresNetworkxGraphStore


def run(candidate, mode, mutant=None, row="store"):
    from pseudolife_memory.storage.postgres import PostgresStorage
    import psutil
    names = [pg.PREFIX + str(os.getpid() * 10 + n) for n in range(3)]
    proc = oracle = None
    cases = []
    try:
        template = pg.create(names[0])
        seed(template)
        urls = [pg.create(name, template=names[0]) for name in names[1:]]
        initial = dbstate.dump(urls[0])
        clocks = [ClockState("python"), ClockState("rust")]
        for clock in clocks:
            clock.initialize(initial)
        golden_path = GOLDEN if row == "store" else GOLDEN.with_name("graph-read.json")
        golden = json.loads(golden_path.read_text(encoding="utf-8")) if mode == "golden" else None
        if golden and golden["operations"] != operations(row):
            raise AssertionError("golden input inventory is stale")
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
                if mode != "golden":
                    oracle = PostgresStorage(urls[0])
                for index, request in enumerate(operations(row)):
                    context = request
                    if request["op"] == "subgraph":
                        if oracle:
                            loaded = oracle.load_graph()
                            context = {**request, "op": "build_subgraph", "edges": [
                                dict(src=e["src_id"], relation=e["relation"], dst=e["dst_id"],
                                     confidence=e["confidence"], origin=e["origin"])
                                for e in loaded["edges"]], "relations": {r["name"]: dict(
                                    transitive=r["transitive"], inverse_of=r["inverse_of"])
                                    for r in oracle.load_relations()}}
                        else:
                            context = golden["cases"][index]["comparison_context"]
                    pstart = time.time()
                    expected = oracle_call(oracle, request) if oracle else golden["cases"][index]["response"]
                    pend = time.time()
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
                        raise AssertionError("candidate unexpectedly changed the bank catalog")
                    actual = clocks[1].response(actual)
                    if oracle:
                        pstate = clocks[0].state(dbstate.dump(urls[0]), index, (pstart, pend))
                        if catalog_shape(pstate) != catalog_shape(initial):
                            raise AssertionError("oracle unexpectedly changed the bank catalog")
                        expected = clocks[0].response(expected)
                        state_diffs = dbstate.diff(pstate, sstate)
                        expected_hash = digest(pstate)
                    else:
                        expected_hash = golden["cases"][index]["state_sha256"]
                        state_diffs = [] if digest(sstate) == expected_hash else ["golden bank-state digest differs"]
                    if context["op"] in {"derive_edges", "build_subgraph", "shortest_path"}:
                        from graph_read_compare import compare
                        diffs = compare(context, expected, actual, response_diff) + state_diffs
                    else:
                        diffs = response_diff(expected, actual) + state_diffs
                    cases.append(dict(index=index, op=request["op"], response=expected,
                                      state_sha256=expected_hash, diffs=diffs))
                    if request["op"] == "subgraph":
                        cases[-1]["comparison_context"] = context
                    if diffs:
                        print(f"case {index} {request['op']}: {diffs[:3]}", flush=True)
                proc.stdin.close()
                if proc.wait(timeout=30):
                    raise RuntimeError("candidate exit failure")
        return cases
    finally:
        if oracle:
            oracle.close()
        if proc and proc.poll() is None:
            # This Popen child has not been reaped; verify its recorded start
            # time as well, then stop only this fixture (it has no children).
            if psutil.Process(proc.pid).create_time() != started:
                raise RuntimeError("fixture process identity changed")
            proc.terminate()
            proc.wait(timeout=10)
        for name in reversed(names):
            pg.drop(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["live", "golden", "mutants"])
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--row", choices=["store", "read"], default="store")
    args = parser.parse_args()
    results = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO,
                                               text=True).strip(),
               "platform": sys.platform,
               "row": args.row,
               "candidate_sha256": hashlib.sha256(args.candidate.read_bytes()).hexdigest()}
    try:
        control = run(args.candidate, "golden" if args.mode == "golden" else "live", row=args.row)
        failed = sum(bool(c["diffs"]) for c in control)
        results["summary"] = {"cases": len(control), "diff_cases": failed}
        print(json.dumps(results["summary"]), flush=True)
        if failed:
            return 1
        if args.record:
            if args.mode != "live":
                parser.error("--record requires live mode")
            golden_path = GOLDEN if args.row == "store" else GOLDEN.with_name("graph-read.json")
            golden_path.write_text(json.dumps(dict(operations=operations(args.row), cases=control,
                normalizers="ClockState in graph_store.py; operation-specific windows; JSON object order free" +
                    ("; graph_read_compare.py: derived multisets, inverse collision provenance, node sets, "
                     "standalone minimum paths; subgraph selection exact" if args.row == "read" else "")),
                indent=1, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")
        if args.mode == "mutants":
            outcomes = {}
            failures = {}
            mutants = MUTANTS
            if args.row == "read":
                from graph_read_profile import MUTANTS as mutants
            for mutant in mutants:
                try:
                    cases = run(args.candidate, "live", mutant, row=args.row)
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
