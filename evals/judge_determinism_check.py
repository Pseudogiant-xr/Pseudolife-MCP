#!/usr/bin/env python
"""Judge/answerer reproducibility check for the local GPU bench stack (dev-only).

Runs over PAIRS of answer-phase passes that used byte-identical inputs (same
persisted contexts, same questions, same server config). Any difference between
a pair is nondeterminism in the answerer+judge stack, not a real effect — so
this measures the floor below which no benchmark delta is interpretable.

Two levels are reported because they answer different questions:
  * response_diff — the answerer produced different TEXT. Sensitive; catches
    drift that never reaches the verdict.
  * verdict_flip  — the graded yes/no changed. This is the one that moves
    published accuracy numbers.

``--pair`` takes ``label=tagA,tagB`` and may be repeated, so configurations
(e.g. speculative decoding on vs off) can be compared side by side.

Writes a JSON artifact — a noise floor that lives only in a terminal cannot
constrain a later claim.

Usage:
  python evals/judge_determinism_check.py --extractor sonnet-5 \
      --pair "mtp-on=detcheck-mtp-a,detcheck-mtp-b" \
      --pair "mtp-off=detcheck-nomtp-a,detcheck-nomtp-b"
"""
from __future__ import annotations

import argparse
import json
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


def compare(a: dict, b: dict) -> dict:
    qids = sorted(set(a) & set(b))
    # Guard: the whole measurement assumes identical inputs. If the contexts
    # differ, any divergence is a real effect and this tool is meaningless.
    ctx_identical = all(a[q]["contexts"] == b[q]["contexts"] for q in qids)
    out = {"n_questions": len(qids), "inputs_identical": ctx_identical,
           "arms": {}}
    flips = diffs = 0
    for arm in ARMS:
        f = [q for q in qids if a[q][f"{arm}_correct"] != b[q][f"{arm}_correct"]]
        d = [q for q in qids
             if a[q].get(f"{arm}_response") != b[q].get(f"{arm}_response")]
        acc_a = sum(a[q][f"{arm}_correct"] for q in qids) / len(qids)
        acc_b = sum(b[q][f"{arm}_correct"] for q in qids) / len(qids)
        out["arms"][arm] = {
            "accuracy_pass_a": round(acc_a, 3),
            "accuracy_pass_b": round(acc_b, 3),
            "accuracy_swing": round(abs(acc_a - acc_b), 3),
            "verdict_flips": len(f),
            "response_diffs": len(d),
        }
        flips += len(f)
        diffs += len(d)
    total = len(qids) * len(ARMS)
    out["verdict_flip_rate"] = round(flips / total, 4)
    out["response_diff_rate"] = round(diffs / total, 4)
    out["max_accuracy_swing"] = max(v["accuracy_swing"]
                                    for v in out["arms"].values())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extractor", default="sonnet-5")
    ap.add_argument("--pair", action="append", required=True,
                    help="label=tagA,tagB (repeatable)")
    ap.add_argument("--out", type=Path,
                    default=RESULTS_DIR / "judge-determinism-check.json")
    args = ap.parse_args()

    out = {"extractor": args.extractor, "configurations": {}}
    for spec in args.pair:
        label, tags = spec.split("=", 1)
        tag_a, tag_b = tags.split(",")
        out["configurations"][label] = compare(
            load(args.extractor, tag_a), load(args.extractor, tag_b))
        out["configurations"][label]["tags"] = [tag_a, tag_b]

    swings = [c["max_accuracy_swing"] for c in out["configurations"].values()]
    out["verdict"] = {
        "reproducible": all(c["verdict_flip_rate"] == 0
                            for c in out["configurations"].values()),
        "worst_accuracy_swing": max(swings),
        "interpretation": (
            "No benchmark delta smaller than worst_accuracy_swing is "
            "interpretable from a single pass; use paired replicates."),
    }
    args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nartifact: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
