"""Within-run paired arm comparison over one judged BEAM JSONL.

``beam_cross_run_paired.py`` pairs the SAME arm across two runs; this pairs
every non-control arm against the ``rag`` control INSIDE one run, which is
the comparison a five-arm run exists to make. Per arm it writes:

  * the arm's mean score and the control's mean score
  * the paired per-question delta (arm minus rag), its 95% CI (normal
    approximation, 1.96 x SE over n rows) and a two-sided sign-flip
    permutation p (10k draws, seed 0)
  * per-type means for the arm and the control
  * mean served-context characters per question where the row persisted
    the arm's context (``contexts[arm]``), so a turn-matched comparison can
    say how far it is from character-matched

Usage (tags resolve against evals/results/):

  python evals/beam_within_run_pairs.py --tag chip12-b16 \
      --arms refind,hybrid,cortex,nomem

Writes ``evals/results/beam-100K-qwen-27b-<tag>.arms-vs-rag.json`` and
refuses to overwrite an existing artifact (never overwrite a canonical
result file — rerun with ``--out-tag``).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CONTROL = "rag"
PERMS = 10_000
SEED = 0


def _context_chars(row: dict, arm: str) -> int | None:
    ctx = (row.get("contexts") or {}).get(arm)
    if ctx is None:
        return None
    if isinstance(ctx, str):
        return len(ctx)
    if isinstance(ctx, list):
        return sum(len(x if isinstance(x, str) else json.dumps(x))
                   for x in ctx)
    if isinstance(ctx, dict):
        return len(json.dumps(ctx))
    return len(str(ctx))


def _perm_p(deltas: list[float], perms: int, seed: int) -> float:
    """Two-sided sign-flip permutation p for mean(deltas) != 0."""
    observed = abs(statistics.fmean(deltas))
    if observed == 0.0:
        return 1.0
    rng = random.Random(seed)
    hits = 0
    n = len(deltas)
    for _ in range(perms):
        flipped = sum(d if rng.random() < 0.5 else -d for d in deltas)
        if abs(flipped / n) >= observed:
            hits += 1
    return (hits + 1) / (perms + 1)


def pair_run(rows: list[dict], arms: list[str], perms: int = PERMS,
             seed: int = SEED) -> dict:
    out: dict = {
        "control": CONTROL, "n_rows": len(rows), "perms": perms,
        "seed": seed, "arms": {},
    }
    ctrl_key = f"{CONTROL}_score"
    ctrl_types: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        ctrl_types[r["type"]].append(r[ctrl_key])
    out["control_mean"] = round(statistics.fmean(
        r[ctrl_key] for r in rows), 4)
    out["control_types"] = {
        t: round(statistics.fmean(v), 4) for t, v in sorted(ctrl_types.items())}
    ctrl_chars = [c for c in (_context_chars(r, CONTROL) for r in rows)
                  if c is not None]
    out["control_context_chars_mean"] = (
        round(statistics.fmean(ctrl_chars)) if ctrl_chars else None)
    for arm in arms:
        key = f"{arm}_score"
        paired = [(r[key], r[ctrl_key]) for r in rows if key in r]
        if not paired:
            out["arms"][arm] = {"n": 0}
            continue
        deltas = [a - b for a, b in paired]
        n = len(deltas)
        mean_d = statistics.fmean(deltas)
        se = statistics.stdev(deltas) / math.sqrt(n) if n > 1 else 0.0
        types: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            if key in r:
                types[r["type"]].append(r[key])
        chars = [c for c in (_context_chars(r, arm) for r in rows)
                 if c is not None]
        out["arms"][arm] = {
            "n": n,
            "mean": round(statistics.fmean(a for a, _ in paired), 4),
            "delta_vs_control": round(mean_d, 4),
            "ci95_halfwidth": round(1.96 * se, 4),
            "perm_p": round(_perm_p(deltas, perms, seed), 4),
            "wins": sum(1 for d in deltas if d > 0),
            "losses": sum(1 for d in deltas if d < 0),
            "ties": sum(1 for d in deltas if d == 0),
            "full_marks_rows": sum(1 for a, _ in paired if a == 1.0),
            "types": {t: round(statistics.fmean(v), 4)
                      for t, v in sorted(types.items())},
            "context_chars_mean": (round(statistics.fmean(chars))
                                   if chars else None),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", required=True,
                    help="run tag, e.g. chip12-b16")
    ap.add_argument("--prefix", default="beam-100K-qwen-27b-")
    ap.add_argument("--arms", default="refind,hybrid,cortex,nomem")
    ap.add_argument("--out-tag", default=None,
                    help="write <prefix><out-tag>.arms-vs-rag.json instead")
    ap.add_argument("--perms", type=int, default=PERMS)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)

    src = RESULTS_DIR / f"{args.prefix}{args.tag}.jsonl"
    out_path = RESULTS_DIR / (
        f"{args.prefix}{args.out_tag or args.tag}.arms-vs-rag.json")
    if out_path.exists():
        raise SystemExit(f"refusing to overwrite {out_path}; use --out-tag")
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8")
            .splitlines() if line.strip()]
    result = pair_run(rows, [a for a in args.arms.split(",") if a],
                      perms=args.perms, seed=args.seed)
    result["source"] = src.name
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("control_types",)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
