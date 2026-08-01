"""Paired between-run arm comparison over judged LongMemEval JSONL results.

The committed producer for ``compare-<tag>-pairs.json`` artifacts (the
earlier c2op-count pairs file was produced by an ad-hoc uncommitted script —
a gate that cannot be reproduced is not a gate). Given two judged runs of
the same question set — e.g. a prompt-variant arm vs the shipped baseline —
this computes, per arm (rag / cortex / hybrid / derived cascade):

  * per-run accuracy
  * the paired per-question delta (A minus B)
  * a two-sided sign-flip permutation p (default 10k draws, seed 0)
  * win/loss counts with question ids, for class-breakdown analysis

The ``rag`` arm never touches the extractor, so its A-vs-B delta bounds the
measurement noise of the whole comparison: on the reproducible bench server
it must be exactly 0, and a nonzero rag delta invalidates the run.

Usage (tags resolve against evals/results/):

  python evals/compare_arms.py --a c2v6-literal --b c2op-count \
      --out-tag c2v6-literal
  python evals/compare_arms.py --a-file results/x.jsonl --b-file results/y.jsonl

Writes ``evals/results/compare-<out-tag>-pairs.json``; refuses to overwrite
an existing artifact (rerun with a fresh tag — never overwrite a canonical
result file).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from replicate import cascade_correct  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ARMS = ("rag", "cortex", "hybrid", "cascade")


def _load_rows(path: Path) -> dict[str, dict]:
    rows = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                rows[str(r["question_id"])] = r
    return rows


def _correct(row: dict, arm: str) -> bool:
    if arm == "cascade":
        return bool(cascade_correct(row))
    return bool(row[f"{arm}_correct"])


def _perm_p(deltas: list[int], draws: int, seed: int) -> float:
    """Two-sided sign-flip permutation p for paired binary deltas."""
    observed = abs(sum(deltas))
    nonzero = [d for d in deltas if d]
    if not nonzero or observed == 0:
        return 1.0
    rng = random.Random(seed)
    hits = 0
    for _ in range(draws):
        s = sum(d if rng.random() < 0.5 else -d for d in nonzero)
        if abs(s) >= observed:
            hits += 1
    return hits / draws


def compare(a_file: Path, b_file: Path, *, draws: int = 10_000,
            seed: int = 0) -> dict:
    a_rows = _load_rows(Path(a_file))
    b_rows = _load_rows(Path(b_file))
    shared = sorted(a_rows.keys() & b_rows.keys())
    n = len(shared)
    if n == 0:
        raise SystemExit("no shared question_ids between the two runs")
    out = {
        "n": n,
        "dropped_a": len(a_rows) - n,
        "dropped_b": len(b_rows) - n,
        "draws": draws,
        "seed": seed,
        "a": {"file": str(a_file), "arms": {}},
        "b": {"file": str(b_file), "arms": {}},
        "paired": {"a_vs_b": {}},
    }
    for arm in ARMS:
        a_ok = {q: _correct(a_rows[q], arm) for q in shared}
        b_ok = {q: _correct(b_rows[q], arm) for q in shared}
        out["a"]["arms"][arm] = round(sum(a_ok.values()) / n, 4)
        out["b"]["arms"][arm] = round(sum(b_ok.values()) / n, 4)
        deltas = [int(a_ok[q]) - int(b_ok[q]) for q in shared]
        win_qids = [q for q in shared if a_ok[q] and not b_ok[q]]
        loss_qids = [q for q in shared if b_ok[q] and not a_ok[q]]
        out["paired"]["a_vs_b"][arm] = {
            "delta": round(sum(deltas) / n, 4),
            "p": round(_perm_p(deltas, draws, seed), 5),
            "wins": len(win_qids),
            "losses": len(loss_qids),
            "win_qids": win_qids,
            "loss_qids": loss_qids,
        }
    return out


def _resolve(tag_or_none: str | None, file_or_none: str | None,
             dataset: str, extractor: str, side: str) -> Path:
    if file_or_none:
        return Path(file_or_none)
    if not tag_or_none:
        raise SystemExit(f"pass --{side} <tag> or --{side}-file <path>")
    return (RESULTS_DIR
            / f"longmemeval-ku-{dataset}-{extractor}-{tag_or_none}.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--a", default=None, help="tag of run A (the candidate)")
    ap.add_argument("--b", default=None, help="tag of run B (the baseline)")
    ap.add_argument("--a-file", default=None)
    ap.add_argument("--b-file", default=None)
    ap.add_argument("--dataset", default="oracle")
    ap.add_argument("--extractor", default="qwen-27b")
    ap.add_argument("--out-tag", required=True)
    ap.add_argument("--draws", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    a_file = _resolve(args.a, args.a_file, args.dataset, args.extractor, "a")
    b_file = _resolve(args.b, args.b_file, args.dataset, args.extractor, "b")
    out_path = RESULTS_DIR / f"compare-{args.out_tag}-pairs.json"
    if out_path.exists():
        raise SystemExit(f"{out_path} exists — pick a fresh --out-tag "
                         "(never overwrite a canonical result file)")

    result = compare(a_file, b_file, draws=args.draws, seed=args.seed)
    out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"n={result['n']}  (dropped a={result['dropped_a']} "
          f"b={result['dropped_b']})")
    print(f"{'arm':<10}{'A':>8}{'B':>8}{'delta':>8}{'p':>10}{'W/L':>8}")
    for arm in ARMS:
        pa = result["paired"]["a_vs_b"][arm]
        print(f"{arm:<10}{result['a']['arms'][arm]:>8.3f}"
              f"{result['b']['arms'][arm]:>8.3f}{pa['delta']:>8.3f}"
              f"{pa['p']:>10.5f}{pa['wins']:>5}/{pa['losses']}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
