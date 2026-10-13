"""Combine single-arm embedder_recall.py artifacts into one comparison.

Running one arm per process isolates a crash or an incompatible model to
that arm and lets each arm use its own environment; the paired test still
works because every artifact carries its per-gold hit vectors in a stable
order. The inputs must share dataset, question count and gold count.

    python evals/embedder_recall_combine.py --baseline qwen3-0.6b \
        --out evals/results/embedder-recall-<tag>.json runs/locomo/*.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from embedder_recall import mcnemar


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("--baseline", required=True,
                    help="arm key (input file stem) or arm label to test "
                         "every other arm against")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rows, meta = [], None
    for path in sorted(args.inputs):
        d = json.loads(path.read_text(encoding="utf-8"))
        m = (d.get("dataset", "lme"), d["questions"], d["gold_turns"])
        if meta is None:
            meta = m
        elif m != meta:
            raise SystemExit(f"{path}: dataset/questions/gold {m} != {meta}")
        for arm in d["arms"]:
            rows.append({"key": path.stem, **arm})

    base = next((r for r in rows
                 if args.baseline in (r["key"], r["arm"])), None)
    if base is None:
        raise SystemExit(f"baseline {args.baseline!r} not among inputs")
    ks = sorted(base["per_gold_hits"], key=int)
    tests = []
    for r in rows:
        if r is base:
            continue
        for k in ks:
            won, lost, p = mcnemar(base["per_gold_hits"][k],
                                   r["per_gold_hits"][k])
            tests.append({"arm": r["arm"], "k": int(k), "gained": won,
                          "lost": lost, "p_value": p})

    rows.sort(key=lambda r: -r["recall"]["10"])
    dataset, n_q, n_gold = meta
    args.out.write_text(json.dumps(
        {"dataset": dataset, "questions": n_q, "gold_turns": n_gold,
         "baseline": base["arm"], "arms": rows,
         "mcnemar_vs_baseline": tests}, indent=2), encoding="utf-8")

    print(f"{dataset}: {n_q} questions, {n_gold} gold turns; "
          f"baseline {base['arm']}")
    for r in rows:
        line = "  ".join(f"R@{k}={r['recall'][k]:.3f}" for k in ks)
        t10 = next((t for t in tests if t["arm"] == r["arm"]
                    and t["k"] == 10), None)
        sig = (f"  @10 +{t10['gained']}/-{t10['lost']} p={t10['p_value']:.3f}"
               if t10 else "  (baseline)")
        print(f"  {r['arm'][:44]:44} {line}  dim {r['dim']:<5}"
              f" {r['encode_seconds']:>7.1f}s{sig}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
