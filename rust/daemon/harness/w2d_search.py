"""W2-D live differential harness for GET /api/search: the Python daemon
(oracle) against the Rust daemon, scenario by scenario, each on its own copy
of one seeded disposable bank.

usage:
  python w2d_search.py live    --rust-bin PATH [--only NAME ...] [--out FILE] [--record]
  python w2d_search.py golden  --rust-bin PATH [--only NAME ...] [--out FILE]
  python w2d_search.py mutants --rust-bin PATH_BUILT_WITH_FEATURE_MUTANTS [--only NAME ...]

Per case it compares the status, the content type and the body by value
(exact, with the declared rules below). After every scenario it dumps both
banks and diffs every table; ``retrieval_events`` rows are compared field by
field with the float rule. Exit status 0 only when nothing differs.

Declared rules, never applied by hand:
* SCORE_TOL: an entry or fact ``score`` and every float inside
  ``retrieval_events.served``/``params`` may differ by 2e-4 (torch and ONNX
  embeddings differ in the last bits; spec "Free").
* Tie order: two entries may swap places only when their scores are within
  SCORE_TOL of each other (spec "Free": tie order).
* ``retrieval_events.created_at`` and other wall-clock columns: null-or-not.

Environment: ORT_DYLIB_PATH, PSEUDOLIFE_DAEMON_ONNX_DIR (Qwen3 export) and
PSEUDOLIFE_DAEMON_RERANK_DIR (cross-encoder export) for the Rust side;
PSEUDOLIFE_TEST_PG_HOST_PORT / the test login for the bank.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daemons  # noqa: E402
import dbstate  # noqa: E402
import pgdisposable as pg  # noqa: E402
from run import DB_DECLARED, bearer, call, settled, wait_settled  # noqa: E402,F401

REPO = HERE.parents[2]
GOLDENS = HERE / "goldens" / "w2d"
SCORE_TOL = 2e-4
TOKEN = "tok-w2d-search-0001"
# Python-only init writes owned by other slices (W1-A's list, minus the
# retrieval event this slice now writes).
DECLARED = [d for d in DB_DECLARED if d[1] not in ("retrieval_events", "retrieval_events_id_seq")]
CLOCK_COLUMNS = {
    ("retrieval_events", "created_at"): "clock: wall clock per event (storage/postgres.py:2160)",
    ("relations", "created_at"): "clock: wall clock per row (storage/postgres.py:765)",
    ("slot_reads", "last_read_at"): "clock: wall clock per read",
    ("slot_reads", "first_read_at"): "clock: wall clock per read",
}
# Each deliberate break (rust/daemon/src/mutants.rs, `--features mutants`)
# and the scenario that must catch it.
MUTANTS = {"search-skip-event": "log-off-control", "search-no-access-bump": "default",
           "search-timeline-unsorted": "timeline", "search-chronicle-limit": "default",
           "search-no-contiguity": "contiguity", "search-rrf-off": "rrf-pool",
           "search-no-cortex": "default", "search-rerank-unfused": "margin-gate"}
pg.DISPOSABLE_NAME = re.compile(r"pl_cf_w2d_[a-z0-9_]{1,40}")


def q(**kw) -> str:
    return "/api/search?" + urllib.parse.urlencode(kw)


QUERIES = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))
if isinstance(QUERIES, dict):
    QUERIES = QUERIES.get("queries", [])
CRAFTED = [
    "when did we deploy the release",
    "how many times did we deploy",
    "what happened on 2026-08-12",
    "process_chunk_v2 KeyError",
    "PLX-4471",
    "do I have a cat",
    "what os does the homelab box run",
    "which port does the daemon listen on",
    "what services run on the homelab box",
    "where is the extractor endpoint",
    "first we backed up the bank then what",
    "staging database host",
]


def default_cases() -> list[dict]:
    auth = [bearer(TOKEN)]
    out = []
    for i, text in enumerate(QUERIES[:20]):
        out.append(case(f"corpus {i}", q(q=text, top_k=8), auth))
    for i, text in enumerate(CRAFTED):
        out.append(case(f"crafted {i}", q(q=text, top_k=8), auth))
    out += [
        case("repeat crafted 5 (access counts)", q(q=CRAFTED[5], top_k=8), auth),
        case("top_k blank", q(q="deploy the daemon", top_k=""), auth),
        case("top_k 0", q(q="deploy the daemon", top_k=0), auth),
        case("top_k 3", q(q="deploy the daemon", top_k=3), auth),
        case("source filter", q(q="deploy", source="conversation"), auth),
        case("tag filter", q(q="deploy", tag="DEPLOY, bug"), auth),
        case("band flat", q(q="deploy", band="flat"), auth),
        case("unknown band", q(q="deploy", band="working"), auth),
        case("blank q", q(q="   "), auth),
        case("min_score 0.5", q(q="deploy the daemon", min_score="0.5"), auth),
        case("min_score 0.95", q(q="bearer token", min_score="0.95"), auth),
        case("bm25 off", q(q="PLX-4471 writer lease", bm25="false"), auth),
        case("bm25 auto", q(q="PLX-4471 writer lease", bm25="auto"), auth),
        case("recency off", q(q="deploy the daemon", disable_recency_boost="true"), auth),
        case("rerank on", q(q="how do I back up the bank", rerank="1"), auth),
        case("rerank on, top_k 30 (over budget)", q(q="how do I back up the bank", rerank="1", top_k=30), auth),
        case("session header", q(q="deploy the daemon"), auth + [("X-PL-Session", "sess-w2d-1")]),
        case("open-episode session header", q(q="failed deploy incident"),
             auth + [("X-PL-Session", "sess-w2d-seed")]),
        case("slot channel: jacque top_k 1", q(q="is jacque female", top_k=1), auth),
        case("slot channel: staging migration top_k 1", q(q="staging migration", top_k=1), auth),
        case("slot channel: superseded endpoint top_k 1", q(q="extractor endpoint old gpu box", top_k=1), auth),
        case("slot channel under explicit floor", q(q="is jacque female", top_k=1, min_score="0.7"), auth),
        case("two session headers", q(q="bearer token"),
             auth + [("X-PL-Session", "sess-a"), ("X-PL-Session", "sess-b")]),
    ]
    return out


def case(name, path, headers=(), declared=None):
    return {"name": name, "method": "GET", "path": path, "headers": list(headers), "declared": declared}


class Scenario:
    name = ""
    config_yaml: str | None = None

    def cases(self) -> list[dict]:
        return default_cases()


class Default(Scenario):
    name = "default"


class Timeline(Scenario):
    name = "timeline"
    config_yaml = "memory:\n  search:\n    timeline_channel: true\n"

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"temporal {i}", q(q=t, top_k=8), auth) for i, t in enumerate(
            ["when did we deploy the release", "what happened first", "in what order did we update",
             "the timeline of the migration", "since the migration what changed", "deploy the daemon"])] + [
            case(f"temporal top_k 2 {i}", q(q=t, top_k=2), auth) for i, t in enumerate(
                ["when did the session step happen", "what happened before the deploy runbook",
                 "in what order did the session steps go"])]


class Contiguity(Scenario):
    name = "contiguity"
    config_yaml = "memory:\n  search:\n    contiguity_neighbors: 2\n"

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"neighbours {i}", q(q=t, top_k=4), auth) for i, t in enumerate(
            ["first we backed up the bank", "clients were updated", "bearer token", "deploy the daemon",
             "restarting the bench Postgres"])]


class RrfPool(Scenario):
    name = "rrf-pool"
    config_yaml = ("memory:\n  search:\n    fusion: rrf\n    candidate_pool_multiplier: 3\n"
                   "  reranker:\n    enabled: true\n    top_n: 40\n")

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"rrf {i}", q(q=t, top_k=6), auth) for i, t in enumerate(CRAFTED[:6] + QUERIES[:4])]


class MarginGate(Scenario):
    name = "margin-gate"
    config_yaml = "memory:\n  reranker:\n    enabled: true\n    skip_margin: 0.05\n"

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"margin {i}", q(q=t, top_k=5), auth) for i, t in enumerate(CRAFTED[:8])]


class LogOff(Scenario):
    name = "log-off"
    config_yaml = "memory:\n  retrieval_log:\n    enabled: false\n  cortex:\n    enabled: false\n"

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"quiet {i}", q(q=t), auth) for i, t in enumerate(CRAFTED[:4])]


class LogOffControl(Scenario):
    """The default config again, few cases: the event-log mutant's catcher."""
    name = "log-off-control"

    def cases(self):
        auth = [bearer(TOKEN)]
        return [case(f"logged {i}", q(q=t), auth) for i, t in enumerate(CRAFTED[:3])]


