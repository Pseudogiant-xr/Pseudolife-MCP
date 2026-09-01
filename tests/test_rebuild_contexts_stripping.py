"""rebuild_contexts.py must strip EVERY arm's verdict, not three of them.

It rewrites the cortex/hybrid contexts under new serving knobs and strips
the verdicts so the answer phase re-runs. A comparator arm's verdict left
behind is a stale judgement sitting beside freshly rebuilt contexts — and
`leak_check.py` does not filter on judged-ness, so a half-answered file
would produce a real-looking artifact whose arms are averaged over
different row sets.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import replicate  # noqa: E402


def _row() -> dict:
    row = {"question_id": "q1", "contexts": {"rag": "r", "cortex": "c",
                                             "hybrid": "h", "refind": "f",
                                             "nomem": ""}}
    for arm in ("rag", "cortex", "hybrid", "refind", "nomem", "hybrid_ctg"):
        row[f"{arm}_response"] = "resp"
        row[f"{arm}_correct"] = True
        row[f"{arm}_context_tokens"] = 10
        row[f"{arm}_answerable_judge"] = True
    return row


def test_is_judge_field_is_the_one_rule_both_strippers_use():
    assert replicate.is_judge_field("refind_correct")
    assert replicate.is_judge_field("hybrid_ctg_context_tokens")
    assert replicate.is_judge_field("nomem_response")
    # answerability_probe --judge verdicts are judged fields too: a
    # rebuilt or replicated row must not carry a stale one.
    assert replicate.is_judge_field("rag_answerable_judge")
    # row fields that merely look similar must survive
    for keep in ("answer_in_current_fact", "gold_in_question",
                 "refind_top_k", "question_id", "contexts", "answer"):
        assert not replicate.is_judge_field(keep)


def test_rebuild_contexts_strips_every_arm_not_just_the_canonical_three():
    import rebuild_contexts

    row = _row()
    rebuild_contexts.strip_verdicts(row)
    assert not [k for k in row if replicate.is_judge_field(k)]
    # contexts survive: they are what the answer phase re-answers
    assert row["contexts"]["refind"] == "f"
    assert row["question_id"] == "q1"
