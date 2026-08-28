"""``evals/lme_v2_smoke.py``'s two response parsers: judge verdicts and MC.

**Judge verdict parse.** Qwen3.8 wraps judge verdicts in ``\\boxed{}`` when
the judged model response itself contains boxed answers (the compose prompt's
convention primes it). The 2026-08-17 smoke scored judge_acc 0.0 on every arm
because the bare ``startswith("yes")`` parse rejected ``\\boxed{yes}``. The V1
bench parse is deliberately untouched: an empirical re-judge audit of all 49
correct=False ceiling-v38 verdicts found zero boxed outputs there (KU answers
are terse and do not prime boxing).

**Multiple-choice scorer.** ``score_mc``'s no-box fallback used to accept any
standalone ``[A-Ha-h]`` token, upper-cased. In an answerer that ran out of
tokens mid-reasoning — the dominant unboxed shape in every committed lme-v2
artifact — that matches the English article "a" and scores the row as answer
**A**. Measured on ``evals/results/lme-v2-smoke-slice2.jsonl`` (2026-08-25
audit, issue #173): of 105 MC arm-answers 57 carry no box, and the old
fallback's matched token was the article "a" 40 times against 4 real uppercase
letters — all of the latter option *enumerations* ("*   A. Service Catalog"),
never a stated answer.

So the fallback is now uppercase-only AND anchored: the whole response is the
letter, or the letter follows an explicit answer marker. Anchoring on a bare
"Option X" was measured and rejected — option enumerations are the noise class
itself.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import lme_v2_smoke as S  # noqa: E402


# ══ judge verdict parse ══════════════════════════════════════════════════

def test_bare_yes_still_passes():
    assert S.judge_says_yes("yes")
    assert S.judge_says_yes("Yes.")


def test_bare_no_and_empty_fail():
    assert not S.judge_says_yes("no")
    assert not S.judge_says_yes("")


def test_boxed_yes_passes():
    assert S.judge_says_yes("\\boxed{yes}")
    assert S.judge_says_yes("\\boxed{Yes}")


def test_boxed_no_fails():
    assert not S.judge_says_yes("\\boxed{no}")


def test_degenerate_answer_echo_fails():
    # The judge echoing the model's answer instead of a verdict is a miss,
    # not a yes — observed live on 2026-08-17 (hybrid arm, question 025db8ef).
    assert not S.judge_says_yes("\\boxed{Reports; Problems}")


def test_nested_text_wrapper_passes():
    # Qwen commonly emits \boxed{\text{yes}}; the old [^{}]* regex parse
    # could not cross the inner brace (2026-08-19 review, empirically shown).
    assert S.judge_says_yes("\\boxed{\\text{yes}}")


def test_truncated_boxed_passes():
    # The judge call is capped at 8 tokens; a box truncated before its
    # closing brace is still a leading yes-verdict.
    assert S.judge_says_yes("\\boxed{yes")


def test_quoted_gold_answer_is_not_a_verdict():
    # False-positive direction (2026-08-19 review, empirically shown): a
    # boxed yes quoted mid-sentence is the gold answer, not the verdict.
    assert not S.judge_says_yes("No - the correct answer was \\boxed{yes}")


# ══ multiple-choice scoring ══════════════════════════════════════════════

# ── the false-positive direction (the bug) ───────────────────────────────
def test_prose_article_a_is_not_answer_a():
    # Verbatim shape of the truncated traces that scored 8 rows correct by
    # accident in lme-v2-smoke-qwen38-slice.jsonl.
    resp = ("Based on the memory context, the typical workflow for "
            "offboarding a user involves the modules Hardware Assets and")
    assert not S.score_mc(resp, "A", {})


def test_lowercase_eg_is_not_answer_e():
    resp = 'The context mentions filtering by Configuration item (e.g., "CI").'
    assert not S.score_mc(resp, "E", {})


def test_option_enumeration_is_not_an_answer():
    resp = ("Looking at the options:\n"
            "*   A. Service Catalog; Catalog Items\n"
            "*   B. Procurement; Purchase Orders\n")
    assert not S.score_mc(resp, "A", {})


def test_quoted_option_mention_is_not_an_answer():
    resp = 'The text says "one hour", though option E says "at most 1 hour"'
    assert not S.score_mc(resp, "E", {})


def test_dont_know_scores_wrong():
    assert not S.score_mc("I don't know.", "D", {})


# ── the true-positive direction (what must keep scoring) ─────────────────
def test_bare_letter_scores():
    assert S.score_mc("B", "B", {})
    assert S.score_mc("B.", "B", {})
    assert S.score_mc("(B)", "B", {})
    assert not S.score_mc("B", "C", {})


def test_answer_marker_scores():
    assert S.score_mc("Answer: C", "C", {})
    assert S.score_mc("The answer is C.", "C", {})
    assert S.score_mc("**Final answer:** C", "C", {})
    assert S.score_mc("The correct option is F.", "F", {})
    assert S.score_mc('Per the docs, the answer is "C".', "C", {})
    assert S.score_mc("The answer is: B", "B", {})   # both separators
    assert not S.score_mc("Answer: C", "D", {})


def test_answer_marker_needs_an_uppercase_letter():
    # "Answer: a" is prose continuing into a sentence far more often than
    # it is a stated answer; uppercase-only is the whole point of the fix.
    assert not S.score_mc("Answer: a report with the tag", "A", {})
    assert not S.score_mc("The answer is not A.", "A", {})


def test_last_answer_marker_wins():
    # A trace that revises itself states the answer twice; the final one is
    # the answer, mirroring _extract_boxed's last-box rule.
    assert S.score_mc("Answer: A. Wait — reconsidering, the answer is D.",
                      "D", {})


def test_a_later_prose_marker_does_not_erase_a_stated_answer():
    # Uppercase filtering happens before "last wins": a trailing prose
    # "the answer is not ..." must not swallow the real answer above it.
    assert S.score_mc("Answer: C. Note the answer is not obvious.", "C", {})


# ── the primary path is untouched ────────────────────────────────────────
def test_boxed_answer_unchanged():
    assert S.score_mc("\\boxed{D}", "D", {})
    assert S.score_mc("some reasoning\n\\boxed{d}", "D", {})
    assert not S.score_mc("\\boxed{D}", "C", {})


def test_boxed_wins_over_prose_article():
    resp = "creating a new record ...\n\\boxed{G}"
    assert S.score_mc(resp, "G", {})
    assert not S.score_mc(resp, "A", {})


def test_require_non_empty_flag_still_rejects_unparseable():
    flags = {"require_non_empty": "true"}
    assert not S.score_mc("no letter here at all", flags=flags, answer="A")
