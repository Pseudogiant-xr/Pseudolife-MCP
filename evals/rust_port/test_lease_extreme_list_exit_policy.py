"""The two unsupported-date list exits never authorize other exit differences."""
import base64
import copy

import pytest

from evals.rust_port.lease_policy_preparation import EXTREME_LIST_CASES, extreme_list_response


def response(exit_code=1, stderr=b""):
    return {"exit_code": exit_code, "stdout_b64": "", "stderr_b64": base64.b64encode(stderr).decode(),
            "post_files_b64": {"instance.id": "MDEyMzQ1Njc4OWFiCg=="}}


def overflow():
    return response(stderr=b'Traceback (most recent call last):\n  File "lease_cli.py", line 1, in _list\n'
                    b'    _clock(value)\nOverflowError: signed integer is greater than maximum\n')


@pytest.mark.parametrize("case_id", sorted(EXTREME_LIST_CASES))
def test_only_named_extreme_list_cells_admit_validated_overflow_to_success(case_id):
    oracle = overflow()
    stdout = b"board: http://127.0.0.1:1\nlease resource: held by holder, expected end ?\n"
    candidate = response(0)
    candidate["stdout_b64"] = base64.b64encode(stdout).decode()
    original = copy.deepcopy(oracle)
    assert extreme_list_response(case_id, oracle, candidate, stdout)
    assert oracle == original
    for field, value in (("exit_code", 1), ("stdout_b64", "eA=="), ("stderr_b64", "eA=="),
                         ("post_files_b64", {**candidate["post_files_b64"], "unrelated": "eA=="})):
        changed = {**candidate, field: value}
        assert not extreme_list_response(case_id, oracle, changed, stdout)


@pytest.mark.parametrize("case_id", ["lease-list-empty-json", "lease-list-local-sorted", "I3-0-text",
                                    "list-expected-json", "list-waiter-json", "another-list-cell"])
def test_any_other_list_exit_difference_is_rejected(case_id):
    oracle = response(0)
    candidate = {**oracle, "exit_code": 1}
    assert not extreme_list_response(case_id, oracle, candidate, b"?\n")
    oracle = overflow()
    candidate = response(0)
    candidate["stdout_b64"] = "Pwo="
    assert not extreme_list_response(case_id, oracle, candidate, b"?\n")


def test_named_cells_keep_normal_python_responses_exact_and_require_a_traceback():
    oracle = response(0)
    assert extreme_list_response("list-expected-extreme", oracle, dict(oracle), b"?\n")
    assert not extreme_list_response("list-expected-extreme", oracle, response(1), b"?\n")
    assert not extreme_list_response("list-expected-extreme", response(), response(0), b"?\n")
    malformed = response(stderr=b'Traceback (most recent call last):\nunknown frame\nOverflowError: too large\n')
    assert not extreme_list_response("list-expected-extreme", malformed, response(0), b"?\n")
