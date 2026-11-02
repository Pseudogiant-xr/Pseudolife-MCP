"""The reranker parity check's order verdict: only the declared tie passes."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import w2d_rerank_check as check  # noqa: E402

TIE = 20
CASE = {"query": "postgres", "candidates": ["short", "long a", "long b"]}
# "long a" and "long b" truncate to the same tokens; "short" does not.
TOKENS = [[101, 1, 102], [101, 2, 3, 102], [101, 2, 3, 102]]


def result(logits, rerank=None, tokens=None):
    out = {"logits": logits, "rerank": rerank if rerank is not None else [0.5] * len(logits)}
    if tokens is not None:
        out["tokens"] = tokens
    return out


def test_agreeing_orders_agree():
    p = result([0.1, 0.9, 0.5], tokens=TOKENS)
    r = result([0.1, 0.9, 0.5])
    assert check.order_verdict(1, CASE, p, r) == "agree"


def test_a_swap_between_identically_truncated_passages_is_the_declared_tie():
    # Python orders the identical inputs by noise; Rust ties them exactly.
    p = result([0.1, 0.9 - 9.5e-7, 0.9], tokens=TOKENS)
    r = result([0.1, 0.9, 0.9])
    assert check.ranked(CASE["candidates"], p["logits"]) != check.ranked(CASE["candidates"], r["logits"])
    assert check.order_verdict(TIE, CASE, p, r) == "declared"


def test_a_genuine_order_difference_still_fails_in_the_declared_case():
    # "short" and "long a" swap: their truncated tokens differ.
    p = result([0.9, 0.1, 0.5], tokens=TOKENS)
    r = result([0.1, 0.9, 0.5])
    assert check.order_verdict(TIE, CASE, p, r) == "differs"


def test_the_same_tie_fails_in_any_other_case():
    p = result([0.1, 0.9 - 9.5e-7, 0.9], tokens=TOKENS)
    r = result([0.1, 0.9, 0.9])
    for n in (1, 19, 21, 40):
        assert check.order_verdict(n, CASE, p, r) == "differs"


def test_the_declared_tie_fails_when_a_score_is_outside_tolerance():
    p = result([0.1, 0.9 - 9.5e-7, 0.9], rerank=[0.5, 0.5, 0.5], tokens=TOKENS)
    r = result([0.1, 0.9, 0.9], rerank=[0.5, 0.5, 0.5 + 2 * check.SIGMOID_TOL])
    assert check.order_verdict(TIE, CASE, p, r) == "differs"


def test_the_declared_tie_fails_without_token_evidence():
    p = result([0.1, 0.9 - 9.5e-7, 0.9])
    r = result([0.1, 0.9, 0.9])
    assert check.order_verdict(TIE, CASE, p, r) == "differs"
    p["tokens"] = TOKENS[:2]
    assert check.order_verdict(TIE, CASE, p, r) == "differs"


def test_the_declared_case_is_the_truncated_lorem_pool_and_its_entry_exists():
    case = check.build_cases()[TIE - 1]
    assert case["query"] == "postgres"
    assert case["candidates"] == [check.LOREM * k for k in (1, 3, 10, 25, 50)]
    for path in check.DECLARED_TIES.values():
        assert (check.REPO / path).is_file(), path
