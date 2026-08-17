"""Merge-judge model ladder: can arm X reproduce ratified panel judgment?

Runs the SHIPPED judge code path (``OpenAICompatExtractor.judge_merges`` —
same system prompt, same proposal serialization, same batch size as the
daemon's autonomous Step C) against ``evals/data/judge_eval_20260816.json``
and scores agreement with the ground-truth labels.

Metrics per arm (majority vote across replicates; per-replicate flip rate
reported beside it):
  * reject_precision — of the arm's rejects, the fraction truly reject.
    THE Phase-1 gate: auto-reject ships only where this is high.
  * false_reject_rate — true accepts the arm rejected (a wrong auto-reject
    buries a real fold; pair dismissal suppresses re-proposal).
  * accept_precision / coverage (decided fraction; "leave" = abstain).

Usage (one arm per invocation; results append into one JSON artifact):
    python evals/judge_ladder.py --arm sidecar --base-url http://... \
        --model gemma4-e4b [--replicates 1] [--batch 8] \
        [--out evals/results/judge-ladder-20260816.json]

Persists by default (a bench that only prints was never measured).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # repo root
from pseudolife_memory.memory.dream import (  # noqa: E402
    ExtractorError, OpenAICompatExtractor,
)

DATA = Path(__file__).parent / "data" / "judge_eval_20260816.json"
DEFAULT_OUT = Path(__file__).parent / "results" / "judge-ladder-20260816.json"


def run_replicate(ex: OpenAICompatExtractor, rows: list[dict],
                  batch: int) -> list[tuple[str, float] | None]:
    """One full pass; one (verdict, confidence) — or None — per row."""
    verdicts: list[tuple[str, float] | None] = [None] * len(rows)
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        proposals = [{"n": i + 1, "from": r["from"], "into": r["into"],
                      "reason": r.get("reason"), "score": r.get("score")}
                     for i, r in enumerate(chunk)]
        try:
            out = ex.judge_merges(proposals)
        except ExtractorError as exc:
            print(f"  batch {start // batch}: FAILED ({exc}) — rows skipped")
            continue
        for v in out:
            verdicts[start + v["n"] - 1] = (v["verdict"], v["confidence"])
    return verdicts


def majority(votes: list[tuple[str, float] | None]) -> tuple[str, float] | None:
    """Majority verdict; its confidence = mean confidence of agreeing votes."""
    votes = [v for v in votes if v]
    if not votes:
        return None
    counts = collections.Counter(v[0] for v in votes)
    top, n = counts.most_common(1)[0]
    if n * 2 <= len(votes):
        return ("leave", 0.0)                          # no majority = abstain
    confs = [c for v, c in votes if v == top]
    return (top, sum(confs) / len(confs))


def score(rows: list[dict], final: list[tuple[str, float] | None],
          auto_conf: float = 0.8) -> dict:
    """Raw-vote metrics plus the deployment simulation: what auto-reject at
    ``judge_reject_min_confidence`` = ``auto_conf`` would actually apply."""
    dec = acc_tp = acc_fp = rej_tp = rej_fp = false_rej = 0
    auto_rej = auto_rej_bad = 0
    for r, v in zip(rows, final):
        if v is None:
            continue
        verdict, conf = v
        if verdict in ("accept", "reject"):
            dec += 1
        if verdict == "accept":
            (acc_tp, acc_fp) = ((acc_tp + 1, acc_fp) if r["label"] == "accept"
                                else (acc_tp, acc_fp + 1))
        elif verdict == "reject":
            if r["label"] == "reject":
                rej_tp += 1
            else:
                rej_fp += 1
                false_rej += 1
            if conf >= auto_conf:
                auto_rej += 1
                auto_rej_bad += r["label"] == "accept"
    n_true_acc = sum(1 for r in rows if r["label"] == "accept")
    n_true_rej = len(rows) - n_true_acc
    agree = acc_tp + rej_tp

    def _rate(num, den):
        return round(num / den, 4) if den else None
    return {
        "rows": len(rows), "decided": dec,
        "coverage": _rate(dec, len(rows)),
        "agreement_on_decided": _rate(agree, dec),
        "accept_precision": _rate(acc_tp, acc_tp + acc_fp),
        "reject_precision": _rate(rej_tp, rej_tp + rej_fp),
        "false_reject_rate": _rate(false_rej, n_true_acc),
        "false_rejects": false_rej, "true_accepts": n_true_acc,
        "auto_conf": auto_conf,
        "auto_rejected": auto_rej,
        "auto_reject_share_of_true_rejects": _rate(auto_rej - auto_rej_bad,
                                                   n_true_rej),
        "auto_false_rejects": auto_rej_bad,
        "auto_reject_precision": _rate(auto_rej - auto_rej_bad, auto_rej),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, help="arm name in the artifact")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--replicates", type=int, default=1)
    ap.add_argument("--batch", type=int, default=8,
                    help="proposals per call; keep = deep_dream.judge_batch")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--thinking", action="store_true",
                    help="unpin enable_thinking:false on the judge call so "
                         "the server/template reasoning default governs — "
                         "an experimental arm, NOT the shipped code path")
    args = ap.parse_args()

    rows = json.loads(DATA.read_text(encoding="utf-8"))["rows"]
    ex = OpenAICompatExtractor(args.base_url, args.model,
                               timeout_seconds=args.timeout,
                               judge_thinking=args.thinking)
    reps: list[list[str | None]] = []
    for i in range(args.replicates):
        t0 = time.time()
        reps.append(run_replicate(ex, rows, args.batch))
        n = sum(1 for v in reps[-1] if v)
        print(f"replicate {i + 1}/{args.replicates}: {n}/{len(rows)} "
              f"verdicts in {time.time() - t0:.0f}s")

    final = [majority([rep[i] for rep in reps]) for i in range(len(rows))]
    flips = 0
    if len(reps) > 1:
        for i in range(len(rows)):
            got = {rep[i][0] for rep in reps if rep[i]}
            flips += len(got) > 1
    result = {
        "arm": args.arm, "model": args.model, "base_url": args.base_url,
        "replicates": args.replicates, "batch": args.batch,
        "flip_rows": flips, **score(rows, final),
        "per_row": [{"from": r["from"]["display"],
                     "into": r["into"]["display"], "label": r["label"],
                     "votes": [list(rep[i]) if rep[i] else None
                               for rep in reps]}
                    for i, r in enumerate(rows)],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc = (json.loads(args.out.read_text(encoding="utf-8"))
           if args.out.exists() else {"data": DATA.name, "arms": {}})
    doc["arms"][args.arm] = result
    args.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    slim = {k: v for k, v in result.items() if k != "per_row"}
    print(json.dumps(slim, indent=1))


if __name__ == "__main__":
    main()
