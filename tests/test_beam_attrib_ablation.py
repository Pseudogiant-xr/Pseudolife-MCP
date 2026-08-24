"""Unit tests for evals/beam_attrib_ablation.py's pure parts (no GPU).

The attribution ablation re-answers a completed BEAM run's persisted
contexts with the pre-Phase-1 answer prompt, holding budget, ordinals,
and judge fixed — the per-row paired delta against the source run's
recorded scores is then the answer-prompt term alone. Everything below
runs offline with fake chat callables.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import beam_adapter  # noqa: E402
import beam_attrib_ablation as aba  # noqa: E402


def test_old_prompt_is_new_prompt_minus_contradiction_sentence():
    """The isolation claim rests on the two prompts differing by exactly
    one sentence — pin it as a string identity so a future prompt edit
    cannot silently turn this into a multi-term ablation."""
    assert "CONTRADICTORY" in beam_adapter._BEAM_ANSWER_SYSTEM
    assert "CONTRADICTORY" not in aba.OLD_ANSWER_SYSTEM
    assert beam_adapter._BEAM_ANSWER_SYSTEM.replace(
        aba.CONTRADICTION_SENTENCE, "") == aba.OLD_ANSWER_SYSTEM


def test_out_path_derives_from_source_and_tag():
    src = Path("evals/results/beam-100K-qwen-27b-p1-b16.jsonl")
    out = aba.out_path_for(src, "oldprompt")
    assert out.name == "beam-100K-qwen-27b-p1-b16-ablate-oldprompt.jsonl"
    assert out.parent == src.parent


def _source_row(i: int = 0, qtype: str = "summarization") -> dict:
    return {"chat_id": "1", "tier": "100K", "type": qtype, "index": i,
            "question": "q?", "difficulty": "easy", "rubric": ["r1", "r2"],
            "rag_top_k": 16, "hybrid_top_k": 16,
            "contexts": {"rag": "ctx-rag", "hybrid": "ctx-hyb"},
            "rag_score": 1.0, "rag_score_intfaithful": 1.0,
            "hybrid_score": 0.5, "hybrid_score_intfaithful": 0.0}


def test_ablate_row_reanswers_each_arm_with_old_prompt():
    systems = []

    def chat(system, user, **_):
        if system:                                # answer call
            systems.append(system)
            return "old-prompt answer"
        return '{"score": 1.0}'                   # judge call

    out = aba.ablate_row(_source_row(), "j <question> <rubric_item> "
                         "<llm_response>", chat)
    assert out["rag_score"] == 1.0 and out["hybrid_score"] == 1.0
    assert out["rag_response"] == "old-prompt answer"
    assert len(out["rag_judge"]) == 2
    assert (out["chat_id"], out["type"], out["index"]) == \
        ("1", "summarization", 0)
    # baseline scores ride along for paired summarization later
    assert out["source_rag_score"] == 1.0
    assert out["source_hybrid_score"] == 0.5
    assert systems == [aba.OLD_ANSWER_SYSTEM] * 2


def test_ablate_row_requires_persisted_contexts():
    row = _source_row()
    del row["contexts"]
    with pytest.raises(SystemExit, match="contexts"):
        aba.ablate_row(row, "j", lambda *a, **k: "x")


def test_pending_rows_skips_done_keys():
    rows = [_source_row(0), _source_row(1)]
    done = {("1", "summarization", 0)}
    assert [r["index"] for r in aba.pending_rows(rows, done)] == [1]


def test_summarize_reports_paired_deltas_per_arm_and_type():
    def chat(system, user, **_):
        return "a" if system else '{"score": 0.5}'

    jp = "j <question> <rubric_item> <llm_response>"
    ab_rows = [aba.ablate_row(_source_row(0, "abstention"), jp, chat),
               aba.ablate_row(_source_row(1, "summarization"), jp, chat)]
    s = aba.summarize(ab_rows, source_name="p1-b16")
    # ablation scores 0.5 everywhere; source rag 1.0, hybrid 0.5
    assert s["arms"]["rag"]["score"] == 0.5
    assert s["arms"]["rag"]["source_score"] == 1.0
    assert s["arms"]["rag"]["paired_delta_new_minus_old"] == 0.5
    assert s["arms"]["hybrid"]["paired_delta_new_minus_old"] == 0.0
    assert "paired_delta_se" in s["arms"]["rag"]
    assert s["types"]["abstention"]["rag_delta"] == 0.5
    assert s["source_run"] == "p1-b16"
    assert s["n_questions"] == 2
