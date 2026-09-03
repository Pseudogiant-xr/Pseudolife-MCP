"""contiguity_cue_split: the cue-gated offline re-read of a variants run.

Pure tests — synthetic rows, no GPU, no bank, no files beyond tmp_path.
Pins the gating composite (variant where the cue fired, vanilla where it
did not), the context-block diff, the never-overwrite guard, and that
the cue predicates come from the engine rather than a local copy.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evals"))

import contiguity_cue_split as ccs  # noqa: E402

MEMS = "\n\nRelevant memories:\n"


def _turn(day, who, body):
    return f"[2023/04/{day:02d} (Mon) 14:47] {who}: {body}"


def _ctx(facts, turns):
    return "Known facts:\n" + "\n".join(facts) + MEMS + "\n\n".join(turns)


def _row(qid, question, qtype, *, hybrid, ctg, tl=None, enum=None, all_=None,
         rag=True, hybrid_turns=("a",), ctg_turns=("a", "b")):
    """One judged row carrying every field the analyzer reads."""
    tl = hybrid if tl is None else tl
    enum = hybrid if enum is None else enum
    all_ = ctg if all_ is None else all_
    h_ctx = _ctx(["f1"], [_turn(1, "user", t) for t in hybrid_turns])
    c_ctx = _ctx(["f1"], [_turn(1, "user", t) for t in ctg_turns])
    row = {
        "question_id": qid, "question": question, "question_type": qtype,
        "contexts": {"hybrid": h_ctx, "hybrid_ctg": c_ctx,
                     "hybrid_tl": h_ctx, "hybrid_enum": h_ctx,
                     "hybrid_all": c_ctx},
        "rag_correct": rag, "rag_context_tokens": 300,
        "hybrid_correct": hybrid, "hybrid_context_tokens": 100,
        "hybrid_ctg_correct": ctg, "hybrid_ctg_context_tokens": 200,
        "hybrid_tl_correct": tl, "hybrid_tl_context_tokens": 100,
        "hybrid_enum_correct": enum, "hybrid_enum_context_tokens": 110,
        "hybrid_all_correct": all_, "hybrid_all_context_tokens": 210,
    }
    return row


# A cue-firing question ("first"/"how many") and a quiet one, checked
# against the engine's own predicates in the first test below.
CUED = "What was the first issue I had with my car?"
QUIET = "Which brand of coffee do I prefer?"


def test_cue_flags_come_from_the_engine():
    from pseudolife_memory.memory import cms

    assert ccs.has_temporal_cue is cms.has_temporal_cue
    assert ccs.has_aggregation_cue is cms.has_aggregation_cue
    assert ccs.has_date_cue is cms.has_date_cue
    assert ccs.cue_flags(CUED)["temporal"] is True
    assert ccs.cue_flags(CUED)["any"] is True
    assert ccs.cue_flags(QUIET)["any"] is False
    assert ccs.cue_flags("how many times did I go?")["aggregation"] is True
    assert ccs.cue_flags("what happened on 2023-04-10?")["date"] is True


def test_mem_blocks_split_on_turn_headers_not_blank_lines():
    # An assistant turn with an internal blank line is ONE block: naive
    # "\n\n" splitting would count it as two and inflate the diff.
    body = "Here you go:\n\n1. first\n\n2. second"
    ctx = _ctx(["f1"], [_turn(1, "user", "hi"), _turn(2, "assistant", body)])
    blocks = ccs.mem_blocks(ctx)
    assert len(blocks) == 2
    assert blocks[1].endswith("2. second")
    assert ccs.mem_blocks("no memories header here") == []


def test_gated_composite_serves_variant_only_where_cue_fired():
    rows = [
        # cue fires, variant wrong, vanilla right -> gated takes the loss
        _row("q1", CUED, "temporal-reasoning", hybrid=True, ctg=False),
        # cue quiet, variant wrong, vanilla right -> gated keeps vanilla
        _row("q2", QUIET, "single-session-preference",
             hybrid=True, ctg=False),
    ]
    assert ccs.gated_correct(rows[0], "hybrid_ctg", "any") == 0
    assert ccs.gated_correct(rows[1], "hybrid_ctg", "any") == 1
    assert ccs.gated_tokens(rows[0], "hybrid_ctg", "any") == 200
    assert ccs.gated_tokens(rows[1], "hybrid_ctg", "any") == 100

    g = ccs.gated_arm(rows, "hybrid_ctg", "any", draws=200, seed=0)
    assert g["ungated_acc"] == 0.0          # variant wrong on both
    assert g["gated_acc"] == 0.5            # rescued only the quiet row
    assert g["hybrid_acc"] == 1.0
    assert g["gated_context_tokens"] == 150.0
    assert g["vs_hybrid"]["delta"] == -0.5


def test_gating_cannot_beat_vanilla_where_the_variant_only_loses():
    """The bar the verdict checks: a gated arm is bounded above by the
    vanilla baseline when the variant never wins on a cue-fired row."""
    rows = [_row(f"q{i}", CUED, "multi-session", hybrid=True, ctg=False)
            for i in range(6)]
    g = ccs.gated_arm(rows, "hybrid_ctg", "any", draws=200, seed=0)
    assert g["gated_acc"] <= g["hybrid_acc"]
    assert g["vs_hybrid"]["delta"] == -1.0


def test_paired_split_is_deterministic_under_a_fixed_seed():
    rows = [_row(f"q{i}", CUED, "temporal-reasoning",
                 hybrid=(i % 2 == 0), ctg=(i % 3 == 0)) for i in range(12)]
    a = ccs.paired(rows, "hybrid_ctg", draws=2000, seed=0)
    b = ccs.paired(rows, "hybrid_ctg", draws=2000, seed=0)
    assert a == b
    assert a["n"] == 12
    assert a["wins"] + a["losses"] <= 12
    assert a["ci_lo"] <= a["delta"] <= a["ci_hi"]


def test_context_effect_counts_added_and_displaced_turns():
    rows = [_row("q1", CUED, "multi-session", hybrid=True, ctg=True,
                 hybrid_turns=("a", "b"), ctg_turns=("n", "a"))]
    eff = ccs.context_effect(rows, "hybrid_ctg")
    assert eff["mean_turns_added"] == 1.0        # "n" is new
    assert eff["mean_turns_displaced"] == 1.0    # "b" fell out of top-k
    assert eff["mean_context_token_delta"] == 100.0
    assert eff["identical_context_rows"] == 0


def test_noise_floor_counts_only_identical_context_rows():
    rows = [
        # identical contexts, verdicts agree -> in the denominator, no flip
        _row("q1", QUIET, "single-session-user", hybrid=True, ctg=True,
             hybrid_turns=("a",), ctg_turns=("a",)),
        # identical contexts, verdicts disagree -> a measured flip
        _row("q2", QUIET, "single-session-user", hybrid=True, ctg=False,
             hybrid_turns=("a",), ctg_turns=("a",)),
        # different contexts -> excluded entirely
        _row("q3", CUED, "multi-session", hybrid=True, ctg=False),
    ]
    nf = ccs.noise_floor(rows, "hybrid_ctg")
    assert nf["identical_context_rows"] == 2
    assert nf["verdict_disagreements"] == 1
    assert nf["disagreement_rate"] == 0.5


def test_cue_report_confusion_against_the_weak_type_label():
    rows = [_row("q1", CUED, "multi-session", hybrid=True, ctg=True),
            _row("q2", QUIET, "temporal-reasoning", hybrid=True, ctg=True),
            _row("q3", CUED, "knowledge-update", hybrid=True, ctg=True),
            _row("q4", QUIET, "knowledge-update", hybrid=True, ctg=True)]
    rep = ccs.cue_report(rows)
    c = rep["confusion_vs_weak_types"]
    assert (c["weak_fired"], c["weak_missed"]) == (1, 1)
    assert (c["strong_fired"], c["strong_quiet"]) == (1, 1)
    assert c["recall_on_weak"] == 0.5
    assert c["precision_for_weak"] == 0.5
    assert rep["by_type"]["multi-session"]["any"] == 1.0


def test_analyze_end_to_end_and_never_overwrites(tmp_path):
    rows = [_row(f"q{i}", CUED if i % 2 else QUIET,
                 "multi-session" if i % 2 else "knowledge-update",
                 hybrid=True, ctg=(i % 4 == 0)) for i in range(8)]
    rep = ccs.analyze(rows, draws=200, seed=0)
    assert rep["n"] == 8
    assert set(rep["variants"]) == set(ccs.VARIANTS)
    assert rep["verdict"]["arm"] == "hybrid_ctg"
    assert rep["verdict"]["rescued"] is False
    assert rep["provenance"]["new_answer_calls"] == 0

    out = tmp_path / "rep.json"
    out.write_text(json.dumps(rep), encoding="utf-8")
    src = tmp_path / "rows.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    argv = ["contiguity_cue_split.py", "--rows", str(src),
            "--out", str(out), "--draws", "100"]
    old = sys.argv
    sys.argv = argv
    try:
        with pytest.raises(SystemExit, match="never overwrite"):
            ccs.main()
    finally:
        sys.argv = old


def test_committed_artifact_matches_a_fresh_recompute():
    """The published artifact is reproducible from the committed rows —
    the whole claim of an offline re-read."""
    src = (REPO / "evals" / "results"
           / "longmemeval-all-oracle-qwen-27b-aggp1-variants-0803.jsonl")
    art = (REPO / "evals" / "results"
           / "contiguity-cue-split-20260904.json")
    if not src.exists() or not art.exists():   # pragma: no cover
        pytest.skip("aggp1-variants rows or artifact not present")
    published = json.loads(art.read_text(encoding="utf-8"))
    fresh = ccs.analyze(ccs.load_rows(src),
                        draws=published["draws"], seed=published["seed"])
    assert fresh == published