SCENARIOS = {s.name: s for s in (Default, Timeline, Contiguity, RrfPool, MarginGate, LogOff,
                                  LogOffControl)}


# ---- comparison ----------------------------------------------------------------------

def kind(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    return type(v).__name__


def diff(a, b, path="", tol_keys=("score",), tol_all=False) -> list[str]:
    """Strict by-value diff: types must match (True is not 1), floats exact
    except under the declared tolerance."""
    if kind(a) != kind(b) and not (tol_all and {kind(a), kind(b)} <= {"int", "float"}):
        return [f"{path}: python {kind(a)} {json.dumps(a)[:160]} vs rust {kind(b)} {json.dumps(b)[:160]}"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}.{k}: only in python")
            elif k not in a:
                out.append(f"{path}.{k}: only in rust")
            else:
                out += diff(a[k], b[k], f"{path}.{k}", tol_keys, tol_all)
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}: length python {len(a)} vs rust {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]", tol_keys, tol_all)]
    if isinstance(a, float) or isinstance(b, float):
        leaf = path.rsplit(".", 1)[-1]
        if tol_all or leaf in tol_keys:
            return [] if abs(float(a) - float(b)) <= SCORE_TOL else [f"{path}: python {a} vs rust {b}"]
    return [] if a == b else [f"{path}: python {json.dumps(a)[:160]} vs rust {json.dumps(b)[:160]}"]


