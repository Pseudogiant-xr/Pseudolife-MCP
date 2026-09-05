#!/usr/bin/env python
"""Paired same-window verdict for an extraction-prompt re-cut on the
LongMemEval knowledge-update oracle slice.

Two ``longmemeval_bench.py`` runs over the SAME 78 questions, same
extractor endpoint, same answerer/judge, differing only in
``--system-prompt-file``: the pre arm is the shipped prompt, the post arm
the candidate. Per arm (rag / cortex / hybrid / the derived cascade) the
design is paired binary, so the test is McNemar's exact test on the
discordant pairs, and the ``rag`` arm - built from raw turns, never
touching the extractor - is the noise floor any cortex/hybrid effect must
clear (the convention of ``analyze_extractor_comparison.py``, whose
numbers this reproduces on the sg2 v5/v10 pair).

Two readouts the 2026-09-05 prompt-example audit asked for on top of that:

* ``leave_out`` - accuracies recomputed with the questions whose gold the
  OLD prompt's worked examples stated (``affe2881``, ``89941a94``) dropped
  from both arms, so the comparison cannot be flattered by a lifted
  example on either side.
* ``count_class`` - per-arm correct counts over the digit-gold and
  spelled-gold questions, the census split ``c2op-count-verdict.json``
  reports, so a count-rule regression shows as a class, not one row.
* ``frozen_total`` - the seven ``frozen-total (rule-recoverable)`` losses
  of ``c2op-count-census.json`` row by row: the count-exclusion rule was
  written to recover them, and its worked example was cut from one of
  them, so the re-cut must show the rule still recovers the OTHER six.

Gate (pre-registered 2026-09-06, memory): PASS when no arm of the post run
is significantly below the pre run (McNemar exact p < 0.05 with negative
net = FAIL), the rag control sits at net 0, and none of the six
non-lifted frozen-total questions loses cortex or cascade correctness.

Usage (repo root):
  PYTHONPATH=. python evals/recut_verdict.py --pre recut-v10 --post recut-v11 \\
      --out evals/results/prompt-recut-v11-ku-paired-verdict.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.analyze_extractor_comparison import mcnemar_exact  # noqa: E402
from evals.analyze_frozen_totals import NUM, nums  # noqa: E402
from evals.replicate import arm_correct, is_judged, load_rows  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
ARMS = ("rag", "cortex", "hybrid", "cascade")
# Gold answers stated by the shipped (v10) prompt's worked examples
# (tests/test_prompt_example_lifts.py KNOWN_GOLD_COLLISIONS).
LIFTED_QIDS = ("affe2881", "89941a94")
# c2op-count-census.json "frozen-total (rule-recoverable)" rows.
FROZEN_QIDS = ("01493427", "41698283", "45dc21b6", "4d6b87c8",
               "5831f84d", "a2f3aa27", "affe2881")


def _load(tag: str, dataset: str, extractor: str) -> dict[str, dict]:
    path = RESULTS / f"longmemeval-ku-{dataset}-{extractor}-{tag}.jsonl"
    rows = load_rows(path)
    if not rows or not all(is_judged(r) for r in rows):
        raise SystemExit(f"{path.name}: missing or not fully judged")
    return {r["question_id"]: r for r in rows}


def _compare(pre: dict[str, dict], post: dict[str, dict],
             qids: list[str], floor: int) -> dict:
    out = {}
    for arm in ARMS:
        b = sum(arm_correct(post[q], arm) and not arm_correct(pre[q], arm)
                for q in qids)
        c = sum(not arm_correct(post[q], arm) and arm_correct(pre[q], arm)
                for q in qids)
        acc_post = sum(arm_correct(post[q], arm) for q in qids) / len(qids)
        acc_pre = sum(arm_correct(pre[q], arm) for q in qids) / len(qids)
        out[arm] = {
            "accuracy": round(acc_post, 4),
            "reference_accuracy": round(acc_pre, 4),
            "delta": round(acc_post - acc_pre, 4),
            "wins": b, "losses": c, "net": b - c,
            "p_mcnemar_exact": round(mcnemar_exact(b, c), 4),
            "exceeds_noise_floor": abs(b - c) > floor,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pre", required=True, help="tag of the shipped-prompt run")
    ap.add_argument("--post", required=True, help="tag of the candidate run")
    ap.add_argument("--dataset", default="oracle")
    ap.add_argument("--extractor", default="qwen-27b")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    pre = _load(a.pre, a.dataset, a.extractor)
    post = _load(a.post, a.dataset, a.extractor)
    qids = sorted(pre)
    if set(qids) != set(post):
        raise SystemExit("pre and post runs cover different questions")

    identical = sum(pre[q]["contexts"]["rag"] == post[q]["contexts"]["rag"]
                    for q in qids)
    rag_flips = [q for q in qids
                 if pre[q]["rag_correct"] != post[q]["rag_correct"]]
    floor = {"identical_rag_context": f"{identical}/{len(qids)}",
             "rag_disagreement": len(rag_flips), "flipped_qids": rag_flips}

    full = _compare(pre, post, qids, len(rag_flips))
    kept = [q for q in qids if q not in LIFTED_QIDS]
    leave_out = {"dropped": [q for q in LIFTED_QIDS if q in pre],
                 "n": len(kept),
                 "comparisons": _compare(pre, post, kept, len(rag_flips))}

    frozen = {}
    for q in FROZEN_QIDS:
        if q not in pre:
            continue
        frozen[q] = {
            "lifted_example_source": q in LIFTED_QIDS,
            "gold": pre[q]["answer"],
            **{f"{arm}_{side}": arm_correct(rows[q], arm)
               for side, rows in (("pre", pre), ("post", post))
               for arm in ("cortex", "cascade")},
            "answer_in_current_fact": {
                "pre": pre[q]["answer_in_current_fact"],
                "post": post[q]["answer_in_current_fact"]},
        }
    six = [q for q in frozen if not frozen[q]["lifted_example_source"]]
    frozen_losses = [
        q for q in six
        if (frozen[q]["cortex_pre"] and not frozen[q]["cortex_post"])
        or (frozen[q]["cascade_pre"] and not frozen[q]["cascade_post"])]

    # Count-class census, the split c2op-count-verdict.json reports:
    # digit golds carry a numeral, spelled golds a number word only.
    digit = [q for q in qids if NUM.search(str(pre[q]["answer"]))]
    spelled = [q for q in qids if q not in digit and nums(pre[q]["answer"])]
    count_class = {
        f"{name}_gold_n{len(group)}": {
            arm: {"pre": sum(arm_correct(pre[q], arm) for q in group),
                  "post": sum(arm_correct(post[q], arm) for q in group)}
            for arm in ("cortex", "hybrid", "cascade")}
        for name, group in (("digit", digit), ("spelled", spelled))}

    regressions = [arm for arm, e in full.items()
                   if e["net"] < 0 and e["p_mcnemar_exact"] < 0.05]
    checks = {
        "no_arm_significantly_below_pre": not regressions,
        "rag_control_net_zero": full["rag"]["net"] == 0,
        "six_frozen_totals_no_loss": not frozen_losses,
    }
    verdict = {
        "what": ("extraction-prompt example re-cut, paired same-window "
                 "KU-oracle e2e: pre = shipped prompt, post = candidate; "
                 "same extractor endpoint, answerer and judge"),
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "pre_tag": a.pre, "post_tag": a.post,
        "dataset": a.dataset, "extractor": a.extractor, "n": len(qids),
        "noise_floor_rag_control": floor,
        "comparisons": full,
        "leave_out": leave_out,
        "count_class": count_class,
        "frozen_total": frozen,
        "frozen_total_losses_on_six": frozen_losses,
        "significant_regressions": regressions,
        "checks": checks,
        "gate": "PASS" if all(checks.values()) else "FAIL",
    }
    Path(a.out).write_text(json.dumps(verdict, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
