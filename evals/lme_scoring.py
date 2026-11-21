"""LongMemEval answer and judge protocol, shared by the Rust quality drivers."""
from __future__ import annotations

import re

from evals.retained_daemon import BenchError, excerpt

_ANSWER_SYSTEM = (
    "You answer questions about a user from their memory context. Use ONLY the "
    "provided context. When the context shows a fact was updated, answer with "
    "the most CURRENT value — unless the question explicitly asks about an "
    "earlier/initial/previous state, then use the earlier value. Answer in one "
    "short sentence. If the context does not contain the information, say "
    "exactly: I don't know."
)

# Faithful to the official LongMemEval GPT-4o judge for knowledge-update:
# equivalence counts, mentioning the old value is fine IF the updated value is
# what's answered, and abstention questions score on declining to answer.
_JUDGE_SYSTEM = (
    "You grade a model response against a correct answer. Reply with exactly "
    "one word: yes or no.\n"
    "- yes if the response contains or is equivalent to the correct answer.\n"
    "- The question asks about updated knowledge: if the response mentions "
    "outdated information but clearly gives the updated answer as current, "
    "grade yes.\n"
    "- no if the response gives only the outdated value, a different value, "
    "or omits the required information.\n"
    "- If the correct answer indicates the information was never mentioned, "
    "grade yes only if the response abstains (e.g. says it doesn't know)."
)

# Non-KU question types get the same judge minus the update-specific clause
# (its "the question asks about updated knowledge" framing is wrong for the
# other five LongMemEval types). KU rows — and rows from files predating the
# --types extension, which carry no question_type — keep _JUDGE_SYSTEM
# verbatim so canonical results re-judge byte-identically.
_JUDGE_SYSTEM_GENERIC = (
    "You grade a model response against a correct answer. Reply with exactly "
    "one word: yes or no.\n"
    "- yes if the response contains or is equivalent to the correct answer.\n"
    "- no if the response gives a different value or omits the required "
    "information.\n"
    "- If the correct answer indicates the information was never mentioned, "
    "grade yes only if the response abstains (e.g. says it doesn't know)."
)


def judge_answer(row, answer, chat):
    system = (_JUDGE_SYSTEM if row.get("question_type", "knowledge-update") == "knowledge-update"
              else _JUDGE_SYSTEM_GENERIC)
    verdict = chat(system, f"Question: {row['question']}\nCorrect answer: {row['answer']}\n"
                          f"Model response: {answer}", max_tokens=8)
    word = re.search(r"[^\W_]+", verdict)
    token = word.group().casefold() if word else ""
    if token not in {"yes", "no"}:
        raise BenchError(f"unparseable judge verdict: {excerpt(repr(verdict))}")
    return token == "yes"


def score_row(row, answerer, judge):
    out = dict(row)
    for arm, context in row["contexts"].items():
        prompt = (f"Question date: {row['question_date']}\nQuestion: {row['question']}\n\n"
                  f"Memory context:\n{context or '(empty)'}")
        response = answerer(_ANSWER_SYSTEM, prompt, max_tokens=256)
        out[f"{arm}_response"] = response
        out[f"{arm}_correct"] = judge_answer(row, response, judge)
        out[f"{arm}_context_tokens"] = (len(context) + 3) // 4
    return out


def rejudge_row(row, judge):
    out = dict(row)
    for arm in row["contexts"]:
        response = row.get(f"{arm}_response")
        if not isinstance(response, str) or not response.strip():
            raise BenchError("the primary run has a missing answer")
        out[f"{arm}_correct"] = judge_answer(row, response, judge)
    return out
