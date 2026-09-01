"""No-memory control arm — the memory-off rung.

MemTrapBench (arXiv 2608.20202) ran five memory frameworks against a
no-memory arm on trap tasks and every one of them scored BELOW it. That
result only exists because the arm exists: a harness that never asks
"what does this question score with no memory at all?" cannot tell a
retrieval win from a question the answerer could always answer.

So this arm is a standard rung, not a diagnostic. It answers from the
question alone, and everything about the task framing except the memory
is held constant against the arms it is bounding — otherwise a
memory-on/memory-off delta is partly about the instructions.

Holding it constant means the arm's prompt is BUILT PER HARNESS rather
than shared, because the two harnesses instruct answer length
differently: BEAM's rubric judge scores multi-part answers, so its
answerer is told to answer completely, while LongMemEval's judge grades
on containment and its answerer is capped at one sentence. Serving one
no-memory prompt to both would leave the arm verbose in the harness whose
judge rewards verbosity — more shots at containing the gold than the
one-sentence arms it exists to bound, inflating the floor in the one
direction that matters (2026-09-01 review, caught before either arm ran).

The context clauses are removed rather than emptied: an "(empty)" context
block is itself a framing the other arms do not see.
"""
from __future__ import annotations

# The two harness length policies, quoted verbatim from their answerers
# (longmemeval_bench._ANSWER_SYSTEM and beam_adapter._BEAM_ANSWER_SYSTEM).
# tests/test_nomem_arm.py pins each against its harness, so changing a
# harness's policy without re-matching this arm goes red.
LENGTH_ONE_SENTENCE = "Answer in one short sentence."
LENGTH_COMPLETE = (
    "Answer completely — include every part the question asks for; lists "
    "and multi-step answers are fine."
)


def nomem_system(length_clause: str) -> str:
    """The arm's system prompt, carrying the calling harness's own answer
    length policy."""
    return (
        "You answer questions about a long-running conversation. You have "
        "NO access to that conversation — no transcript, no notes, no "
        f"memory of it — so answer from the question alone. {length_clause} "
        "If the question cannot be answered without the conversation, say "
        "exactly: I don't know."
    )


# The BEAM-shaped default, kept as a module constant because that is the
# harness the arm shipped in first.
NOMEM_ANSWER_SYSTEM = nomem_system(LENGTH_COMPLETE)


def nomem_prompt(question: str, question_date: str | None = None) -> str:
    """The arm's whole input. ``question_date`` mirrors the LongMemEval
    answer prompt's prefix so the arm can sit in that harness without a
    second framing; BEAM rows have no question date and omit it."""
    if question_date:
        return f"Question date: {question_date}\nQuestion: {question}"
    return f"Question: {question}"
