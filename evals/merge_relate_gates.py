"""Cross-model merge gates with the merge judge's ``relate`` verdict.

The sweep's two-vote merge gates pair a first-opinion model with a
DIFFERENT second-opinion model. This replays that pairing from two
``queue_judge_ladder.py`` artifacts — replicate k of the first arm against
replicate k of the second, per panel row — with ``relate`` counted as
reject-class, exactly as ``deep_dream_judge`` gates it (2026-09-30):

  * two_vote_reject_class — both votes reject-class at mean >=
    ``--reject-gate-2``; ``bad`` = the panel labelled the row accept.
    ``links_filed`` = those with at least one relate vote (the sweep files
    the more confident relate vote's relation as a link proposal), and how
    many of those sit on rows the panel itself called related (whether the
    relation matches the panel's is in each arm's ladder ``relate`` block:
    the scrubbed panel does not carry the panel's relations);
  * two_vote_accept_not_lowdiff — both accept at mean >= ``--accept-gate``
    on a row the panel stamped not low-differential;
  * splits — accept vs reject-class disagreements (the sweep leaves these
    for a human; reject vs relate is agreement; the sweep also splits on
    a leave, which this count leaves out);
  * single_vote — per arm and replicate, reject-class votes at/above
    ``--reject-gate`` (the first opinion's single-vote gate).

Reads only committed, scrubbed artifacts (ladder per-row votes and the
panel's labels, verdicts and ``low_differential`` stamps).

Usage:
    python evals/merge_relate_gates.py \\
        --first evals/results/queue-judge-ladder-20260930-relate-opus55.json \\
        --second evals/results/queue-judge-ladder-20260930-relate-sonnet55.json \\
        --out evals/results/queue-judge-cross-20260930-relate.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
PANEL = RESULTS / "queue-judge-panel-20260902.json"
REJECT_CLASS = ("reject", "relate")


def load_arm(path: Path) -> dict:
    """The one arm of a tagged ladder artifact."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if len(doc["arms"]) != 1:
        raise SystemExit(f"{path}: expected one arm, found {sorted(doc['arms'])}")
    (arm,) = doc["arms"].values()
    return arm


def merge_votes(arm: dict) -> dict:
    return {r["id"]: r for r in arm["queues"]["merges"]["per_row"]}


def _vote(row, rep):
    return row["votes"][rep] if row and len(row["votes"]) > rep else None


def gates(first: dict, second: dict, panel: dict, rep: int, *,
          reject_gate_2: float, accept_gate: float) -> dict:
    rej = {"n": 0, "bad": 0, "bad_ids": [], "links_filed": 0,
           "links_on_panel_relate": 0}
    acc = {"n": 0, "bad": 0, "bad_ids": []}
    splits = []
    for rid, row in first.items():
        a, b = _vote(row, rep), _vote(second.get(rid), rep)
        if not a or not b:
            continue
        ca, cb = (v["verdict"] in REJECT_CLASS for v in (a, b))
        if {a["verdict"], b["verdict"]} & {"accept"} and (ca or cb):
            splits.append(rid)
        mean = (a["confidence"] + b["confidence"]) / 2
        label = row["label"]
        if ca and cb and mean >= reject_gate_2:
            rej["n"] += 1
            if label != "reject":
                rej["bad"] += 1
                rej["bad_ids"].append(rid)
            if any(v["verdict"] == "relate" for v in (a, b)):
                rej["links_filed"] += 1
                rej["links_on_panel_relate"] += (
                    (panel.get(rid) or {}).get("panel_verdict") == "relate")
        if (a["verdict"] == b["verdict"] == "accept" and mean >= accept_gate
                and (panel.get(rid) or {}).get("low_differential") is False):
            acc["n"] += 1
            if label != "accept":
                acc["bad"] += 1
                acc["bad_ids"].append(rid)
    return {"two_vote_reject_class": rej, "two_vote_accept_not_lowdiff": acc,
            "splits": {"n": len(splits), "rows": sorted(splits)}}


def single_vote(votes: dict, rep: int, gate: float) -> dict:
    out = {"n": 0, "bad": 0, "relate": 0}
    for row in votes.values():
        v = _vote(row, rep)
        if v and v["verdict"] in REJECT_CLASS and v["confidence"] >= gate:
            out["n"] += 1
            out["bad"] += row["label"] != "reject"
            out["relate"] += v["verdict"] == "relate"
    return out


def build(first_arm: dict, second_arm: dict, panel_doc: dict, *,
          reject_gate: float = 0.8, reject_gate_2: float = 0.7,
          accept_gate: float = 0.6) -> dict:
    panel = {r["id"]: r for r in panel_doc["merges"]}
    first, second = merge_votes(first_arm), merge_votes(second_arm)
    reps = min(first_arm["replicates"], second_arm["replicates"])
    return {
        "first": {"arm": first_arm["arm"], "model": first_arm["model"]},
        "second": {"arm": second_arm["arm"], "model": second_arm["model"]},
        "gates": {"reject_gate": reject_gate, "reject_gate_2": reject_gate_2,
                  "accept_gate": accept_gate},
        "replicates": [gates(first, second, panel, k,
                             reject_gate_2=reject_gate_2,
                             accept_gate=accept_gate) for k in range(reps)],
        "single_vote": {
            arm["arm"]: [single_vote(votes, k, reject_gate)
                         for k in range(arm["replicates"])]
            for arm, votes in ((first_arm, first), (second_arm, second))},
        "relate_by_arm": {
            arm["arm"]: arm["queues"]["merges"].get("relate")
            for arm in (first_arm, second_arm)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--first", type=Path, required=True)
    ap.add_argument("--second", type=Path, required=True)
    ap.add_argument("--reject-gate", type=float, default=0.8)
    ap.add_argument("--reject-gate-2", type=float, default=0.7)
    ap.add_argument("--accept-gate", type=float, default=0.6)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--force", action="store_true",
                    help="replace an existing --out (never silent)")
    args = ap.parse_args(argv)
    if args.out.exists() and not args.force:
        raise SystemExit(f"{args.out} exists; pass --force or pick a new name")
    doc = build(load_arm(args.first), load_arm(args.second),
                json.loads(PANEL.read_text(encoding="utf-8")),
                reject_gate=args.reject_gate,
                reject_gate_2=args.reject_gate_2,
                accept_gate=args.accept_gate)
    doc = {"built_by": "evals/merge_relate_gates.py",
           "inputs": [args.first.name, args.second.name],
           "panel": PANEL.name, **doc}
    args.out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: doc[k] for k in ("replicates", "single_vote")},
                     indent=1))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
