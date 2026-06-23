#!/usr/bin/env python
"""Seed-selection tuning bench (dev-only).

Compares mechanical seed-selection heuristics for memory_recall on a corpus with
CROSS-TALK snippets (co-mentioning unrelated entities) that reproduce the live
"liberal seeder" noise. Objective: max seed precision at ZERO answer-recall loss.

Variants: v0 (current: query+hits), A (query-first), A+B (query-first + degree
filter), C (ranked+capped). Optional LLM arm measures the served Gemma E2B seed
call's real latency (the perf comparison) — skipped if no endpoint.

Isolation: dedicated pseudolife_memory_bench DB, CPU only, live bank untouched.
See docs/specs/2026-06-23-recall-seed-tuning-design.md.
"""
from __future__ import annotations

import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ladder_sweep import build_service  # noqa: E402
from pseudolife_memory.memory.recall import _mentions, run_recall  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# ---------------------------------------------------------------------------
# Corpus: multi-hop chains (give edges + the snippets that imply them) + edge-
# less entities + CROSS-TALK snippets that co-mention entities across chains
# (the live noise source) + pure distractors.
# ---------------------------------------------------------------------------
CHAINS = [
    # (subject, [(src, rel, dst), ...], gold)
    ("checkout-svc", [("checkout-svc", "depends-on", "billing-lib"),
                      ("billing-lib", "runs-on", "jdk-21")], "jdk-21"),
    ("order-svc", [("order-svc", "part-of", "commerce-suite"),
                   ("commerce-suite", "runs-on", "nomad-cluster")], "nomad-cluster"),
    ("web-portal", [("web-portal", "uses", "gateway-proxy"),
                    ("gateway-proxy", "stores-data-in", "vault-kms")], "vault-kms"),
    ("report-svc", [("report-svc", "runs-on", "jvm-17")], "jvm-17"),
]

# Natural-language snippets implying each edge (so search has real text + the
# edge endpoints co-occur, as a real extractor would have produced them).
CHAIN_SNIPPETS = [
    "The checkout-svc depends on the billing-lib package.",
    "The billing-lib package targets jdk-21 for its bytecode.",
    "order-svc ships as one module of the commerce-suite.",
    "The commerce-suite is scheduled onto the nomad-cluster.",
    "The web-portal routes requests through the gateway-proxy.",
    "The gateway-proxy keeps its secrets in vault-kms.",
    "report-svc runs on the jvm-17 runtime.",
]

# Edge-less entities (exist in vocab, no edges — cannot bridge). They appear ONLY
# in cross-talk, never as a question subject. These are the live "Brain/MemCoT".
EDGELESS = ["telemetry-agent", "audit-log", "legacy-portal"]

# CROSS-TALK: co-mention multiple unrelated entities, so a search for one chain's
# subject drags in the others as seed noise. Mix connected + edge-less names.
CROSSTALK = [
    "In the Q3 review, checkout-svc, order-svc and web-portal were all flagged.",
    "The platform team owns billing-lib, commerce-suite and gateway-proxy.",
    "Infra manages jdk-21, nomad-cluster and vault-kms together.",
    "The telemetry-agent, audit-log and legacy-portal were discussed at standup.",
    "checkout-svc and the telemetry-agent share a dashboard with audit-log.",
    "order-svc, web-portal and legacy-portal appear on the same roadmap slide.",
]

DISTRACTORS = [
    "Daily standups are at 9:30am in the main channel.",
    "The wiki lives at wiki.corp.local.",
    "Release notes ship every Friday.",
    "The staging autoscaler kicks in above 70% CPU.",
]

QUESTIONS = [
    {"q": "What does the checkout-svc run on?", "subject": "checkout-svc",
     "relevant": {"checkout-svc", "billing-lib", "jdk-21"}, "gold": "jdk-21"},
    {"q": "Which cluster hosts the order-svc?", "subject": "order-svc",
     "relevant": {"order-svc", "commerce-suite", "nomad-cluster"}, "gold": "nomad-cluster"},
    {"q": "Where does the web-portal store its data?", "subject": "web-portal",
     "relevant": {"web-portal", "gateway-proxy", "vault-kms"}, "gold": "vault-kms"},
    {"q": "What runtime does report-svc run on?", "subject": "report-svc",
     "relevant": {"report-svc", "jvm-17"}, "gold": "jvm-17"},
]

# Vocabulary the seeder matches against = every entity that exists (connected +
# edge-less), mirroring the live _recall_vocab (all entities, not just edged).
VOCAB = sorted({e for _s, edges, _g in CHAINS for (a, _r, b) in edges
                for e in (a, b)} | set(EDGELESS))

# Degree = count of base edges incident on each entity (edge-less -> 0).
DEGREE: dict[str, int] = {n: 0 for n in VOCAB}
for _s, edges, _g in CHAINS:
    for (a, _r, b) in edges:
        DEGREE[a] = DEGREE.get(a, 0) + 1
        DEGREE[b] = DEGREE.get(b, 0) + 1


# ---------------------------------------------------------------------------
# Seed-selection strategies under test: (query, hits, names, degree) -> seeds
# ---------------------------------------------------------------------------
def _matches(blob: str, names: list[str]) -> list[str]:
    return [n for n in names if _mentions(blob, n)]


