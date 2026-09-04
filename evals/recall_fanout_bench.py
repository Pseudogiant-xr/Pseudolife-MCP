"""Does capping ``memory_recall``'s search fan-out cost it any answers?

Read-only, CPU-only, against a RESTORED copy of the bank (never the live
bank and never the shared bench DB). One arm per invocation:

``--arm before``
    the walk as shipped — one re-query per newly discovered entity per
    hop, no ceiling. On a star-shaped graph that is a mean of 89.15
    ``service.search`` calls per recall and 205 on the worst question
    (measured 2026-09-04 by this harness on a restored live-bank copy,
    1,296 entries / 5,504 entities — ``results/recall-fanout-cap-
    20260904.json``).

``--arm after``
    the same walk under ``memory.recall.max_searches_per_hop`` /
    ``max_total_searches`` / ``time_budget_seconds``.

Both arms run the identical question set and record, per question: how
many ``service.search`` calls the walk issued, wall time, served
characters, whether the expected entity surfaced, and how the added
entities arrived (hub / ``part-of`` / domain relation). ``--combine``
pairs two arm files into the committed artifact, including the honesty
check: every expected target the BEFORE arm found and the AFTER arm
lost.

The pure helpers (``served_chars_*``, ``classify_arrivals``,
``expected_hit_*``, ``percentile``) are copied from
``evals/graph_ablation.py`` so the two harnesses score the same way.

    python evals/recall_fanout_bench.py --arm before \\
        --dsn postgresql://.../pseudolife_memory_replay_YYYYMMDD \\
        --out evals/results/recall-fanout-before.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# Imported eagerly and by name so a long BEFORE run cannot pick up an
# edit made to the package while it is still walking.
import pseudolife_memory.memory.recall  # noqa: E402,F401
import pseudolife_memory.service  # noqa: E402,F401

RESULTS = Path(__file__).resolve().parent / "results"
FORBIDDEN_DBS = {"pseudolife_memory", "pseudolife_memory_bench"}

# Twenty relational questions. The first twelve are exactly the slice the
# 2026-09-04 graph-ablation probe ran (evals/graph_ablation.py, landing
# separately: ``sample_evenly(RELATIONAL_QUESTIONS, 12)`` — indices
# 0,2,5,7,10,12,15,17,20,22,25,27 of its thirty), so the before/after
# here is comparable to that run;
# the last eight are new, same construction (each needs an edge to
# answer, and every ``expect`` is a string that also occurs in the
# tracked repo tree, so the artifact leaks nothing).
QUESTIONS: list[dict[str, str]] = [
    {"q": "what does the pseudolife daemon depend on", "expect": "daemon"},
    {"q": "what process owns the memory bank volumes", "expect": "pseudolife-daemon"},
    {"q": "where does the backup script write its artifacts", "expect": "ops-backup-ps1"},
    {"q": "which service does the Cortex Console talk to", "expect": "cortex-console"},
    {"q": "which component owns the cortex fact store", "expect": "cortex"},
    {"q": "what does the codex shim connect to", "expect": "codex-shim"},
    {"q": "what does service.py implement", "expect": "service-py"},
    {"q": "what hosts the postgres container", "expect": "postgres"},
    {"q": "what does memory_search depend on", "expect": "memory-search"},
    {"q": "what does the Sonnet shim implement", "expect": "evals-sonnet-shim-py"},
    {"q": "which eval run used chip12-b16", "expect": "chip12-b16"},
    {"q": "what does the BEAM 100k run measure", "expect": "beam-100k-run"},
    # New for this bench (2026-09-04).
    {"q": "what does the regression gate compare against", "expect": "regression-gate"},
    {"q": "which runtime serves the qwen bench model", "expect": "llama-cpp"},
    {"q": "what is the system atlas built from", "expect": "system-atlas"},
    {"q": "what does the review queue hold", "expect": "review-queue"},
    {"q": "which GPU runs the local evals", "expect": "4090"},
    {"q": "what does the CHANGELOG track for each release", "expect": "changelog-md"},
    {"q": "where do the container images get published", "expect": "ghcr"},
    {"q": "what does memory_recall walk over", "expect": "memory-recall"},
]


def _git_head() -> str:
    """Short commit of the checkout being measured, plus a dirty marker.
    Never a path — an absolute path here embeds the OS username."""
    import subprocess  # noqa: PLC0415
    try:
        repo = str(Path(__file__).resolve().parents[1])
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=repo, capture_output=True, text=True,
                              timeout=15)
        if head.returncode != 0:
            return "(unknown)"
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                               capture_output=True, text=True, timeout=30)
        suffix = "+dirty" if (dirty.stdout or "").strip() else ""
        return head.stdout.strip() + suffix
    except Exception:  # noqa: BLE001 — provenance is best-effort
        return "(unknown)"


def guard_dsn(dsn: str) -> None:
    db = re.sub(r"\?.*$", "", dsn).rsplit("/", 1)[-1]
    if db in FORBIDDEN_DBS:
        sys.exit(f"refusing to run against {db!r} — restore a dedicated "
                 "replay copy instead (see the module docstring)")


# ══════════════════════════════════════════════════════════════════════════
# pure helpers (copied from evals/graph_ablation.py — same scoring)
# ══════════════════════════════════════════════════════════════════════════

def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, int(round(pct / 100.0 * (len(xs) - 1)))))
    return float(xs[idx])


def served_chars_search(res: dict[str, Any]) -> int:
    return sum(len(e.get("text") or "") for e in res.get("entries", []))


def served_chars_recall(res: dict[str, Any]) -> int:
    n = sum(len(t if isinstance(t, str) else (t.get("text") or ""))
            for t in res.get("texts", []))
    for ent in res.get("entities", []):
        n += len(ent.get("entity") or "")
        for f in ent.get("facts", []):
            n += len(str(f.get("attribute", ""))) + len(str(f.get("value", "")))
    for ed in res.get("edges", []):
        n += sum(len(str(ed.get(k, ""))) for k in ("src", "relation", "dst"))
    return n


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def expected_hit_search(res: dict[str, Any], expect: str) -> bool:
    needle = _norm(expect.replace("-", " "))
    return any(needle in _norm(e.get("text") or "")
               for e in res.get("entries", []))


def hit_channel_recall(res: dict[str, Any], expect: str) -> str | None:
    """WHICH channel carried the expected target: the entity set, or the
    supporting ``texts``. Load-bearing for reading ``targets_lost``: the
    caps leave graph expansion alone, so the entity sets are identical
    before and after by construction and only a ``texts``-borne target
    could ever be lost. A run where every target arrived on ``entity`` is
    a run where the honesty check had no way to fail, and the artifact
    should say so rather than claim a clean bill of health."""
    if any(_norm(expect) == _norm(e.get("entity") or "")
           for e in res.get("entities", [])):
        return "entity"
    if expected_hit_search({"entries": [
            {"text": t if isinstance(t, str) else (t.get("text") or "")}
            for t in res.get("texts", [])]}, expect):
        return "texts"
    return None


def expected_hit_recall(res: dict[str, Any], expect: str) -> bool:
    return hit_channel_recall(res, expect) is not None


def classify_arrivals(seeds: list[str], entities: list[str],
                      edges: list[dict[str, Any]], degrees: dict[str, int],
                      hub_threshold: float) -> dict[str, Any]:
    seed_set = set(seeds)
    added = [e for e in entities if e not in seed_set]
    touching: dict[str, list[dict[str, Any]]] = {n: [] for n in added}
    for ed in edges:
        for side in ("src", "dst"):
            n = ed.get(side)
            if n in touching:
                touching[n].append(ed)
    out = {"added": len(added), "via_part_of": 0, "via_domain": 0,
           "via_hub": 0, "unlinked": 0}
    for n in added:
        eds = touching[n]
        if not eds:
            out["unlinked"] += 1
            continue
        rels = {e.get("relation") for e in eds}
        if rels == {"part-of"}:
            out["via_part_of"] += 1
        else:
            out["via_domain"] += 1
        hub_side = any(
            degrees.get(e.get("src"), 0) >= hub_threshold
            or degrees.get(e.get("dst"), 0) >= hub_threshold for e in eds)
        if degrees.get(n, 0) >= hub_threshold or hub_side:
            out["via_hub"] += 1
    return out


# ══════════════════════════════════════════════════════════════════════════
# arm
# ══════════════════════════════════════════════════════════════════════════

def build_service(dsn: str, config_path: str | None):
    from pseudolife_memory.service import MemoryService  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="rfan_"))
    if config_path:
        shutil.copyfile(config_path, tmp / "config.yaml")
    svc = MemoryService(data_dir=str(tmp), database_url=dsn)
    svc.config.embedding.device = "cpu"
    svc.config.memory.retrieval_log.enabled = False
    return svc


def apply_arm(svc, arm: str, caps: dict[str, Any]) -> dict[str, Any]:
    """Set (or explicitly disable) the fan-out caps for this arm.

    On the pre-change package the knobs do not exist at all, so the
    BEFORE arm is the shipped walk by construction; on the changed
    package BEFORE sets each knob to its off value, which the
    byte-identity test pins as the same walk.
    """
    cfg = svc.config.memory.recall
    applied: dict[str, Any] = {}
    for name, off_value in (("max_searches_per_hop", 0),
                            ("max_total_searches", 0),
                            ("time_budget_seconds", 0.0),
                            ("skip_part_of_expansion", False)):
        if not hasattr(cfg, name):
            applied[name] = "(absent — pre-change package)"
            continue
        value = off_value if arm == "before" else caps.get(name, getattr(cfg, name))
        setattr(cfg, name, value)
        applied[name] = value
    return applied


def run_arm(svc, cases: list[dict[str, Any]], degrees: dict[str, int],
            hub_threshold: float, top_k: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    original_search = svc.search
    for i, c in enumerate(cases):
        q, expect = c["q"], c["expect"]
        t0 = time.perf_counter()
        s = original_search(q, top_k=top_k)
        t1 = time.perf_counter()

        counter = {"n": 0}

        def counted(query, *a, **kw):
            counter["n"] += 1
            return original_search(query, *a, **kw)

        svc.search = counted
        try:
            r = svc.recall(q, top_k=top_k)
        finally:
            svc.search = original_search
        t2 = time.perf_counter()
        ents = [e.get("entity") for e in r.get("entities", [])]
        rows.append({
            "q": q,
            "expect": expect,
            "search": {"wall_s": round(t1 - t0, 4),
                       "served_chars": served_chars_search(s),
                       "expected_hit": expected_hit_search(s, expect)},
            "recall": {"wall_s": round(t2 - t1, 4),
                       "searches_issued": counter["n"],
                       "reported_searches_issued": r.get("searches_issued"),
                       "truncated": bool(r.get("truncated", False)),
                       "served_chars": served_chars_recall(r),
                       "n_entities": len(ents),
                       "n_edges": len(r.get("edges", [])),
                       "n_texts": len(r.get("texts", [])),
                       "iterations": r.get("iterations"),
                       "low_confidence": bool(r.get("low_confidence")),
                       "expected_hit": expected_hit_recall(r, expect),
                       "hit_channel": hit_channel_recall(r, expect),
                       "arrivals": classify_arrivals(
                           r.get("seeds", []), ents, r.get("edges", []),
                           degrees, hub_threshold)},
        })
        print(f"  [{i + 1}/{len(cases)}] searches={counter['n']} "
              f"wall={t2 - t1:.1f}s hit={rows[-1]['recall']['expected_hit']}",
              flush=True)
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1

    def agg(key: str, arm: str = "recall") -> dict[str, Any]:
        xs = sorted(r[arm][key] for r in rows)
        return {"mean": round(sum(xs) / n, 3), "median": xs[len(xs) // 2],
                "max": xs[-1], "total": round(sum(xs), 3)}

    arrivals: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    for r in rows:
        for k, v in (r["recall"].get("arrivals") or {}).items():
            arrivals[k] += v
        channels[str(r["recall"].get("hit_channel"))] += 1
    return {
        # How many expected targets rode each channel — the power of the
        # `targets_lost` check (see hit_channel_recall).
        "hit_channels": dict(channels),
        "n": len(rows),
        "searches_issued": agg("searches_issued"),
        "recall_wall_s": agg("wall_s"),
        "recall_served_chars": agg("served_chars"),
        "search_wall_s": agg("wall_s", "search"),
        "search_served_chars": agg("served_chars", "search"),
        "recall_expected_hits": sum(1 for r in rows
                                    if r["recall"]["expected_hit"]),
        "search_expected_hits": sum(1 for r in rows
                                    if r["search"]["expected_hit"]),
        "truncated_calls": sum(1 for r in rows if r["recall"]["truncated"]),
        "low_confidence_calls": sum(1 for r in rows
                                    if r["recall"]["low_confidence"]),
        "arrivals_total": dict(arrivals),
    }


# ══════════════════════════════════════════════════════════════════════════
# combine
# ══════════════════════════════════════════════════════════════════════════

def combine(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Pair two arm files. The honesty check is ``targets_lost``: an
    expected target the BEFORE arm surfaced and the AFTER arm did not."""
    b_rows = {r["q"]: r for r in before["rows"]}
    a_rows = {r["q"]: r for r in after["rows"]}
    shared = [q for q in b_rows if q in a_rows]
    lost, gained, per_q = [], [], []
    for q in shared:
        b, a = b_rows[q], a_rows[q]
        if b["recall"]["expected_hit"] and not a["recall"]["expected_hit"]:
            lost.append({"q": q, "expect": b["expect"]})
        if a["recall"]["expected_hit"] and not b["recall"]["expected_hit"]:
            gained.append({"q": q, "expect": b["expect"]})
        per_q.append({
            "q": q, "expect": b["expect"],
            "searches_before": b["recall"]["searches_issued"],
            "searches_after": a["recall"]["searches_issued"],
            "wall_s_before": b["recall"]["wall_s"],
            "wall_s_after": a["recall"]["wall_s"],
            "chars_before": b["recall"]["served_chars"],
            "chars_after": a["recall"]["served_chars"],
            "hit_before": b["recall"]["expected_hit"],
            "hit_after": a["recall"]["expected_hit"],
            "truncated_after": a["recall"]["truncated"],
        })
    b_sum, a_sum = before["summary"], after["summary"]
    # What the caps did and did NOT touch. Graph expansion is deliberately
    # left alone — the caps bound the SEARCH budget — so the entity and
    # edge sets should be identical question for question, and the whole
    # saving should land in ``texts``. Counted rather than asserted: if a
    # future change to the walk starts dropping structure, this row moves.
    structural = {
        "questions_with_different_entity_count": sum(
            1 for q in shared
            if b_rows[q]["recall"]["n_entities"]
            != a_rows[q]["recall"]["n_entities"]),
        "questions_with_different_edge_count": sum(
            1 for q in shared
            if b_rows[q]["recall"]["n_edges"]
            != a_rows[q]["recall"]["n_edges"]),
        "questions_with_different_iteration_count": sum(
            1 for q in shared
            if b_rows[q]["recall"]["iterations"]
            != a_rows[q]["recall"]["iterations"]),
        "texts_total_before": sum(b_rows[q]["recall"]["n_texts"]
                                  for q in shared),
        "texts_total_after": sum(a_rows[q]["recall"]["n_texts"]
                                 for q in shared),
    }
    return {
        "bench": "recall fan-out cap (memory.recall.max_*_searches)",
        "date": "2026-09-04",
        "source_db": before.get("source_db"),
        "n_questions": len(shared),
        "question_set": ("12 questions from the 2026-09-04 graph-ablation "
                         "probe's slice (evals/graph_ablation.py, landing "
                         "separately) + 8 written for this bench"),
        "honesty_check": (
            "targets_lost is the check that matters, and its POWER is "
            "bounded: the caps leave graph expansion alone, so the entity "
            "sets are identical before and after (structural_identity) and "
            "only a target carried by `texts` could be lost. Read "
            "hit_channels in each arm's summary — a run where every target "
            "arrived on `entity` is a run the check could not have failed."),
        "before": {"caps": before.get("caps"),
                   "code_commit": before.get("code_commit"), "summary": b_sum},
        "after": {"caps": after.get("caps"),
                  "code_commit": after.get("code_commit"), "summary": a_sum},
        "deltas": {
            "mean_searches": round(a_sum["searches_issued"]["mean"]
                                   - b_sum["searches_issued"]["mean"], 3),
            "max_searches": (a_sum["searches_issued"]["max"]
                             - b_sum["searches_issued"]["max"]),
            "mean_wall_s": round(a_sum["recall_wall_s"]["mean"]
                                 - b_sum["recall_wall_s"]["mean"], 3),
            "mean_served_chars": round(a_sum["recall_served_chars"]["mean"]
                                       - b_sum["recall_served_chars"]["mean"], 1),
            "expected_hits": (a_sum["recall_expected_hits"]
                              - b_sum["recall_expected_hits"]),
        },
        "structural_identity": structural,
        "targets_lost": lost,
        "targets_gained": gained,
        "per_question": per_q,
        "privacy": ("no entity names beyond the question set's own "
                    "``expect`` strings, all of which occur in the tracked "
                    "repo tree; no query or entry text from the bank"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", choices=("before", "after"))
    ap.add_argument("--dsn")
    ap.add_argument("--config", default=None)
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-searches-per-hop", type=int, default=None)
    ap.add_argument("--max-total-searches", type=int, default=None)
    ap.add_argument("--time-budget-seconds", type=float, default=None)
    ap.add_argument("--skip-part-of-expansion", action="store_true")
    ap.add_argument("--code-label", default=None,
                    help="Override the recorded commit — for a BEFORE arm "
                         "run from a `git archive` export of the pre-change "
                         "tree, which has no .git of its own.")
    ap.add_argument("--combine", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.combine:
        b = json.loads(Path(args.combine[0]).read_text(encoding="utf-8"))
        a = json.loads(Path(args.combine[1]).read_text(encoding="utf-8"))
        report = combine(b, a)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in
                          ("n_questions", "deltas", "structural_identity",
                           "targets_lost", "targets_gained")}, indent=2))
        print(f"wrote {out}")
        return 0

    if not args.arm or not args.dsn:
        ap.error("--arm and --dsn are required unless --combine is given")
    guard_dsn(args.dsn)

    caps: dict[str, Any] = {}
    if args.max_searches_per_hop is not None:
        caps["max_searches_per_hop"] = args.max_searches_per_hop
    if args.max_total_searches is not None:
        caps["max_total_searches"] = args.max_total_searches
    if args.time_budget_seconds is not None:
        caps["time_budget_seconds"] = args.time_budget_seconds
    if args.skip_part_of_expansion:
        caps["skip_part_of_expansion"] = True

    svc = build_service(args.dsn, args.config)
    applied = apply_arm(svc, args.arm, caps)
    degrees = svc._graph_degrees()  # noqa: SLF001 — eval reads the same map
    hub_threshold = percentile([float(v) for v in degrees.values()], 95.0)
    cases = QUESTIONS[:args.limit] if args.limit else QUESTIONS
    print(f"arm={args.arm} n={len(cases)} caps={applied}", flush=True)
    t0 = time.perf_counter()
    rows = run_arm(svc, cases, degrees, hub_threshold, args.top_k)
    report = {
        "arm": args.arm,
        # The two arms can run on DIFFERENT checkouts (the before arm on
        # the pre-cap package, where the knobs do not exist), so the
        # commit is the load-bearing provenance for the pair.
        "code_commit": args.code_label or _git_head(),
        "source_db": re.sub(r"\?.*$", "", args.dsn).rsplit("/", 1)[-1],
        "top_k": args.top_k,
        "caps": applied,
        "hub_degree_p95": hub_threshold,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "summary": summarize(rows),
        "rows": rows,
    }
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, default=str))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
