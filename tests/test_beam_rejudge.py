"""Unit tests for evals/beam_rejudge.py's pure parts (no CLI, no GPU).

The frontier re-judge replays recorded responses through an injected judge
callable; everything below exercises the offline machinery — output naming,
arm detection, row pairing, summary deltas, and the seeded stability
sample — with fake judges.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import beam_rejudge  # noqa: E402


def _row(i: int = 0, qtype: str = "abstention", **scores) -> dict:
    base = {"chat_id": "1", "tier": "100K", "type": qtype, "index": i,
            "question": "q?", "difficulty": "medium", "rubric": ["r1", "r2"],
            "rag_response": "answer A", "rag_score": 0.5,
            "rag_score_intfaithful": 0.0,
            "hybrid_response": "answer B", "hybrid_score": 1.0,
            "hybrid_score_intfaithful": 1.0}
    base.update(scores)
    return base


def test_out_path_is_tagged_and_never_the_source(tmp_path):
    src = tmp_path / "beam-100K-qwen-27b-beam100k-qwen38.jsonl"
    out = beam_rejudge.out_path_for(src, "opus5")
    assert out != src
    assert out.name == "beam-100K-qwen-27b-beam100k-qwen38.rejudge-opus5.jsonl"
    assert out.parent == src.parent


def test_detect_arms_from_rows_in_canonical_order():
    rows = [_row()]
    assert beam_rejudge.detect_arms(rows) == ("rag", "hybrid")
    rows[0]["cortex_score"] = 0.0
    rows[0]["hybrid_ev_score"] = 0.0
    assert beam_rejudge.detect_arms(rows) == (
        "rag", "cortex", "hybrid", "hybrid_ev")


def test_rejudge_row_pairs_original_and_new_scores():
    def judge(system, user, **_):
        return '{"score": 1.0}'

    out = beam_rejudge.rejudge_row(_row(), ("rag", "hybrid"),
                                   "j <question> <rubric_item> "
                                   "<llm_response>", judge)
    assert out["rag_score"] == 1.0 and out["rag_score_orig"] == 0.5
    assert out["hybrid_score"] == 1.0 and out["hybrid_score_orig"] == 1.0
    assert out["rag_judge_failures"] == 0
    assert len(out["rag_judge"]) == 2                    # one per rubric item
    assert (out["chat_id"], out["type"], out["index"]) == ("1", "abstention", 0)


def test_rejudge_row_counts_unparseable_as_failures():
    out = beam_rejudge.rejudge_row(_row(), ("rag",), "j", lambda *a, **k: "?")
    assert out["rag_judge_failures"] == 2
    assert out["rag_score"] == 0.0                       # no scored items

    def judge(system, user, **_):
        raise RuntimeError("cli broke")

    out = beam_rejudge.rejudge_row(_row(), ("rag",), "j", judge)
    assert out["rag_judge_failures"] == 2                # errors never abort a row


def test_summarize_reports_deltas_per_arm_and_type():
    rows = [
        beam_rejudge.rejudge_row(_row(0, "abstention"), ("rag", "hybrid"),
                                 "j", lambda *a, **k: '{"score": 1.0}'),
        beam_rejudge.rejudge_row(_row(1, "event_ordering"), ("rag", "hybrid"),
                                 "j", lambda *a, **k: '{"score": 0.0}'),
    ]
    s = beam_rejudge.summarize(rows, ("rag", "hybrid"), "claude-opus-5",
                               "src.jsonl")
    assert s["judge"] == "claude-opus-5"
    assert s["n_questions"] == 2
    assert s["arms"]["rag"]["score"] == 0.5              # (1.0 + 0.0) / 2
    assert s["arms"]["rag"]["score_orig"] == 0.5
    assert s["arms"]["rag"]["delta"] == 0.0
    assert s["arms"]["hybrid"]["score_orig"] == 1.0
    assert s["arms"]["hybrid"]["delta"] == -0.5
    assert s["types"]["abstention"]["rag"] == 1.0
    assert s["types"]["abstention"]["rag_orig"] == 0.5
    assert s["types"]["event_ordering"]["hybrid"] == 0.0


def test_stability_pairs_are_seeded_and_capped():
    rows = [_row(i) for i in range(10)]
    a = beam_rejudge.stability_pairs(rows, ("rag", "hybrid"), 5)
    b = beam_rejudge.stability_pairs(rows, ("rag", "hybrid"), 5)
    assert a == b and len(a) == 5                        # deterministic
    assert beam_rejudge.stability_pairs(rows, ("rag",), 99) == \
        beam_rejudge.stability_pairs(rows, ("rag",), 99)
    assert len(beam_rejudge.stability_pairs(rows, ("rag",), 99)) == 10  # capped


def test_stability_report_measures_item_agreement():
    row = beam_rejudge.rejudge_row(_row(), ("rag",), "j",
                                   lambda *a, **k: '{"score": 1.0}')
    # Second pass disagrees on every item.
    rep = beam_rejudge.stability_report(
        [row], [("1", "abstention", 0, "rag")], "j",
        lambda *a, **k: '{"score": 0.5}')
    assert rep["n_pairs"] == 1 and rep["n_items"] == 2
    assert rep["item_agreement"] == 0.0
    assert rep["mean_abs_delta"] == 0.5


def test_unknown_requested_arm_is_loud():
    with pytest.raises(SystemExit):
        beam_rejudge.detect_arms([_row()], only="rag,cortex")  # no cortex col