def align_ties(py_list: list, rs_list: list, key: str = "id") -> tuple[list, list[str]]:
    """Reorder the Rust list to Python's where the only difference is a swap
    among entries whose scores are within SCORE_TOL; report each swap."""
    notes = []
    if len(py_list) != len(rs_list):
        return rs_list, notes
    rs = list(rs_list)
    for i, want in enumerate(py_list):
        if not isinstance(want, dict) or i >= len(rs) or rs[i].get(key) == want.get(key):
            continue
        j = next((j for j in range(i + 1, len(rs)) if rs[j].get(key) == want.get(key)), None)
        if j is None:
            continue
        span = [x.get("score", 0.0) for x in rs[i:j + 1]]
        if max(span) - min(span) <= SCORE_TOL:
            rs.insert(i, rs.pop(j))
            notes.append(f"tie swap at {i}: {want.get(key)}")
    return rs, notes


def compare(py: dict, rs: dict) -> tuple[list[str], list[str]]:
    diffs, notes = [], []
    if py["status"] != rs["status"]:
        diffs.append(f"status: python {py['status']} vs rust {rs['status']}")
    if py["headers"].get("content-type") != rs["headers"].get("content-type"):
        diffs.append(f"content-type: {py['headers'].get('content-type')} vs {rs['headers'].get('content-type')}")
    a, b = py.get("json"), rs.get("json")
    if a is None or b is None:
        return diffs + ([] if py.get("bytes") == rs.get("bytes") else ["body bytes differ"]), notes
    if py["status"] in (400, 500) and isinstance(a.get("error"), str) and isinstance(b.get("error"), str):
        a = dict(a, error="<free>")
        b = dict(b, error="<free>")
    if isinstance(a, dict) and isinstance(b, dict):
        for k in ("entries", "cortex"):
            if isinstance(a.get(k), list) and isinstance(b.get(k), list):
                key = "id" if k == "entries" else "value"
                b = dict(b)
                b[k], n = align_ties(a[k], b[k], key)
                notes += [f"{k}: {x}" for x in n]
    diffs += diff(a, b)
    return diffs, notes


# ---- bank state -----------------------------------------------------------------------

def scrub(state: dict) -> list[str]:
    declared = []
    for kind_, table, key, why in DECLARED:
        t = state["rows"].get(f"public.{table}")
        if not t:
            continue
        keep = [r for r in t["rows"] if kind_ == "row" and r[0] != key]
        if len(keep) != len(t["rows"]):
            declared.append(f"{table}{'.' + key if key else ''}: {why}")
            t["rows"] = keep
    return declared


def bank_diff(py_state: dict, rs_state: dict) -> list[str]:
    out = []
    ev = "public.retrieval_events"
    a = py_state["rows"].pop(ev, None)
    b = rs_state["rows"].pop(ev, None)
    if (a is None) != (b is None):
        out.append(f"{ev}: present on one side only")
    elif a is not None:
        cols = a["columns"]
        if len(a["rows"]) != len(b["rows"]):
            out.append(f"{ev}: python {len(a['rows'])} rows vs rust {len(b['rows'])}")
        for ra, rb in zip(a["rows"], b["rows"]):
            for c, x, y in zip(cols, ra, rb):
                if c == "created_at":
                    if (x is None) != (y is None):
                        out.append(f"{ev}[{ra[0]}].{c}: null on one side")
                    continue
                if c == "served" and isinstance(x, list) and isinstance(y, list):
                    y, notes = align_ties(x, y, "entry_id")
                    if notes:
                        # A tie swap renumbers `rank` (the served position).
                        x = [{k: v for k, v in r.items() if k != "rank"} for r in x]
                        y = [{k: v for k, v in r.items() if k != "rank"} for r in y]
                out += diff(x, y, f"{ev}[{ra[0]}].{c}", tol_all=c in ("served", "params", "served_facts"))
    out += dbstate.diff(py_state, rs_state)
    return out


