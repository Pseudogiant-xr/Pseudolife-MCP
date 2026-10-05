import base64
import copy
import hashlib

import pytest

from .cli_doorbell_seen import NONCE, THREAD, assert_effect, cases, controls


def successful_observation():
    key = hashlib.sha256(THREAD.encode()).hexdigest()
    pending = "digests/" + key + ".bell-pending"
    before = {pending: base64.b64encode(b"fixed pending\n").decode()}
    post = dict(before)
    post["digests/" + key + ".bell-lock"] = base64.b64encode(b"0").decode()
    post["digests/" + key + ".bell-prompt-seen"] = base64.b64encode((NONCE + "\n").encode()).decode()
    return key, before, dict(exit_code=0, stdout_b64="", stderr_b64="", post_files_b64=post)


@pytest.mark.parametrize("mutation", ["exit", "stdout", "stderr", "pending", "nonce", "lock", "extra"])
def test_independent_expected_effect_rejects_each_corruption(mutation):
    key, before, observation = successful_observation()
    case = dict(writes=True, locks=True, id="positive")
    assert_effect(case, observation, key, before)
    changed = copy.deepcopy(observation)
    if mutation == "exit":
        changed["exit_code"] = 1
    elif mutation in ("stdout", "stderr"):
        changed[mutation + "_b64"] = "eA=="
    elif mutation == "extra":
        changed["post_files_b64"]["digests/unexpected"] = ""
    else:
        suffix = {"pending": "bell-pending", "nonce": "bell-prompt-seen", "lock": "bell-lock"}[mutation]
        changed["post_files_b64"]["digests/" + key + "." + suffix] = "eA=="
    with pytest.raises(AssertionError):
        assert_effect(case, changed, key, before)


def test_candidate_controls_use_exact_judge_for_all_fields():
    _, _, observation = successful_observation()
    checked = controls(observation)
    assert set(checked) == {"exit_code", "stdout_b64", "stderr_b64", "post_files_b64"}
    assert all(control["rejected"] and control["mutation_arm"] == "candidate" for control in checked.values())


def test_review_integer_counterexamples_have_independent_receipt_expectations():
    def notice_text(*args, **kwargs):
        return "fixed notice"
    selected = {case["id"]: case for case in cases(notice_text)}
    for limit, digits, writes in [("640", 640, True), ("640", 641, False),
                                  ("0", 4301, True), ("0", 65000, True),
                                  ("5000", 4301, True), ("5000", 5000, True),
                                  ("5000", 5001, False)]:
        case = selected[f"configured-integer-limit-{limit}-{digits}"]
        assert case["environment_deltas"] == {"PYTHONINTMAXSTRDIGITS": limit}
        assert case["writes"] is writes and case["locks"] is writes
        assert b'"extra":' + b'9'*digits in base64.b64decode(case["stdin_b64"])
