"""The V2 smoke's LLM-judge verdict parse must accept boxed verdicts.

Qwen3.8 wraps judge verdicts in ``\\boxed{}`` when the judged model response
itself contains boxed answers (the compose prompt's convention primes it).
The 2026-08-17 smoke scored judge_acc 0.0 on every arm because the bare
``startswith("yes")`` parse rejected ``\\boxed{yes}``. The V1 bench parse is
deliberately untouched: an empirical re-judge audit of all 49 correct=False
ceiling-v38 verdicts found zero boxed outputs there (KU answers are terse and
do not prime boxing).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import lme_v2_smoke as S  # noqa: E402


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
