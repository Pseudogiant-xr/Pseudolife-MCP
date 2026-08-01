#!/usr/bin/env python
"""Frozen-total census — the CPU forecast that gated the count-exclusion
op-prompt arm.

The c2op-guard gate (evals/results/c2op-guard-verdict.json) isolated the
KU damage mechanism as extraction-side: under the op prompt, count/total
UPDATES are re-routed into op:"add" member claims, freezing the stated
total at its first value. This script counts how many of the op run's lost
questions carry that signature — i.e. the recoverable ceiling of a prompt
rule excluding counts from op — before any GPU is spent.

Slot naming differs across extraction prompts, so joins are by VALUE: for
each question the op run lost vs the op-less control, ask whether the gold
count is present among control-bank CURRENT values but absent from op-bank
current values (the stale-total signature), and whether the op bank
carries numeric member facts (the re-route evidence). Spelled-out counts
("four Korean restaurants") normalize to digits before matching.

Usage (repo root, bank dumps present under evals/results/banks/):
  PYTHONPATH=. python evals/analyze_frozen_totals.py \
      --control ceiling-e2e --op c2op-e2e --out evals/results/c2op-count-census.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.replicate import load_rows, arm_correct  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "evals" / "results"
NUM = re.compile(r"[$€£]?\d[\d,.:]*")
# Spelled-out counts are still counts.
WORDS = {w: str(i) for i, w in enumerate(
    ("zero one two three four five six seven eight nine ten eleven twelve "
     "thirteen fourteen fifteen sixteen seventeen eighteen nineteen "
     "twenty").split())}
ARMS = ("cortex", "cascade")


def nums(text: object) -> set[str]:
    t = str(text).lower()
    found = set(NUM.findall(t))
    for w, d in WORDS.items():
        if re.search(rf"\b{w}\b", t):
            found.add(d)
    return found


def bank_facts(banks: Path, extractor: str, tag: str, qid: str):
    p = banks / f"oracle-{extractor}-{tag}" / f"{qid}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)["facts"]


def classify(qid: str, ctrl_row: dict, banks: Path, extractor: str,
             control_tag: str, op_tag: str) -> dict:
    gold = ctrl_row["answer"]
    gold_nums = nums(gold)
    cb = bank_facts(banks, extractor, control_tag, qid)
    ob = bank_facts(banks, extractor, op_tag, qid)
    if cb is None or ob is None:
        return {"qid": qid, "class": "bank-missing"}
    c_cur = [f for f in cb if f.get("status") == "current"]
    o_cur = [f for f in ob if f.get("status") == "current"]
    o_members = [f for f in ob if f.get("kind") == "member"]
    r = {
        "qid": qid,
        "question": ctrl_row["question"][:110],
        "gold": gold,
        "gold_numeric": bool(gold_nums),
        "gold_in_control_current": any(gold_nums & nums(f["value"]) for f in c_cur) if gold_nums else None,
        "gold_in_op_current": any(gold_nums & nums(f["value"]) for f in o_cur) if gold_nums else None,
        "op_numeric_members": [
            f"{f['entity']}/{f['attribute']}={f['value']}"
            for f in o_members if NUM.findall(str(f["value"]))],
        "op_member_count": len(o_members),
    }
    if r["gold_numeric"] and r["gold_in_control_current"] and not r["gold_in_op_current"]:
        r["class"] = "frozen-total (rule-recoverable)"
    elif r["gold_numeric"] and r["gold_in_control_current"]:
        r["class"] = "gold present both banks (loss is retrieval/answer-side)"
    elif not r["gold_numeric"]:
        r["class"] = "non-numeric gold (rule out of scope)"
    else:
        r["class"] = "gold absent from control too (extraction miss both)"
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default="oracle")
    ap.add_argument("--extractor", default="qwen-27b")
    ap.add_argument("--control", default="ceiling-e2e")
    ap.add_argument("--op", default="c2op-e2e")
    ap.add_argument("--banks", default=str(RES / "banks"),
                    help="bank-dump root (dumps are working-tree artifacts)")
    ap.add_argument("--out", default=str(RES / "c2op-count-census.json"))
    args = ap.parse_args()

    def rows(tag: str) -> dict[str, dict]:
        p = RES / f"longmemeval-ku-{args.dataset}-{args.extractor}-{tag}.jsonl"
        return {r["question_id"]: r for r in load_rows(p)}

    ctrl, op = rows(args.control), rows(args.op)
    if set(ctrl) != set(op):
        raise SystemExit("question sets differ between runs")

    banks = Path(args.banks)
    losses = {a: sorted(q for q in ctrl
                        if arm_correct(ctrl[q], a) and not arm_correct(op[q], a))
              for a in ARMS}
    gains = {a: sorted(q for q in ctrl
                       if not arm_correct(ctrl[q], a) and arm_correct(op[q], a))
             for a in ARMS}

    out = {"control": args.control, "op": args.op,
           "losses": losses, "gains": gains, "lost": [], "gain_risk": []}
    for qid in sorted(set(losses["cortex"]) | set(losses["cascade"])):
        c = classify(qid, ctrl[qid], banks, args.extractor, args.control, args.op)
        c["lost_arms"] = [a for a in ARMS if qid in losses[a]]
        out["lost"].append(c)
    for qid in sorted(set(gains["cortex"]) | set(gains["cascade"])):
        c = classify(qid, ctrl[qid], banks, args.extractor, args.control, args.op)
        c["gained_arms"] = [a for a in ARMS if qid in gains[a]]
        out["gain_risk"].append(c)

    out["summary"] = {
        "lost_by_class": dict(Counter(c["class"] for c in out["lost"])),
        "recoverable_cascade": sum(
            1 for c in out["lost"]
            if c["class"].startswith("frozen") and "cascade" in c["lost_arms"]),
        "recoverable_cortex": sum(
            1 for c in out["lost"]
            if c["class"].startswith("frozen") and "cortex" in c["lost_arms"]),
        "gains_with_numeric_members": sum(
            1 for c in out["gain_risk"] if c.get("op_numeric_members")),
    }

    dst = Path(args.out)
    dst.write_text(json.dumps(out, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(json.dumps(out["summary"], indent=2))
    print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