def seeds_v0(query, hits, names, degree):           # current (liberal)
    return _matches(query + " " + " ".join(hits), names)


def seeds_A(query, hits, names, degree):            # query-first (+hits fallback)
    q = _matches(query, names)
    if q:
        return q
    return _matches(" ".join(hits), names)


def seeds_AB(query, hits, names, degree):           # query-first + degree filter
    q = [n for n in _matches(query, names) if degree.get(n, 0) > 0 or True]  # keep query subjects
    if q:
        return q
    return [n for n in _matches(" ".join(hits), names) if degree.get(n, 0) > 0]


def seeds_C(query, hits, names, degree, cap=2):     # ranked + capped
    qset = set(_matches(query, names))
    cands = set(_matches(query + " " + " ".join(hits), names))

    def score(n):
        return (2 if n in qset else 0) + (1 if degree.get(n, 0) > 0 else 0) + len(n) / 100.0
    return sorted(cands, key=score, reverse=True)[:cap]


STRATEGIES = {"v0": seeds_v0, "A": seeds_A, "A+B": seeds_AB, "C": seeds_C}


class _StrategyController:
    def __init__(self, strategy, names, degree):
        self._strategy, self._names, self._degree = strategy, names, degree

    def seed_entities(self, query, hits, vocab):
        return self._strategy(query, hits, self._names, self._degree)

    def next_queries(self, query, newly):
        return [f"{query} {name}" for name in newly]


def _seed_bench_service(tmp):
    svc = build_service(tmp)
    for s in CHAIN_SNIPPETS + CROSSTALK + DISTRACTORS:
        svc.store(s, source="bench")
    for _s, edges, _g in CHAINS:
        for (a, r, b) in edges:
            out = svc.graph_relate(a, r, b, origin="bench")
            if out.get("error"):
                raise RuntimeError(f"seed edge failed: {a} {r} {b}: {out}")
    # Edge-less entities (telemetry-agent etc.) are NOT created in the graph: they
    # exist only in VOCAB (so v0 seeds them as noise from cross-talk text), and
    # graph_neighborhood on a nonexistent entity returns found:False (graceful) —
    # exactly modelling a vocab entity that can't bridge.
    return svc


def run(top_k: int = 5, hops: int = 3) -> dict:
    import tempfile
    rows = {}
    with tempfile.TemporaryDirectory(prefix="plseed_", ignore_cleanup_errors=True) as td:
        svc = _seed_bench_service(Path(td))
        for name, strat in STRATEGIES.items():
            prec, srec, arec, ents, gcalls, lat = [], 0, 0, [], [], []
            for item in QUESTIONS:
                calls = {"n": 0}
                orig = svc.graph_neighborhood

                def counted(entity, depth=1, _o=orig, _c=calls):
                    _c["n"] += 1
                    return _o(entity, depth)

                t0 = time.perf_counter()
                st = run_recall(svc.search, counted, VOCAB, item["q"],
                                _StrategyController(strat, VOCAB, DEGREE),
                                hops=hops, top_k=top_k)
                lat.append((time.perf_counter() - t0) * 1000)
                seeds = st.seeds
                if seeds:
                    prec.append(len(set(seeds) & item["relevant"]) / len(seeds))
                if item["subject"] in seeds:
                    srec += 1
                reached = set(st.entities) | {e["dst"] for e in st.edges} | {e["src"] for e in st.edges}
                if item["gold"] in reached:
                    arec += 1
                ents.append(len(st.entities))
                gcalls.append(calls["n"])
            n = len(QUESTIONS)
            rows[name] = {
                "seed_precision": round(sum(prec) / len(prec), 3) if prec else 0.0,
                "seed_recall": round(srec / n, 3),
                "answer_recall": round(arec / n, 3),
                "mean_entities": round(sum(ents) / n, 2),
                "mean_graph_calls": round(sum(gcalls) / n, 2),
                "mean_latency_ms": round(sum(lat) / n, 1),
            }
    return rows


def _report(rows: dict) -> None:
    hdr = f"{'variant':<8}{'seedP↑':>8}{'seedR':>7}{'ansR':>7}{'ents↓':>7}{'gcalls↓':>9}{'ms':>7}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for name in ("v0", "A", "A+B", "C"):
        r = rows[name]
        print(f"{name:<8}{r['seed_precision']:>8}{r['seed_recall']:>7}"
              f"{r['answer_recall']:>7}{r['mean_entities']:>7}"
              f"{r['mean_graph_calls']:>9}{r['mean_latency_ms']:>7}")
    base = rows["v0"]["answer_recall"]
    winner = None
    for name in ("A", "A+B", "C", "v0"):
        r = rows[name]
        if r["answer_recall"] >= base and r["seed_recall"] >= 1.0:
            if winner is None or r["seed_precision"] > rows[winner]["seed_precision"]:
                winner = name
    print(f"\n[winner: max seed_precision @ answer_recall>={base} & seed_recall=1.0] "
          f"-> {winner} (precision {rows[winner]['seed_precision']})")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    if args.run:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        rows = run()
        (RESULTS_DIR / "seed_bench.json").write_text(json.dumps(rows, indent=2))
        _report(rows)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
