"""Unit tests for the no-memory control arm (MemTrapBench rung)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from nomem_arm import (  # noqa: E402
    LENGTH_COMPLETE, LENGTH_ONE_SENTENCE, NOMEM_ANSWER_SYSTEM, nomem_prompt,
    nomem_system,
)


def test_the_length_policy_matches_the_harness_the_arm_runs_in():
    """The whole point of the control is that only the MEMORY differs.

    The two harnesses instruct answer length differently — BEAM's rubric
    judge wants every part, LongMemEval's containment judge wants one
    sentence — so a single no-memory prompt cannot be matched to both. It
    is built per harness from that harness's own clause, and pinned here
    against both: a verbose no-memory arm judged on containment would get
    more shots at the gold than the one-sentence arms it exists to bound,
    inflating the floor in the one direction that matters (2026-09-01
    review).
    """
    import beam_adapter
    import longmemeval_bench as lmb
    assert LENGTH_ONE_SENTENCE in lmb._ANSWER_SYSTEM
    assert LENGTH_COMPLETE in beam_adapter._BEAM_ANSWER_SYSTEM
    assert LENGTH_ONE_SENTENCE in nomem_system(LENGTH_ONE_SENTENCE)
    assert LENGTH_COMPLETE not in nomem_system(LENGTH_ONE_SENTENCE)
    # each harness serves the arm the clause it uses for its memory arms
    assert LENGTH_ONE_SENTENCE in lmb.answer_call("nomem", "q", "d", "")[0]
    assert LENGTH_COMPLETE in beam_adapter.answer_call("nomem", "q", "")[0]


def test_nomem_prompt_carries_the_question_and_no_context_block():
    """The arm answers from the question alone: no memory context, and no
    "(empty)" placeholder either — an empty context block is itself a
    framing the other arms do not share."""
    p = nomem_prompt("Where did I move?")
    assert p.strip() == "Question: Where did I move?"
    assert "context" not in p.lower()


def test_nomem_system_keeps_the_shared_answer_contract():
    """Task framing is held constant against the memory arms — answer
    completeness and the exact abstention string — so a memory-on vs
    memory-off delta is about the memory, not about the instructions."""
    import beam_adapter
    s = NOMEM_ANSWER_SYSTEM
    assert "Answer completely" in s
    assert "say exactly: I don't know" in s
    assert "Answer completely" in beam_adapter._BEAM_ANSWER_SYSTEM
    assert "say exactly: I don't know" in beam_adapter._BEAM_ANSWER_SYSTEM


def test_nomem_system_promises_no_context():
    """It must not tell the model to use "the provided context" — there
    is none, and the memory arms' context clauses are exactly what this
    arm removes."""
    s = NOMEM_ANSWER_SYSTEM
    assert "provided context" not in s.lower()
    assert "no access" in s.lower()


def test_nomem_question_date_is_optional_and_prefixed_like_lme():
    """LongMemEval rows carry a question date in the prompt; keeping the
    same prefix lets the arm sit in that harness without a second
    framing."""
    p = nomem_prompt("Where did I move?", question_date="2023/05/20 (Sat) 02:03")
    assert p.startswith("Question date: 2023/05/20 (Sat) 02:03\n")
    assert p.rstrip().endswith("Question: Where did I move?")
