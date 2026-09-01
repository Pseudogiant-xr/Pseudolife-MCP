"""Unit tests for the no-memory control arm (MemTrapBench rung)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from nomem_arm import NOMEM_ANSWER_SYSTEM, nomem_prompt  # noqa: E402


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