# ---- running --------------------------------------------------------------------------

def rust_env(extra: dict[str, str]) -> dict[str, str]:
    out = dict(extra)
    for key in ("ORT_DYLIB_PATH", "PSEUDOLIFE_DAEMON_ONNX_DIR", "PSEUDOLIFE_DAEMON_RERANK_DIR",
                "PSEUDOLIFE_DAEMON_MUTANT", "PSEUDOLIFE_DAEMON_ORT_THREADS"):
        if os.environ.get(key):
            out[key] = os.environ[key]
    return out


def common_env(dsn: str) -> dict[str, str]:
    return {"PSEUDOLIFE_RELEASE_CHECK": "0", "PSEUDOLIFE_MCP_TOKEN": TOKEN,
            "PSEUDOLIFE_MCP_DATABASE_URL": dsn}


def seed_template(name: str, paragraphs: int) -> None:
    dsn = pg.create(name)
    home = daemons.make_home(daemons.scratch_root(), "w2d-seed", None)
    env = daemons.base_env(home, {"PSEUDOLIFE_MCP_DATABASE_URL": dsn})
    r = subprocess.run([sys.executable, str(HERE / "w2d_seed.py"), str(paragraphs)],
                       env=dict(env, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"seeding failed: {r.stderr[-3000:]}")
    print(r.stdout.strip(), flush=True)


def coverage(responses: list[dict], state: dict) -> dict:
    """What the oracle actually exercised, so a green run cannot be vacuous."""
    cov = {"with_entries": 0, "with_events": 0, "with_cortex": 0, "via": {}, "channels": {},
           "rerank_fired": 0, "rerank_skips": {}, "timeline_fired": 0, "events_rows": 0}
    for r in responses:
        body = r.get("json") or {}
        if not isinstance(body, dict):
            continue
        cov["with_entries"] += bool(body.get("entries"))
        cov["with_events"] += bool(body.get("events"))
        cov["with_cortex"] += bool(body.get("cortex"))
        for e in body.get("entries") or []:
            if e.get("via"):
                cov["via"][e["via"]] = cov["via"].get(e["via"], 0) + 1
    ev = state["rows"].get("public.retrieval_events")
    if ev:
        cols = ev["columns"]
        for row in ev["rows"]:
            cov["events_rows"] += 1
            rec = dict(zip(cols, row))
            for s_ in rec.get("served") or []:
                ch = (s_.get("components") or {}).get("channel")
                cov["channels"][ch] = cov["channels"].get(ch, 0) + 1
            rr = (rec.get("params") or {}).get("reranker") or {}
            cov["rerank_fired"] += bool(rr.get("fired"))
            if rr.get("skip_reason"):
                cov["rerank_skips"][rr["skip_reason"]] = cov["rerank_skips"].get(rr["skip_reason"], 0) + 1
            cov["timeline_fired"] += bool(((rec.get("params") or {}).get("timeline") or {}).get("fired"))
    return cov


def run_scenario(scn: Scenario, binary: Path, template: str, mode: str, record: bool) -> dict:
    tag = scn.name.replace("-", "_")
    dbs = {"python": f"pl_cf_w2d_{tag}_py", "rust": f"pl_cf_w2d_{tag}_rs"}
    sides = ("python", "rust") if mode != "golden" else ("rust",)
    dsns = {k: pg.create(dbs[k], template=template) for k in sides}
    procs = {}
    root = daemons.scratch_root()
    try:
        if "python" in sides:
            home = daemons.make_home(root, f"w2d-{tag}-py", scn.config_yaml)
            procs["python"] = daemons.python_daemon(home, daemons.free_port(),
                                                    daemons.base_env(home, common_env(dsns["python"])))
        home = daemons.make_home(root, f"w2d-{tag}-rs", scn.config_yaml)
        procs["rust"] = daemons.rust_daemon(binary, home, daemons.free_port(),
                                            daemons.base_env(home, rust_env(common_env(dsns["rust"]))))
        for d in procs.values():
            d.start(300)
        wait_settled([d.port for d in procs.values()], timeout=900)
        golden = json.loads((GOLDENS / f"{scn.name}.json").read_text()) if mode == "golden" else None
        rows = []
        py_seen = []
        for i, c in enumerate(scn.cases()):
            py_r = (call(procs["python"].port, "GET", c["path"], c["headers"]) if "python" in procs
                    else golden["responses"][i])
            rs_r = call(procs["rust"].port, "GET", c["path"], c["headers"])
            py_seen.append(py_r)
            diffs, notes = compare(py_r, rs_r)
            rows.append({"case": c["name"], "path": c["path"][:140], "python_status": py_r["status"],
                         "rust_status": rs_r["status"], "diffs": diffs, "notes": notes,
                         "_python": py_r if record else None})
    finally:
        for d in procs.values():
            d.stop()
    states = {}
    declared = {}
    for side in sides:
        st = dbstate.normalize(dbstate.dump(pg.dsn(dbs[side])), CLOCK_COLUMNS)
        declared[side] = scrub(st)
        states[side] = st
    if mode == "golden":
        py_state = golden["db_state"]
    else:
        py_state = states["python"]
    cov = coverage(py_seen, py_state)
    db_diffs = bank_diff(json.loads(json.dumps(py_state)), states["rust"])
    if record:
        GOLDENS.mkdir(parents=True, exist_ok=True)
        (GOLDENS / f"{scn.name}.json").write_text(json.dumps({
            "rules": {"score_tol": SCORE_TOL, "clock_columns": sorted(f"{t}.{c}" for t, c in CLOCK_COLUMNS),
                      "declared": [d[3] for d in DECLARED]},
            "responses": [r.pop("_python") for r in rows],
            "db_state": states["python"]}, indent=1, sort_keys=True), encoding="utf-8")
    for r in rows:
        r.pop("_python", None)
    for side in sides:
        pg.drop(dbs[side])
    return {"scenario": scn.name, "cases": rows, "db_diffs": db_diffs, "declared": declared,
            "coverage": cov}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("live", "golden", "mutants"))
    ap.add_argument("--rust-bin", type=Path, required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--paragraphs", type=int, default=150)
    ap.add_argument("--reuse-template", action="store_true")
    args = ap.parse_args()
    names = args.only or list(SCENARIOS)
    template = "pl_cf_w2d_template"
    if args.mode != "golden" and not args.reuse_template:
        seed_template(template, args.paragraphs)
    if args.mode == "mutants":
        outcome = {}
        for m, scn in MUTANTS.items():
            if args.only and scn not in args.only:
                continue
            os.environ["PSEUDOLIFE_DAEMON_MUTANT"] = m
            res = [run_scenario(SCENARIOS[scn](), args.rust_bin, template, "live", False)]
            n_diff = sum(1 for r in res for c in r["cases"] if c["diffs"]) + sum(1 for r in res if r["db_diffs"])
            outcome[m] = n_diff
            print(f"mutant {m}: {n_diff} diffs", flush=True)
        os.environ.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
        survivors = [m for m, n in outcome.items() if n == 0]
        print(json.dumps({"mutants": outcome, "survivors": survivors}, indent=1))
        return 1 if survivors else 0
    results = [run_scenario(SCENARIOS[n](), args.rust_bin, template, args.mode, args.record) for n in names]
    n_cases = sum(len(r["cases"]) for r in results)
    n_diff = 0
    for r in results:
        for c in r["cases"]:
            if c["diffs"]:
                n_diff += 1
                print(f"DIFF [{r['scenario']}] {c['case']}: {c['diffs'][:5]}")
            for n in c["notes"]:
                print(f"note [{r['scenario']}] {c['case']}: {n}")
        for d in r["db_diffs"][:20]:
            print(f"DB DIFF [{r['scenario']}] {d}")
        print(f"[{r['scenario']}] declared python-only writes: {r['declared'].get('python')}")
        print(f"[{r['scenario']}] coverage: {json.dumps(r['coverage'])}")
    db = sum(1 for r in results if r["db_diffs"])
    summary = {"scenarios": len(results), "cases": n_cases, "case_diffs": n_diff, "db_diff_scenarios": db}
    print(json.dumps(summary))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=1), encoding="utf-8")
    return 1 if (n_diff or db) else 0


if __name__ == "__main__":
    raise SystemExit(main())
