"""Unit tests for the gold-answer leak check (SR-TTT retraction guard).

A retrieval win counted on a row whose gold answer was already sitting in
the non-retrieval input is not a retrieval win. The checker finds those
rows in a committed artifact and reports each arm's score with them
excluded.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import leak_check  # noqa: E402
from leak_check import answer_present, check_row, check_rows  # noqa: E402


def test_answer_present_matches_on_word_boundaries():
    assert answer_present("I finally moved to Sydney last year.", "Sydney")
    assert answer_present("she uses a Trek Domane", "trek domane")
    assert answer_present("the port is open", "Portland") is False
    assert answer_present("nothing relevant", "Sydney") is False


def test_answer_present_ignores_punctuation_and_case():
    assert answer_present("Answer: it's a Trek-Domane, obviously",
                          "trek domane")


def test_answer_present_is_none_for_untestable_answers():
    """Very short or yes/no golds match by chance; they are reported as
    untestable, never silently counted as clean."""
    assert answer_present("yes I did", "yes") is None
    assert answer_present("the count was 3", "3") is None
    assert answer_present("anything", "") is None


def test_check_row_flags_a_gold_answer_sitting_in_the_question():
    row = {"chat_id": "1", "type": "recall", "index": 0,
           "question": "After you moved to Sydney, what did you do?",
           "reference_answer": "Sydney", "rag_score": 1.0}
    v = check_row(row)
    assert v["leak"] is True and "question" in v["sites"]
    assert v["id"] == "1/recall[0]"


def test_check_row_flags_a_context_free_arm_that_was_served_context():
    """The no-memory arm's context must be empty. A non-empty one holding
    the gold answer makes its score meaningless — and would flatter
    memory-off in exactly the comparison the arm exists to make."""
    row = {"question_id": "q7", "question": "Where did I move?",
           "answer": "Sydney", "nomem_correct": True,
           "contexts": {"nomem": "You previously moved to Sydney."}}
    v = check_row(row)
    assert v["leak"] is True and "nomem_context" in v["sites"]


def test_check_row_clean_row_is_not_flagged():
    row = {"question_id": "q1", "question": "Where did I move?",
           "answer": "Sydney", "contexts": {"nomem": ""}, "rag_correct": True}
    v = check_row(row)
    assert v["leak"] is False and v["sites"] == [] and v["testable"] is True


def _rows():
    return [
        # leaked: the gold is in the question itself
        {"chat_id": "1", "type": "t", "index": 0,
         "question": "Since moving to Sydney, how is it?",
         "reference_answer": "Sydney", "rag_score": 1.0, "refind_score": 1.0},
        {"chat_id": "1", "type": "t", "index": 1,
         "question": "Which bike did I buy?",
         "reference_answer": "Trek Domane", "rag_score": 0.0,
         "refind_score": 1.0},
        {"chat_id": "1", "type": "t", "index": 2,
         "question": "Which camera did I buy?",
         "reference_answer": "Nikon Zf", "rag_score": 0.0,
         "refind_score": 0.0},
    ]


def test_check_rows_reports_arm_means_with_leaked_rows_excluded():
    summary = check_rows(_rows())
    assert summary["n_rows"] == 3 and summary["n_leaked"] == 1
    assert summary["leaked"] == ["1/t[0]"]
    assert summary["arms"]["rag"]["all"] == 0.3333
    assert summary["arms"]["rag"]["leak_free"] == 0.0
    assert summary["arms"]["refind"]["all"] == 0.6667
    assert summary["arms"]["refind"]["leak_free"] == 0.5
    assert summary["arms"]["refind"]["n_leak_free"] == 2


def test_check_rows_counts_boolean_judged_arms():
    """LongMemEval artifacts judge with `{arm}_correct` booleans; BEAM with
    `{arm}_score` floats. Both shapes report."""
    rows = [{"question_id": "a", "question": "q", "answer": "Sydney",
             "rag_correct": True},
            {"question_id": "b", "question": "q", "answer": "Perth",
             "rag_correct": False}]
    summary = check_rows(rows)
    assert summary["arms"]["rag"]["all"] == 0.5
    assert summary["n_leaked"] == 0


def test_check_rows_counts_untestable_rows_separately():
    rows = [{"question_id": "a", "question": "did you?", "answer": "yes",
             "rag_score": 1.0}]
    summary = check_rows(rows)
    assert summary["n_untestable"] == 1 and summary["n_leaked"] == 0
    assert summary["untestable"] == ["a"]


def test_untestable_reason_separates_no_gold_from_a_trivial_gold():
    """Five of BEAM's ten question types are rubric-judged and carry NO
    gold string (200 of the 400 rows in the 2026-08-21 artifact) — a
    different fact from a gold too short to test, and reported as one."""
    assert leak_check.untestable_reason("") == "no_gold"
    assert leak_check.untestable_reason("   ") == "no_gold"
    assert leak_check.untestable_reason("yes") == "trivial_gold"
    assert leak_check.untestable_reason("78") == "trivial_gold"
    assert leak_check.untestable_reason("Trek Domane") is None


def test_check_rows_reports_a_testable_only_mean_beside_the_leak_free_one():
    """Untestable rows are not leaked, so they sit inside `leak_free` —
    which means that mean is over rows the check could not actually
    examine. The testable-only mean says what the check can stand
    behind."""
    rows = [{"question_id": "a", "question": "summarise", "answer": "",
             "rag_score": 1.0},
            {"question_id": "b", "question": "which bike?",
             "answer": "Trek Domane", "rag_score": 0.0},
            {"question_id": "c", "question": "since the Nikon Zf arrived?",
             "answer": "Nikon Zf", "rag_score": 1.0}]
    summary = check_rows(rows)
    assert summary["n_leaked"] == 1                     # row c names its gold
    assert summary["arms"]["rag"]["leak_free"] == 0.5   # rows a + b
    assert summary["arms"]["rag"]["n_testable"] == 1    # only row b
    assert summary["arms"]["rag"]["leak_free_testable"] == 0.0


def test_check_rows_breaks_untestable_down_by_reason():
    rows = [{"question_id": "a", "question": "summarise", "answer": "",
             "rag_score": 1.0},
            {"question_id": "b", "question": "did you?", "answer": "yes",
             "rag_score": 1.0},
            {"question_id": "c", "question": "which bike?",
             "answer": "Trek Domane", "rag_score": 1.0}]
    summary = check_rows(rows)
    assert summary["n_untestable"] == 2
    assert summary["untestable_reasons"] == {"no_gold": 1, "trivial_gold": 1}


def test_main_writes_an_artifact_and_exits_nonzero_on_a_leak(tmp_path):
    src = tmp_path / "rows.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in _rows()),
                   encoding="utf-8")
    out = tmp_path / "report.json"
    code = leak_check.main(["--in", str(src), "--out", str(out)])
    assert code == 1                                   # leaks are a gate
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["n_leaked"] == 1 and report["source"] == "rows.jsonl"
    assert report["arms"]["refind"]["leak_free"] == 0.5


def test_main_writes_the_artifact_even_when_clean(tmp_path):
    """Every bench writes a file — a clean check that leaves no artifact
    cannot be cited later."""
    src = tmp_path / "rows.jsonl"
    src.write_text(json.dumps(
        {"question_id": "a", "question": "which bike?",
         "answer": "Trek Domane", "rag_correct": True}), encoding="utf-8")
    out = tmp_path / "clean.json"
    assert leak_check.main(["--in", str(src), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["n_leaked"] == 0


def test_main_defaults_the_artifact_beside_the_input(tmp_path):
    src = tmp_path / "beam-100K-qwen-27b-tag.jsonl"
    src.write_text(json.dumps(
        {"question_id": "a", "question": "which bike?",
         "answer": "Trek Domane", "rag_score": 1.0}), encoding="utf-8")
    assert leak_check.main(["--in", str(src)]) == 0
    assert (tmp_path / "beam-100K-qwen-27b-tag.leakcheck.json").exists()
