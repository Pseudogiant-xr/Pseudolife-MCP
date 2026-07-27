#!/usr/bin/env python
"""Paired significance analysis for the extractor comparison (dev-only).

Compares candidate extractors against a reference extractor over the SAME
questions, using the persisted per-row judgements written by
``longmemeval_bench.py``. Each comparison is a paired binary design, so the
right test is McNemar's exact test on the discordant pairs (b = candidate
right / reference wrong, c = the reverse); the concordant pairs carry no
information about the difference.

The ``rag`` arm is the control: every run's rag context is built from raw
turns and is byte-identical across extractors, so any rag disagreement is
pure answerer/judge nondeterminism. That count is reported as the empirical
noise floor — an effect on cortex/hybrid that does not exceed it is not
evidence of an extractor difference, whatever its p-value.

Writes a JSON artifact (``--out``) rather than only printing: a p-value
without a committed artifact is not a publishable number.

Usage:
  python evals/analyze_extractor_comparison.py --tag smoke0726-qwenjudge \
      --reference sonnet-5 --candidates opus-5 fable-5
"""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from math import comb
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("cortex", "hybrid", "rag")


def load(extractor: str, tag: str) -> dict[str, dict]:
    path = RESULTS_DIR / f"longmemeval-ku-oracle-{extractor}-{tag}.jsonl"
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["question_id"]] = r
    return rows


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial p on the discordant pairs (point-mass method).

    Under H0 each discordant pair is a fair coin, so the two-sided p is the
    total probability of every outcome no more likely than the observed one.
    """
    n = b + c
    if n == 0:
        return 1.0
    observed = comb(n, b)
    return min(1.0, sum(comb(n, k) for k in range(n + 1)
                        if comb(n, k) <= observed) / 2 ** n)


def noise_floor(runs: dict[str, dict[str, dict]], qids: list[str]) -> dict:
    """Disagreements on the rag arm, whose context is identical across runs.

    Reported as both the any-run disagreement count and the worst pairwise
    count — the latter is what a single paired comparison is up against.
    """
    names = list(runs)
    identical = sum(
        len({runs[n][q]["contexts"]["rag"] for n in names}) == 1 for q in qids)
    any_disagree = [q for q in qids
                    if len({runs[n][q]["rag_correct"] for n in names}) > 1]
    pairwise = {}
    for a, b in combinations(names, 2):
        flips = [q for q in qids
                 if runs[a][q]["rag_correct"] != runs[b][q]["rag_correct"]]
        pairwise[f"{a} vs {b}"] = len(flips)
    return {
        "identical_rag_context": f"{identical}/{len(qids)}",
        "any_run_disagreement": len(any_disagree),
        "pairwise_disagreement": pairwise,
        "worst_pairwise": max(pairwise.values()) if pairwise else 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--reference", default="sonnet-5")
    ap.add_argument("--candidates", nargs="+", required=True)
    ap.add_argument("--out", type=Path, default=None,
                    help="artifact path (default results/extractor-"
                         "comparison-<tag>.json)")
    args = ap.parse_args()

    runs = {name: load(name, args.tag)
            for name in [args.reference, *args.candidates]}
    qids = sorted(runs[args.reference])
    floor = noise_floor(runs, qids)

    out = {"tag": args.tag, "reference": args.reference, "n": len(qids),
           "noise_floor_rag_control": floor, "comparisons": {}}

    for cand in args.candidates:
        entry = {}
        for arm in ARMS:
            key = f"{arm}_correct"
            b = sum(runs[cand][q][key] and not runs[args.reference][q][key]
                    for q in qids)
            c = sum(not runs[cand][q][key] and runs[args.reference][q][key]
                    for q in qids)
            acc_cand = sum(runs[cand][q][key] for q in qids) / len(qids)
            acc_ref = sum(runs[args.reference][q][key] for q in qids) / len(qids)
            entry[arm] = {
                "accuracy": round(acc_cand, 3),
                "reference_accuracy": round(acc_ref, 3),
                "delta": round(acc_cand - acc_ref, 3),
                "wins": b, "losses": c, "net": b - c,
                "p_mcnemar_exact": round(mcnemar_exact(b, c), 4),
                "exceeds_noise_floor": abs(b - c) > floor["worst_pairwise"],
            }
        out["comparisons"][cand] = entry

    path = args.out or RESULTS_DIR / f"extractor-comparison-{args.tag}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nartifact: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
