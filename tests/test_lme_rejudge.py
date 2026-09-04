"""Unit tests for evals/lme_rejudge.py's pure parts (no CLI, no GPU, no DB).

The second-judge-family re-judge replays a LongMemEval run's recorded
per-arm responses through an injected judge callable. Everything below
exercises the offline machinery with fake judges: prompt selection by
question type (asserting the harness's OWN prompt objects are the ones
sent, so only the judge family changes), verdict parsing, output naming,
the refuse-overwrite / resume contract, the summary's agreement and
leak-exclusion arithmetic, the seeded stability sample, and the argv
handed to the paired-comparison tool.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

import longmemeval_bench  # noqa: E402
import lme_rejudge  # noqa: E402

TAG = "opus5"


def _row(i: int = 0, qtype: str = "knowledge-update", *,
         rag: bool = True, hybrid: bool = True, leak: bool = False) -> dict:
    """One judged LongMemEval row in the raglite-all-fresh shape."""
    return {
        "question_id": f"q{i}",
        "question": f"question {i}?",
        "question_type": qtype,
        "answer": "Portland Oregon",
        "gold_in_question": leak,
        "rag_response": "they moved to Portland Oregon",
        "rag_correct": rag,
        "rag_context_tokens": 1000,
        "hybrid_response": "Portland",
        "hybrid_correct": hybrid,
        "hybrid_context_tokens": 1200,
    }


# ── naming ────────────────────────────────────────────────────────────────
def test_output_paths_are_tagged_and_never_the_source(tmp_path):
    src = tmp_path / "longmemeval-all-oracle-qwen-27b-raglite-all-fresh.jsonl"
    out = lme_rejudge.out_path_for(src, TAG)
    assert out != src
    assert out.name == ("longmemeval-all-oracle-qwen-27b-raglite-all-fresh"
                        ".rejudge-opus5.jsonl")
    assert lme_rejudge.summary_path_for(out).name == (
        "longmemeval-all-oracle-qwen-27b-raglite-all-fresh"
        ".rejudge-opus5.summary.json")
    assert out.parent == src.parent


# ── arm detection ─────────────────────────────────────────────────────────
def test_detect_arms_from_response_keys_in_canonical_order():
    rows = [_row()]
    assert lme_rejudge.detect_arms(rows) == ("rag", "hybrid")
    rows[0]["cortex_response"] = "c"
    rows[0]["rag1_response"] = "r1"
    assert lme_rejudge.detect_arms(rows) == ("rag", "cortex", "hybrid", "rag1")


def test_detect_arms_narrows_and_is_loud_about_unknown_arms():
    rows = [_row()]
    assert lme_rejudge.detect_arms(rows, "hybrid") == ("hybrid",)
    with pytest.raises(SystemExit) as e:
        lme_rejudge.detect_arms(rows, "hybrid,cortex")
    assert "cortex" in str(e.value)


# ── prompt selection: the harness's own objects, not a copy ───────────────
def test_knowledge_update_rows_use_the_harness_ku_judge_prompt():
    assert lme_rejudge.judge_system_for(_row(qtype="knowledge-update")) is \
        longmemeval_bench._JUDGE_SYSTEM


def test_other_types_use_the_harness_generic_judge_prompt():
    for qtype in ("multi-session", "temporal-reasoning",
                  "single-session-user"):
        assert lme_rejudge.judge_system_for(_row(qtype=qtype)) is \
            longmemeval_bench._JUDGE_SYSTEM_GENERIC


def test_rows_without_a_question_type_keep_the_ku_prompt():
    row = _row()
    del row["question_type"]
    assert lme_rejudge.judge_system_for(row) is longmemeval_bench._JUDGE_SYSTEM


def test_judge_user_message_matches_the_harness_construction():
    row = _row()
    assert lme_rejudge.judge_user(row, "some answer") == (
        "Question: question 0?\n"
        "Correct answer: Portland Oregon\n"
        "Model response: some answer")


# ── verdict parsing ───────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("Yes.", True), ("YES — equivalent", True),
    ("no", False), ("No.", False), ("", False), ("  yes ", True),
    ("nope", False), ("The answer is yes", False),
])
def test_verdict_parsing_is_the_harness_rule(text, expected):
    assert lme_rejudge.parse_verdict(text) is expected


# ── row re-judging ────────────────────────────────────────────────────────
def test_rejudge_row_adds_the_tagged_verdict_and_keeps_the_original():
    calls = []

    def judge(system, user, **_):
        calls.append((system, user))
        return "no"

    out = lme_rejudge.rejudge_row(_row(), ("rag", "hybrid"), TAG, judge)
    assert out["rag_correct"] is True          # original preserved
    assert out["rag_correct_opus5"] is False   # second family
    assert out["hybrid_correct_opus5"] is False
    assert out["rag_context_tokens"] == 1000   # cost column carried forward
    assert out["question_type"] == "knowledge-update"
    assert out["answer"] == "Portland Oregon"
    assert out["gold_in_question"] is False
    assert len(calls) == 2
    assert all(s is longmemeval_bench._JUDGE_SYSTEM for s, _ in calls)


def test_rejudge_row_treats_a_dead_judge_call_as_a_no():
    out = lme_rejudge.rejudge_row(_row(), ("rag",), TAG,
                                  lambda system, user, **_: "")
    assert out["rag_correct_opus5"] is False


# ── summary arithmetic ────────────────────────────────────────────────────
def _six_rows() -> list[dict]:
    """6 rows: 3 KU, 3 multi-session; one KU row is a gold leak."""
    rows = [
        _row(0, "knowledge-update", rag=True, hybrid=True),
        _row(1, "knowledge-update", rag=True, hybrid=False),
        _row(2, "knowledge-update", rag=False, hybrid=True, leak=True),
        _row(3, "multi-session", rag=False, hybrid=True),
        _row(4, "multi-session", rag=True, hybrid=True),
        _row(5, "multi-session", rag=False, hybrid=False),
    ]
    # Second judge: rag agrees on 4 of 6, hybrid agrees on 6 of 6.
    new_rag = [True, False, False, False, False, False]
    for row, v in zip(rows, new_rag):
        row["rag_correct_opus5"] = v
        row["hybrid_correct_opus5"] = row["hybrid_correct"]
    return rows


def test_summarize_reports_both_judges_per_arm_and_the_agreement():
    s = lme_rejudge.summarize(_six_rows(), ("rag", "hybrid"), TAG,
                              "claude-opus-5", "src.jsonl", note="n")
    assert s["n_questions"] == 6
    assert s["judge"] == "claude-opus-5" and s["tag"] == TAG
    rag = s["arms"]["rag"]
    assert rag["accuracy_orig"] == pytest.approx(3 / 6, abs=1e-4)
    assert rag["accuracy"] == pytest.approx(1 / 6, abs=1e-4)
    assert rag["delta"] == pytest.approx(-2 / 6, abs=1e-4)
    assert rag["agreement"] == pytest.approx(4 / 6, abs=1e-4)
    hybrid = s["arms"]["hybrid"]
    assert hybrid["accuracy"] == pytest.approx(hybrid["accuracy_orig"])
    assert hybrid["agreement"] == 1.0
    assert s["note"] == "n" and s["source"] == "src.jsonl"


def test_summarize_reports_per_type_under_both_judges():
    s = lme_rejudge.summarize(_six_rows(), ("rag", "hybrid"), TAG,
                              "claude-opus-5", "src.jsonl")
    ku = s["types"]["knowledge-update"]
    assert ku["n"] == 3
    assert ku["rag_orig"] == pytest.approx(2 / 3, abs=1e-4)
    assert ku["rag"] == pytest.approx(1 / 3, abs=1e-4)
    ms = s["types"]["multi-session"]
    assert ms["n"] == 3 and ms["rag"] == 0.0


def test_summarize_applies_the_gold_leak_exclusion_and_records_the_ids():
    s = lme_rejudge.summarize(_six_rows(), ("rag", "hybrid"), TAG,
                              "claude-opus-5", "src.jsonl")
    lc = s["leak_check"]
    assert lc["n_leaked"] == 1 and lc["leaked"] == ["q2"]
    assert lc["n_leak_free"] == 5
    # q2 is the only excluded row: rag orig 3/6 -> 3/5, new 1/6 -> 1/5.
    assert lc["arms"]["rag"]["accuracy_orig"] == pytest.approx(3 / 5, abs=1e-4)
    assert lc["arms"]["rag"]["accuracy"] == pytest.approx(1 / 5, abs=1e-4)
    # The headline means still span every row (the raglite-all-fresh rule).
    assert s["arms"]["rag"]["accuracy_orig"] == pytest.approx(3 / 6, abs=1e-4)


# ── output contract: refuse-overwrite and resume ──────────────────────────
def test_open_output_refuses_to_overwrite_an_existing_artifact(tmp_path):
    out = tmp_path / "run.rejudge-opus5.jsonl"
    out.write_text('{"question_id": "q0"}\n', encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        lme_rejudge.open_output(out, resume=False, force=False)
    assert "--resume" in str(e.value) or "--force" in str(e.value)
    assert out.read_text(encoding="utf-8")   # untouched


def test_open_output_resume_reports_the_rows_already_judged(tmp_path):
    out = tmp_path / "run.rejudge-opus5.jsonl"
    out.write_text('{"question_id": "q0"}\n{"question_id": "q3"}\n',
                   encoding="utf-8")
    assert lme_rejudge.open_output(out, resume=True, force=False) == {
        "q0", "q3"}


def test_open_output_force_clears_the_artifact(tmp_path):
    out = tmp_path / "run.rejudge-opus5.jsonl"
    out.write_text('{"question_id": "q0"}\n', encoding="utf-8")
    assert lme_rejudge.open_output(out, resume=False, force=True) == set()
    assert not out.exists()


def test_open_output_on_a_fresh_path_is_empty(tmp_path):
    assert lme_rejudge.open_output(tmp_path / "new.jsonl", resume=True,
                                   force=False) == set()


def test_resume_refuses_an_arm_the_existing_rows_never_judged(tmp_path):
    """Resuming with a WIDER --arms than the run that wrote the file would
    leave the new arm's column absent on every already-written row, which
    the summary would then read as a run of False verdicts."""
    out = tmp_path / "run.rejudge-opus5.jsonl"
    out.write_text(json.dumps({"question_id": "q0",
                               "rag_correct_opus5": True}) + "\n",
                   encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        lme_rejudge.open_output(out, resume=True, force=False,
                                arms=("rag", "hybrid"), tag=TAG)
    assert "hybrid" in str(e.value)
    # The arms the file already carries resume without complaint.
    assert lme_rejudge.open_output(out, resume=True, force=False,
                                   arms=("rag",), tag=TAG) == {"q0"}


def test_pending_rows_skip_the_ones_already_judged():
    rows = [_row(i) for i in range(4)]
    pending = lme_rejudge.pending_rows(rows, {"q1", "q3"})
    assert [r["question_id"] for r in pending] == ["q0", "q2"]


# ── stability sample (the second judge's own floor) ────────────────────────
def test_stability_pairs_are_seeded_and_capped():
    rows = [_row(i) for i in range(5)]
    a = lme_rejudge.stability_pairs(rows, ("rag", "hybrid"), 4)
    b = lme_rejudge.stability_pairs(list(reversed(rows)), ("rag", "hybrid"), 4)
    assert a == b and len(a) == 4
    assert len(lme_rejudge.stability_pairs(rows, ("rag",), 99)) == 5


def test_stability_report_measures_the_second_judges_self_agreement():
    rows = _six_rows()
    pairs = [("q0", "rag"), ("q1", "rag")]
    # q0 was judged True; the repeat says no -> one flip out of two.
    rep = lme_rejudge.stability_report(rows, pairs, TAG,
                                       lambda system, user, **_: "no")
    assert rep["n_pairs"] == 2
    assert rep["agreement"] == pytest.approx(0.5)


def test_merge_stability_weights_by_pair_count():
    merged = lme_rejudge.merge_stability([
        {"n_pairs": 2, "agreement": 1.0, "pairs": [1, 2]},
        {"n_pairs": 2, "agreement": 0.0, "pairs": [3, 4]},
    ])
    assert merged["n_pairs"] == 4 and merged["agreement"] == pytest.approx(0.5)


# ── the paired comparison handed on to beam_within_run_pairs ──────────────
def test_pairing_argv_uses_the_tagged_score_key_and_the_rejudge_rows():
    out = Path("evals/results/longmemeval-all-oracle-qwen-27b-"
               "raglite-all-fresh.rejudge-opus5.jsonl")
    argv = lme_rejudge.pairing_argv(out, ("rag", "hybrid", "cortex", "rag1"),
                                    TAG, note="n")
    assert "--score-key" in argv
    assert argv[argv.index("--score-key") + 1] == "correct_opus5"
    assert argv[argv.index("--type-key") + 1] == "question_type"
    # The whole re-judge stem is the tag: that lands the pairing beside the
    # rows it was computed from, as <stem>.arms-vs-rag.json.
    assert argv[argv.index("--prefix") + 1] == ""
    assert argv[argv.index("--tag") + 1] == (
        "longmemeval-all-oracle-qwen-27b-raglite-all-fresh.rejudge-opus5")
    # The control never pairs against itself; the derived cascade arm and the
    # token-matched cortex:rag1 pairing ride along when both arms are present.
    armlist = argv[argv.index("--arms") + 1].split(",")
    assert "rag" not in armlist
    assert armlist == ["hybrid", "cortex", "rag1", "cascade"]
    assert argv[argv.index("--pairs") + 1] == "cortex:rag1"


def test_pairing_argv_drops_the_cascade_when_cortex_was_not_rejudged():
    out = Path("evals/results/x-raglite.rejudge-opus5.jsonl")
    argv = lme_rejudge.pairing_argv(out, ("rag", "hybrid"), TAG)
    assert argv[argv.index("--arms") + 1] == "hybrid"
    assert "--pairs" not in argv


def test_pairing_argv_refuses_a_run_without_the_rag_control():
    out = Path("evals/results/x-raglite.rejudge-opus5.jsonl")
    with pytest.raises(SystemExit):
        lme_rejudge.pairing_argv(out, ("hybrid", "cortex"), TAG)
