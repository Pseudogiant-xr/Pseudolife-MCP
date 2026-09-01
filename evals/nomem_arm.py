"""No-memory control arm — the memory-off rung.

MemTrapBench (arXiv 2608.20202) ran five memory frameworks against a
no-memory arm on trap tasks and every one of them scored BELOW it. That
result only exists because the arm exists: a harness that never asks
"what does this question score with no memory at all?" cannot tell a
retrieval win from a question the answerer could always answer.

So this arm is a standard rung, not a diagnostic. It answers from the
question alone. The task framing is held constant against the memory arms
— same completeness instruction, same exact abstention string — because a
memory-on vs memory-off delta has to be about the memory rather than
about the instructions. The context clauses are the only thing removed,
and they are removed rather than emptied: an "(empty)" context block is
itself a framing the other arms do not share.
"""
from __future__ import annotations

NOMEM_ANSWER_SYSTEM = (
    "You answer questions about a long-running conversation. You have NO "
    "access to that conversation — no transcript, no notes, no memory of "
    "it — so answer from the question alone. Answer completely — include "
    "every part the question asks for; lists and multi-step answers are "
    "fine. If the question cannot be answered without the conversation, "
    "say exactly: I don't know."
)


def nomem_prompt(question: str, question_date: str | None = None) -> str:
    """The arm's whole input. ``question_date`` mirrors the LongMemEval
    answer prompt's prefix so the arm can sit in that harness without a
    second framing; BEAM rows have no question date and omit it."""
    if question_date:
        return f"Question date: {question_date}\nQuestion: {question}"
    return f"Question: {question}"
