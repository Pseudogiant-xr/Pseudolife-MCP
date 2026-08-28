"""compare_arms: paired between-run comparison over judged LongMemEval rows.

Pure tests — synthetic rows, no GPU, no files beyond tmp_path. Pins the
deterministic permutation p (seed 0), the win/loss bookkeeping, and the
cascade derivation (reused from replicate.py, never reimplemented).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import compare_arms  # noqa: E402


def _row(qid, rag=True, cortex=True, hybrid=True, abstain=False):
    return {
        "question_id": qid,
        "rag_correct": rag, "cortex_correct": cortex, "hybrid_correct": hybrid,
        "rag_context_tokens": 100, "cortex_context_tokens": 10,
        "hybrid_context_tokens": 50,
        "cortex_response": "I don't know" if abstain else "the answer is 42",
    }


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


def test_identical_runs_have_zero_delta_p_one(tmp_path):
    rows = [_row(f"q{i}", cortex=(i % 2 == 0)) for i in range(8)]
    a = _write(tmp_path / "a.jsonl", rows)
    b = _write(tmp_path / "b.jsonl", rows)
    out = compare_arms.compare(a, b, draws=1000, seed=0)
    assert out["n"] == 8
    # Artifact hygiene: never an absolute path (home dirs are maintainer
    # identifiers the tracked tree must not carry) — outside-repo inputs
    # fall back to the bare filename.
    assert out["a"]["file"] == "a.jsonl" and out["b"]["file"] == "b.jsonl"
    for arm in ("rag", "cortex", "hybrid", "cascade"):
        pa = out["paired"]["a_vs_b"][arm]
        assert pa["delta"] == 0.0 and pa["p"] == 1.0
        assert pa["wins"] == 0 and pa["losses"] == 0
        assert pa["win_qids"] == [] and pa["loss_qids"] == []


def test_wins_and_losses_carry_qids(tmp_path):
    a_rows = [_row("q1", cortex=True), _row("q2", cortex=False),
              _row("q3", cortex=True)]
    b_rows = [_row("q1", cortex=False), _row("q2", cortex=True),
              _row("q3", cortex=True)]
    a = _write(tmp_path / "a.jsonl", a_rows)
    b = _write(tmp_path / "b.jsonl", b_rows)
    out = compare_arms.compare(a, b, draws=1000, seed=0)
    pa = out["paired"]["a_vs_b"]["cortex"]
    assert pa["wins"] == 1 and pa["win_qids"] == ["q1"]      # a right, b wrong
    assert pa["losses"] == 1 and pa["loss_qids"] == ["q2"]   # a wrong, b right
    assert pa["delta"] == 0.0                                # ties out


def test_permutation_p_is_deterministic_under_seed(tmp_path):
    a_rows = [_row(f"q{i}", cortex=True) for i in range(12)]
    b_rows = [_row(f"q{i}", cortex=(i >= 9)) for i in range(12)]
    a = _write(tmp_path / "a.jsonl", a_rows)
    b = _write(tmp_path / "b.jsonl", b_rows)
    p1 = compare_arms.compare(a, b, draws=2000, seed=0)
    p2 = compare_arms.compare(a, b, draws=2000, seed=0)
    assert p1["paired"]["a_vs_b"]["cortex"]["p"] == p2["paired"]["a_vs_b"]["cortex"]["p"]
    assert p1["paired"]["a_vs_b"]["cortex"]["delta"] == 0.75
    assert p1["paired"]["a_vs_b"]["cortex"]["p"] < 0.05      # 9/12 one-sided flips


def test_cascade_uses_rag_on_abstention(tmp_path):
    # Cortex abstains in run A with a correct rag fallback; run B commits a
    # wrong cortex answer. Cascade must score A correct / B wrong even
    # though the cortex arm is wrong-vs-wrong.
    a_rows = [_row("q1", rag=True, cortex=False, abstain=True)]
    b_rows = [_row("q1", rag=True, cortex=False, abstain=False)]
    a = _write(tmp_path / "a.jsonl", a_rows)
    b = _write(tmp_path / "b.jsonl", b_rows)
    out = compare_arms.compare(a, b, draws=100, seed=0)
    assert out["a"]["arms"]["cascade"] == 1.0
    assert out["b"]["arms"]["cascade"] == 0.0
    assert out["paired"]["a_vs_b"]["cascade"]["wins"] == 1


def test_mismatched_qids_use_intersection(tmp_path):
    a = _write(tmp_path / "a.jsonl", [_row("q1"), _row("q2")])
    b = _write(tmp_path / "b.jsonl", [_row("q2"), _row("q3")])
    out = compare_arms.compare(a, b, draws=100, seed=0)
    assert out["n"] == 1
    assert out["dropped_a"] == 1 and out["dropped_b"] == 1


# ── cross-arm pairs and the question-type filter ─────────────────────────

def _typed_row(qid, qtype, flags: dict) -> dict:
    """A judged row carrying an arbitrary arm set and a question type — the
    variant arms (``hybrid_ctg`` …) and the type field that ``_row`` above,
    which pins the three fixed arms, does not."""
    row = {"question_id": qid, "question_type": qtype,
           "consolidation": {"superseded": 0},
           "cortex_response": "I don't know.", "abstention": False}
    for arm, ok in flags.items():
        row[f"{arm}_correct"] = ok
        row[f"{arm}_context_tokens"] = 100
        row.setdefault(f"{arm}_response", "x")
    return row


def test_compare_arms_cross_arm_pairs_and_types_filter(tmp_path):
    rows = [
        _typed_row("q1", "multi-session", {"hybrid": False, "hybrid_ctg": True,
                                           "rag": True, "cortex": False}),
        _typed_row("q2", "multi-session", {"hybrid": False, "hybrid_ctg": True,
                                           "rag": True, "cortex": False}),
        _typed_row("q3", "temporal-reasoning",
                   {"hybrid": True, "hybrid_ctg": False,
                    "rag": True, "cortex": False}),
        _typed_row("q4", "knowledge-update",
                   {"hybrid": True, "hybrid_ctg": True,
                    "rag": True, "cortex": True}),
    ]
    f = _write(tmp_path / "run.jsonl", rows)
    got = compare_arms.compare(f, f, arm_pairs=[("hybrid_ctg", "hybrid")],
                               types=("multi-session", "temporal-reasoning"))
    assert got["n"] == 3  # KU row filtered out
    pair = got["paired"]["a_vs_b"]["hybrid_ctg_vs_hybrid"]
    assert pair["wins"] == 2 and pair["losses"] == 1
    # Same-file same-arm sanity: rag vs rag pairs to zero delta.
    got2 = compare_arms.compare(f, f, arm_pairs=[("rag", "rag")])
    assert got2["paired"]["a_vs_b"]["rag_vs_rag"]["delta"] == 0.0


# ── score metric (BEAM rows: float rubric means, no _correct booleans) ───

def _beam_row(chat_id, qtype, index, **scores):
    return {
        "chat_id": chat_id, "type": qtype, "index": index,
        **{f"{arm}_score": v for arm, v in scores.items()},
    }


def test_score_metric_pairs_beam_rows_by_chat_type_index(tmp_path):
    rows_a = [_beam_row("1", "event_ordering", i,
                        rag=0.5, cortex=0.0, hybrid=0.5, hybrid_ev=1.0)
              for i in range(4)]
    rows_b = [_beam_row("1", "event_ordering", i,
                        rag=0.5, cortex=0.0, hybrid=0.5, hybrid_ev=0.5)
              for i in range(4)]
    a = _write(tmp_path / "a.jsonl", rows_a)
    b = _write(tmp_path / "b.jsonl", rows_b)
    out = compare_arms.compare(a, b, draws=1000, seed=0, metric="score")
    assert out["n"] == 4                      # keyed without question_id
    assert out["paired"]["a_vs_b"]["rag"]["delta"] == 0.0
    assert out["paired"]["a_vs_b"]["rag"]["p"] == 1.0
    pa = out["paired"]["a_vs_b"]["hybrid_ev"]
    assert pa["delta"] == 0.5
    assert pa["wins"] == 4 and pa["losses"] == 0
    # cascade is an LME accuracy derivation; score mode must not emit it
    assert "cascade" not in out["paired"]["a_vs_b"]


def test_score_metric_half_point_deltas_and_signs(tmp_path):
    # 0.5-step float deltas survive rounding, and a loss counts by sign.
    rows_a = [_beam_row("2", "summarization", 0, hybrid=1.0, hybrid_ev=0.5),
              _beam_row("2", "summarization", 1, hybrid=0.0, hybrid_ev=1.0)]
    a = _write(tmp_path / "ab.jsonl", rows_a)
    out = compare_arms.compare(a, a, draws=1000, seed=0, metric="score",
                               arm_pairs=[("hybrid_ev", "hybrid")])
    pa = out["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid"]
    assert pa["delta"] == 0.25                # (-0.5 + 1.0) / 2
    assert pa["wins"] == 1 and pa["losses"] == 1
    assert pa["win_qids"] == ["2/summarization/1"]
    assert pa["loss_qids"] == ["2/summarization/0"]


def test_score_metric_types_filter_reads_beam_type_field(tmp_path):
    rows = [_beam_row("1", "event_ordering", 0, hybrid=0.0, hybrid_ev=1.0),
            _beam_row("1", "abstention", 0, hybrid=1.0, hybrid_ev=1.0)]
    a = _write(tmp_path / "t.jsonl", rows)
    out = compare_arms.compare(a, a, draws=100, seed=0, metric="score",
                               types=("event_ordering",),
                               arm_pairs=[("hybrid_ev", "hybrid")])
    assert out["n"] == 1
    assert out["paired"]["a_vs_b"]["hybrid_ev_vs_hybrid"]["delta"] == 1.0
