"""Tests for evals/replicate.py — the replication/variance layer.

Pure-function tests only: no endpoints, no GPU, no Postgres. The module
must import without pulling ladder_sweep/torch (that is itself asserted).
"""
import json
import random
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import replicate  # noqa: E402


def _row(qid: str, judged: bool = True, correct: bool = True) -> dict:
    row = {
        "question_id": qid,
        "question": "q?",
        "answer": "a",
        "question_date": "2023/01/01",
        "contexts": {"rag": "r", "cortex": "c", "hybrid": "h"},
    }
    if judged:
        for arm in replicate.ARMS:
            row[f"{arm}_response"] = "resp"
            row[f"{arm}_correct"] = correct
            row[f"{arm}_context_tokens"] = 100
    return row


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                    encoding="utf-8")


def test_module_imports_light():
    # A clean subprocess: importing replicate must not pull heavy modules.
    evals_dir = str(Path(__file__).resolve().parents[1] / "evals")
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import replicate; "
        "banned = {'torch', 'ladder_sweep', 'longmemeval_bench'}; "
        "hit = sorted(banned & set(sys.modules)); "
        "sys.exit('heavy imports: ' + ', '.join(hit) if hit else 0)"
    )
    subprocess.run([sys.executable, "-c", code, evals_dir], check=True)


def test_result_file_matches_bench_convention(tmp_path):
    assert replicate.result_file("oracle", "e4b-ft", "arm1", tmp_path) == \
        tmp_path / "longmemeval-ku-oracle-e4b-ft-arm1.jsonl"
    assert replicate.result_file("oracle", "qwen-27b", "", tmp_path) == \
        tmp_path / "longmemeval-ku-oracle-qwen-27b.jsonl"


def test_replicate_tag():
    assert replicate.replicate_tag("arm1", 2) == "arm1-r2"
    assert replicate.replicate_tag("", 2) == "r2"


def test_strip_judged_removes_only_judge_fields():
    stripped = replicate.strip_judged([_row("q1")])[0]
    for arm in replicate.ARMS:
        assert f"{arm}_correct" not in stripped
        assert f"{arm}_response" not in stripped
        assert f"{arm}_context_tokens" not in stripped
    assert stripped["question_id"] == "q1"
    assert stripped["contexts"] == {"rag": "r", "cortex": "c", "hybrid": "h"}
    assert replicate.is_judged(_row("q1")) is True
    assert replicate.is_judged(stripped) is False


def test_discover_strict_suffix(tmp_path):
    rows = [_row("q1")]
    for name in [
        "longmemeval-ku-oracle-e4b-ft-arm1.jsonl",
        "longmemeval-ku-oracle-e4b-ft-arm1-r2.jsonl",
        "longmemeval-ku-oracle-e4b-ft-arm1-r10.jsonl",
        "longmemeval-ku-oracle-e4b-ft-arm1-baseline.jsonl",   # must NOT match
        "longmemeval-ku-oracle-e4b-ft-arm1-gate.jsonl",       # must NOT match
        "longmemeval-ku-oracle-e4b-ft-arm1-rx.jsonl",         # must NOT match
    ]:
        _write_jsonl(tmp_path / name, rows)
    found = replicate.discover("oracle", "e4b-ft", "arm1", tmp_path)
    assert sorted(found) == ["arm1", "arm1-r10", "arm1-r2"]


def test_discover_untagged_base(tmp_path):
    _write_jsonl(tmp_path / "longmemeval-ku-oracle-qwen-27b.jsonl", [_row("q1")])
    _write_jsonl(tmp_path / "longmemeval-ku-oracle-qwen-27b-r2.jsonl", [_row("q1")])
    found = replicate.discover("oracle", "qwen-27b", "", tmp_path)
    assert sorted(found) == ["", "r2"]


def test_load_rows_tolerates_blank_and_bad_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"question_id": "q1"}\n\nnot json\n', encoding="utf-8")
    rows = replicate.load_rows(p)
    assert [r["question_id"] for r in rows] == ["q1"]
    assert replicate.load_rows(tmp_path / "missing.jsonl") == []


def test_aggregate_math():
    # 4 questions; r1 = 4/4 correct, r2 = 2/4 correct -> mean 0.75
    r1 = [_row(f"q{i}", correct=True) for i in range(4)]
    r2 = ([_row("q0", correct=True), _row("q1", correct=True),
           _row("q2", correct=False), _row("q3", correct=False)])
    agg = replicate.aggregate({"arm1": r1, "arm1-r2": r2})
    assert agg["n_replicates"] == 2
    assert agg["n_questions"] == 4
    assert agg["replicates"] == ["arm1", "arm1-r2"]
    for arm in replicate.ARMS:
        assert agg["arms"][arm]["accuracies"] == [1.0, 0.5]
        assert agg["arms"][arm]["mean"] == pytest.approx(0.75)
        assert agg["arms"][arm]["std"] == pytest.approx(
            statistics.stdev([1.0, 0.5]))


def test_aggregate_skips_unjudged_replicate():
    r1 = [_row("q0"), _row("q1")]
    pending = [_row("q0", judged=False), _row("q1", judged=False)]
    agg = replicate.aggregate({"arm1": r1, "arm1-r2": pending})
    assert agg["n_replicates"] == 1
    assert agg["arms"]["cortex"]["std"] is None


def test_question_rates_and_mismatch():
    r1 = [_row("q0", correct=True), _row("q1", correct=False)]
    r2 = [_row("q0", correct=False), _row("q1", correct=False)]
    rates = replicate.question_rates({"a": r1, "a-r2": r2}, "cortex")
    assert rates == {"q0": 0.5, "q1": 0.0}
    with pytest.raises(ValueError, match="question sets"):
        replicate.question_rates({"a": r1, "a-r2": [_row("qX")]}, "cortex")


def test_paired_permutation_null_and_signal():
    rng = random.Random(42)
    qids = [f"q{i}" for i in range(78)]
    a = {q: rng.random() for q in qids}
    null = replicate.paired_permutation(a, dict(a))
    assert null["delta"] == 0.0
    assert null["p_value"] > 0.9            # identical sides: no effect
    b = {q: max(0.0, a[q] - 0.3) for q in qids}
    sig = replicate.paired_permutation(a, b)
    assert sig["delta"] > 0.2
    assert sig["p_value"] < 0.01
    assert sig["n_questions"] == 78
    # deterministic under the fixed default seed
    assert replicate.paired_permutation(a, b) == sig
    with pytest.raises(ValueError, match="question sets"):
        replicate.paired_permutation(a, {"other": 1.0})
