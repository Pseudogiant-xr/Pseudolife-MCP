"""Cross-model merge gates: what the sweep's two-vote gates would apply when
the first and second opinions come from DIFFERENT models.

``queue_judge_ladder.py`` simulates the two-vote gates from two replicates
of ONE arm. The live merge judge pairs a first-opinion model with a
different ``judge_second_model``, so the decision to raise
``deep_dream.judge_mode`` needs the pair itself: replicate k of the first
arm against replicate k of the second, per panel row. Also reported:

  * splits — rows where the pair disagrees (the sweep leaves these for a
    human), with how often each arm's own vote matched the panel label;
  * calibration — per arm, how many reject votes sit at/above the
    single-vote auto-reject gate. An arm whose every reject clears the gate
    has uninformative confidence: the gate filters nothing.

Reads only committed, scrubbed artifacts (ladder per-row votes and the
panel's ``low_differential`` stamps); no private evidence pack.

Usage:
    python evals/cross_judge_gates.py --first opus55 \\
        --arms sonnet55,fable51,astra,sol,luna \\
        [--tag 20260929] [--out evals/results/queue-judge-cross-20260929.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
PANEL = RESULTS / "queue-judge-panel-20260902.json"


def load_arm(tag: str, name: str) -> dict:
    doc = json.loads((RESULTS / f"queue-judge-ladder-{tag}-{name}.json")
                     .read_text(encoding="utf-8"))
    (arm,) = doc["arms"].values()
    return arm


def merge_votes(arm: dict) -> dict[int, dict]:
    return {r["id"]: r for r in arm["queues"]["merges"]["per_row"]}


def gates(first: dict, second: dict, low: dict, rep: int, *,
          reject_gate_2: float, accept_gate: float) -> dict:
    rej = {"n": 0, "bad": 0, "bad_ids": []}
    acc = {"n": 0, "bad": 0, "bad_ids": []}
    for rid, row in first.items():
        other = second.get(rid)
        if other is None or len(row["votes"]) <= rep or len(other["votes"]) <= rep:
            continue
        a, b = row["votes"][rep], other["votes"][rep]
        if not a or not b:
            continue
        mean = (a["confidence"] + b["confidence"]) / 2
        if a["verdict"] == b["verdict"] == "reject" and mean >= reject_gate_2:
            rej["n"] += 1
            if row["label"] != "reject":
                rej["bad"] += 1
                rej["bad_ids"].append(rid)
        if (a["verdict"] == b["verdict"] == "accept" and mean >= accept_gate
                and low.get(rid) is False):
            acc["n"] += 1
            if row["label"] != "accept":
                acc["bad"] += 1
                acc["bad_ids"].append(rid)
    return {"two_vote_reject": rej, "two_vote_accept_not_lowdiff": acc}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--first", required=True)
    ap.add_argument("--arms", required=True)
    ap.add_argument("--tag", default="20260929")
    ap.add_argument("--split-pair", default=None,
                    help="second arm whose disagreements with --first are "
                         "tallied (default: the first of --arms)")
    ap.add_argument("--reject-gate", type=float, default=0.8)
    ap.add_argument("--reject-gate-2", type=float, default=0.7)
    ap.add_argument("--accept-gate", type=float, default=0.6)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    names = [a.strip() for a in args.arms.split(",") if a.strip()]
    out = args.out or RESULTS / f"queue-judge-cross-{args.tag}.json"

    panel = json.loads(PANEL.read_text(encoding="utf-8"))
    low = {r["id"]: r["low_differential"] for r in panel["merges"]}
    arms = {n: load_arm(args.tag, n) for n in [args.first, *names]}
    votes = {n: merge_votes(a) for n, a in arms.items()}
    first = votes[args.first]

    pairs = {}
    for name in names:
        reps = min(arms[args.first]["replicates"], arms[name]["replicates"])
        pairs[name] = {
            "model": arms[name]["model"],
            "replicates": [gates(first, votes[name], low, k,
                                 reject_gate_2=args.reject_gate_2,
                                 accept_gate=args.accept_gate)
                           for k in range(reps)]}

    def first_vote(arm_votes, rid):
        """Replicate 1's vote on a row, or None when the row is missing or
        its batch failed (the ladder writes a null vote)."""
        row = arm_votes.get(rid)
        return row["votes"][0] if row and row["votes"] else None

    split_arm = args.split_pair or names[0]
    split_rows = [rid for rid in first
                  if (a := first_vote(first, rid))
                  and (b := first_vote(votes[split_arm], rid))
                  and a["verdict"] != b["verdict"]]
    correct = {n: sum(1 for rid in split_rows
                      if (v := first_vote(votes[n], rid))
                      and v["verdict"] == first[rid]["label"])
               for n in votes}

    calibration = {}
    for n, v in votes.items():
        rejects = [vote for row in v.values() for vote in row["votes"]
                   if vote and vote["verdict"] == "reject"]
        calibration[n] = {
            "reject_votes": len(rejects),
            "at_or_above_reject_gate": sum(
                1 for vote in rejects if vote["confidence"] >= args.reject_gate)}

    doc = {"built_by": "evals/cross_judge_gates.py", "tag": args.tag,
           "first": {"arm": args.first, "model": arms[args.first]["model"]},
           "gates": {"reject_gate": args.reject_gate,
                     "reject_gate_2": args.reject_gate_2,
                     "accept_gate": args.accept_gate},
           "panel": PANEL.name, "pairs": pairs,
           "splits": {"pair": [args.first, split_arm], "replicate": 1,
                      "rows": split_rows, "n": len(split_rows),
                      "correct_by_arm": correct},
           "calibration": calibration}
    out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: doc[k] for k in ("pairs", "splits", "calibration")},
                     indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
