"""Regression check of the cortex-first search block (slice W2-D cortex).

usage: python w2d_cortex_check.py [--mode NAME ...] [--mutant NAME] [--keep]

Replays a pinned seed and per-mode query observations captured from the
frozen Python harness. The ignored Rust test receives the captured query
vectors, event ids, clock and ranking parameters.

Comparison rules:

* entries are compared by value, exactly (scores are rounded to 4 dp on
  both sides and the query vector is shared);
* time is frozen: the candidate receives the observation's clock, so
  ``age``, ``effective_confidence``,
  ``stale`` and ``slot_reads.last_read_at`` are deterministic;
* bank state: every table is dumped (``dbstate``) before and after the
  queries. The change set must match the captured one, and the final
  ``slot_reads`` and ``retrieval_events`` tables must match row for row.

Exit status: 0 only when every mode has zero entry diffs and zero state
diffs. ``--mutant NAME`` builds Rust with ``--features mutants`` and sets
``PSEUDOLIFE_DAEMON_MUTANT``; that run must exit non-zero.

Every database this script creates, clones or drops must match
the process-owned randomized ``pl_cf_w2d_*`` namespace; foreign names
are refused before a connection opens. Logs in only with the test login
(``~/.pseudolife-mcp/test-pg.env``). Needs ``CARGO_TARGET_DIR`` set by the
caller; builds with ``-j 2``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_DATABASE_TURN_MARKER = Path.home() / ".pseudolife-mcp/locks/test-postgres.turn-enabled"

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
import dbstate  # noqa: E402

import pgdisposable  # noqa: E402
from w2d_seed_rows import load_capture, replay  # noqa: E402

pg = pgdisposable.Slice("w2d")

SEED = pg.name("pl_cf_w2d_cortex_seed")
RUST_TEST = "read::cortex_search::tests::differential_cases_from_harness"
CAPTURE_SHA256 = "582830a372b398cc67abac0ca2c176f090410035bc638b4f5a7acefe29621db4"

BM25 = {"k1": 1.5, "b": 0.75, "weight": 0.3, "top_n": 20, "min_score": 0.1}
MODES = {
    # The shipped defaults: annotate, cortex BM25 off, pins on.
    "default": {"stale_policy": "annotate", "bm25": None},
    # Cortex BM25 on (memory.bm25.cortex_enabled) and stale facts demoted.
    "bm25_demote": {"stale_policy": "demote", "bm25": BM25},
    # Stale values quarantined behind the wrapper.
    "quarantine": {"stale_policy": "quarantine", "bm25": None},
}

QUERIES = [
    "where is the payments database hosted",
    "payments-db port",
    "what port does payments db listen on",
    "how do I start the bench server?",
    "the bench server's config",
    "bench server gpu",
    "what languages is the project written in",
    "project license",
    "deploy procedure",
    "is the daemon healthy",
    "daemon deployment status",
    "what is the latest release version",
    "which CI runner do we use",
    "editor theme",
    "when is the team standup",
    "I don't know who maintains this",
    "Don role",
    "payments db replicas",
    "migration rules for payments-db",
    "library pinned version",
    "quantum chromodynamics lattice gauge theory",
    "zeta_identifier_9 lookup",
    "PAYMENTS DB write rule",
    "payments-database analytics access",
    "what is probably true about the cache",
    "which platforms does the release support",
]


# ---- disposable databases ------------------------------------------------------------

# Shared helpers refuse foreign names before connecting and enumerate only
# this process's randomized names, including its seed template.
create = pg.create
drop = pg.drop
dsn = pg.dsn
existing = pg.existing


# ---- candidate execution -------------------------------------------------------------

def run_rust(mode: str, rs_dsn: str, work: Path, frozen: float, cases: list[dict],
             mutant: str | None) -> tuple[list[dict], dict, dict]:
    inp = work / f"rs_{mode}_in.json"
    out = work / f"rs_{mode}_out.json"
    knobs = {"pin_constraints": True, "read_tracking": True, "traces_enabled": True,
             "stale_policy": MODES[mode]["stale_policy"], "bm25": MODES[mode]["bm25"]}
    inp.write_text(json.dumps({
        "now": frozen, "knobs": knobs,
        "cases": [{k: c[k] for k in ("query", "vec", "event_id", "top_k", "min_score")}
                  for c in cases]}), encoding="utf-8")
    before = dbstate.dump(rs_dsn)
    env = dict(os.environ, W2D_CORTEX_DSN=rs_dsn, W2D_CORTEX_IN=str(inp),
               W2D_CORTEX_OUT=str(out))
    cmd = ["cargo", "test", "--locked", "-j", "2", "-p", "pseudolife-daemon",
           "--manifest-path", str(REPO / "rust" / "Cargo.toml")]
    if mutant:
        cmd += ["--features", "mutants"]
        env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
    else:
        env.pop("PSEUDOLIFE_DAEMON_MUTANT", None)
    cmd += ["--", "--ignored", "--exact", RUST_TEST]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
        raise SystemExit(f"rust side failed (exit {proc.returncode}):\n{tail}")
    results = json.loads(out.read_text(encoding="utf-8"))["results"]
    after = dbstate.dump(rs_dsn)
    return results, before, after


# ---- comparison -----------------------------------------------------------------------

def value_diff(a, b, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}/{k}: only in python ({json.dumps(a[k])[:120]})")
            elif k not in a:
                out.append(f"{path}/{k}: only in rust ({json.dumps(b[k])[:120]})")
            else:
                out += value_diff(a[k], b[k], f"{path}/{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: python has {len(a)} items, rust {len(b)}: "
                    f"{json.dumps(a)[:300]} vs {json.dumps(b)[:300]}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += value_diff(x, y, f"{path}[{i}]")
        return out
    if a != b or type(a) is bool and type(b) is not bool or type(b) is bool and type(a) is not bool:
        return [f"{path}: python {json.dumps(a)[:200]} vs rust {json.dumps(b)[:200]}"]
    return []


def _rowset(state: dict, table: str) -> set[str]:
    t = state["rows"].get(table, {"rows": []})
    return {json.dumps(r, sort_keys=True) for r in t["rows"]}


def state_delta(before: dict, after: dict) -> dict:
    out = {}
    for table in sorted(set(before["rows"]) | set(after["rows"])):
        b, a = _rowset(before, table), _rowset(after, table)
        if a - b or b - a:
            out[table] = {"added": sorted(a - b), "removed": sorted(b - a)}
    return out


def state_diffs(reference, rs_before, rs_after) -> list[str]:
    diffs = []
    dp, dr = reference["delta"], state_delta(rs_before, rs_after)
    for table in sorted(set(dp) | set(dr)):
        p = dp.get(table, {"added": [], "removed": []})
        r = dr.get(table, {"added": [], "removed": []})
        for kind in ("added", "removed"):
            for row in sorted(set(p[kind]) - set(r[kind]))[:5]:
                diffs.append(f"{table}: row {kind} only by python: {row[:300]}")
            for row in sorted(set(r[kind]) - set(p[kind]))[:5]:
                diffs.append(f"{table}: row {kind} only by rust: {row[:300]}")
    for table in ("public.slot_reads", "public.retrieval_events"):
        if reference["terminal"][table] != rs_after["rows"].get(table):
            diffs.append(f"{table}: final rows differ")
    return diffs


def coverage(cases: list[dict]) -> dict[str, int]:
    """How many captured entries exercised each contract branch, so a
    green run cannot hide a seed that stopped reaching one."""
    out = {k: 0 for k in ("set", "pinned", "pinned_unranked", "re_verify", "contested",
                          "assistant", "stale", "warning", "quarantined", "stance",
                          "source_entries", "empty_blocks")}
    for c in cases:
        if not c["entries"]:
            out["empty_blocks"] += 1
        for e in c["entries"]:
            out["set"] += e.get("kind") == "set"
            out["pinned"] += bool(e.get("pinned"))
            out["re_verify"] += bool(e.get("re_verify"))
            out["contested"] += bool(e.get("contested"))
            out["assistant"] += e.get("origin") == "assistant"
            out["stale"] += bool(e.get("stale"))
            out["warning"] += "warning" in e
            out["quarantined"] += "last_known_value" in e
            out["stance"] += "stance" in e
            out["source_entries"] += bool(e.get("source_entries"))
        ranked = {tuple(s) for s in c.get("ranked_slots", [])}
        out["pinned_unranked"] += sum(1 for e in c["entries"] if e.get("pinned")
                                      and (e["entity"], e["attribute"]) not in ranked)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", action="append", choices=sorted(MODES))
    ap.add_argument("--mutant")
    ap.add_argument("--keep", action="store_true", help="keep the databases")
    args = ap.parse_args()
    if _DATABASE_TURN_MARKER.exists():
        _turn_sys = __import__("sys")
        _turn_root = str(__import__("pathlib").Path(__file__).resolve().parents[3])
        _turn_sys.path.insert(0, _turn_root)
        try:
            from rust.harness_support.database_turn import require_turn

            require_turn(holder="standalone database harness", contain=True)
        finally:
            _turn_sys.path.remove(_turn_root)
    if not os.environ.get("CARGO_TARGET_DIR"):
        raise SystemExit("set CARGO_TARGET_DIR")
    if pg.base_database() is not None:
        ap.error("the fixed cortex capture requires an empty bank; unset PL_HARNESS_BASE_DATABASE")
    fixture = load_capture("cortex", CAPTURE_SHA256)
    modes = args.mode or list(MODES)
    work = Path(tempfile.mkdtemp(prefix="w2d_cortex_",
                                 dir=os.environ.get("PSEUDOLIFE_MCP_DATA_DIR")))
    created = [SEED]
    total_cases = total_diffs = total_state = 0
    try:
        replay(create(SEED), fixture["bank"])
        frozen = fixture["at"]
        for mode in modes:
            rs_name = pg.name(f"pl_cf_w2d_cortex_rs_{mode}")
            created.append(rs_name)
            rs_dsn = create(rs_name, SEED)
            reference = fixture["modes"][mode]
            cases = reference["cases"]
            if [case["query"] for case in cases] != QUERIES:
                raise ValueError(f"{mode}: captured cortex queries differ from the harness corpus")
            results, rs_before, rs_after = run_rust(mode, rs_dsn, work, frozen, cases,
                                                    args.mutant)
            diffs = []
            if len(cases) != len(results):
                diffs.append(f"result count: python {len(cases)} vs rust {len(results)}")
            served = 0
            for c, r in zip(cases, results):
                served += len(c["entries"])
                for d in value_diff(c["entries"], r["entries"], f"[{c['query']}]"):
                    diffs.append(d)
            sdiffs = state_diffs(reference, rs_before, rs_after)
            compared = min(len(cases), len(results))
            total_cases += compared
            total_diffs += len(diffs)
            total_state += len(sdiffs)
            written = {t: len(v["added"]) + len(v["removed"]) for t, v in reference["delta"].items()}
            print(f"mode {mode}: coverage {json.dumps(coverage(cases))}")
            print(f"mode {mode}: {compared}/{len(cases)} cases compared, {served} served facts, "
                  f"{len(diffs)} entry diffs, {len(sdiffs)} state diffs; "
                  f"captured writes {written}")
            for d in (diffs + sdiffs)[:40]:
                print("  DIFF", d)
    finally:
        if not args.keep:
            for name in created:
                drop(name)
    print(f"total: {total_cases} cases, {total_diffs} entry diffs, "
          f"{total_state} state diffs" + (f" (mutant {args.mutant})" if args.mutant else ""))
    return 1 if (total_diffs or total_state) else 0


if __name__ == "__main__":
    raise SystemExit(main())
