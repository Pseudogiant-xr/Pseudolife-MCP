"""Retention-interval eval analysis: paired H2 test + verdict artifact.

Preregistration: docs/superpowers/specs/2026-08-08-retention-interval-eval-design.md

Reads the Study A artifact and N Study B replicate artifacts, computes the
preregistered paired sign-flip permutation (10k draws, seed 0 — the
compare_arms convention) over per-(fact, replicate) differences in
unqualified-stale serving between the flags-visible and flags-stripped
arms, checks fresh-fact non-inferiority (within 0.02), and writes the
verdict artifact.

    PYTHONPATH=. python evals/analyze_retention.py \
        --study-a evals/results/retention-interval-ret-0809.json \
        --study-b evals/results/retention-interval-ret-0809-b-r*.json \
        --out evals/results/retention-interval-verdict.json
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

DRAWS = 10_000
SEED = 0


def paired_permutation_p(diffs: list[int]) -> float:
    """Two-sided sign-flip permutation p for paired differences."""
    observed = abs(sum(diffs))
    rng = random.Random(SEED)
    hits = 0
    for _ in range(DRAWS):
        s = sum(d if rng.random() < 0.5 else -d for d in diffs)
        if abs(s) >= observed:
            hits += 1
    return hits / DRAWS


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--study-a", type=Path, required=True)
    ap.add_argument("--study-b", type=Path, nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    a = json.loads(args.study_a.read_text(encoding="utf-8"))["study_a"]

    diffs: list[int] = []            # stripped_unqualified - visible_unqualified per (fact, replicate)
    per_replicate = {}
    per_fact: dict[str, list[int]] = {}
    fresh_rates = {"flags_visible": [], "flags_stripped": []}
    control_rates = []
    for path in args.study_b:
        b = json.loads(path.read_text(encoding="utf-8"))["study_b"]
        vis = {r["entity"]: r for r in b["arms"]["flags_visible"]
               if r["freshness_class"] == "volatile"}
        strp = {r["entity"]: r for r in b["arms"]["flags_stripped"]
                if r["freshness_class"] == "volatile"}
        rep_diffs = [int(strp[e]["unqualified"]) - int(vis[e]["unqualified"])
                     for e in vis]
        for e in vis:
            per_fact.setdefault(e, []).append(
                int(strp[e]["unqualified"]) - int(vis[e]["unqualified"]))
        diffs.extend(rep_diffs)
        per_replicate[path.name] = {
            "visible_stale_rate": b["summary"]["flags_visible"]["stale_answer_rate"],
            "stripped_stale_rate": b["summary"]["flags_stripped"]["stale_answer_rate"],
            "discordant_pairs": sum(1 for d in rep_diffs if d != 0),
        }
        fresh_rates["flags_visible"].append(
            b["summary"]["flags_visible"]["fresh_answer_rate"])
        fresh_rates["flags_stripped"].append(
            b["summary"]["flags_stripped"]["fresh_answer_rate"])
        control_rates.append(
            b["summary"]["control_evergreen"]["stale_answer_rate"])

    p = paired_permutation_p(diffs)
    fresh_gap = (sum(fresh_rates["flags_stripped"]) -
                 sum(fresh_rates["flags_visible"])) / len(args.study_b)
    h2 = p < 0.05 and fresh_gap <= 0.02

    verdict = {
        "preregistration":
            "docs/superpowers/specs/2026-08-08-retention-interval-eval-design.md",
        "inputs": {"study_a": args.study_a.name,
                   "study_b": [p_.name for p_ in args.study_b]},
        "h1_time_invariance": {
            "pass": a["h1_time_invariant"],
            "offsets": a["offsets"],
        },
        "h2_flag_efficacy": {
            "pass": h2,
            "paired_units": len(diffs),
            "discordant": sum(1 for d in diffs if d != 0),
            "all_discordant_favor_flags": all(d >= 0 for d in diffs)
                                          and any(d > 0 for d in diffs),
            "permutation_p_two_sided": p,
            "draws": DRAWS, "seed": SEED,
            "per_replicate": per_replicate,
            "fresh_noninferiority_gap": round(fresh_gap, 4),
            "control_evergreen_answer_rate": control_rates,
            "per_fact_flag_effect": {
                e: sum(v) / len(v) for e, v in sorted(per_fact.items())},
        },
    }
    args.out.write_text(json.dumps(verdict, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    print(json.dumps({"h1": verdict["h1_time_invariance"]["pass"],
                      "h2": h2, "p": p, "pairs": len(diffs)}), flush=True)
    print(f"verdict -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
