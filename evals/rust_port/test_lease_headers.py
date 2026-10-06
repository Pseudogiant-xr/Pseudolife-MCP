"""Controls for the additive executable header corpus and its exact boundary."""
import base64
import copy

import pytest

from evals.rust_port.lease_headers import cases, comparison, controls, expected
from evals.rust_port.lease_policy_preparation import refusal_response


def test_retained_instances_and_all_c0_fields_remain_executable_inputs():
    inputs = cases()
    identifiers = [case["id"] for case in inputs]
    assert len(identifiers) == len(set(identifiers))
    assert {f"D3-{tag}-{field}" for tag in ("DEL", "CR", "LF", "NUL")
            for field in ("agent_id", "credential")} <= set(identifiers)
    for field in ("agent_id", "credential", "bearer"):
        observed = {ord(character) for case in inputs if case["field"] == field
                    for character in case["value"] if ord(character) < 32 or ord(character) == 127}
        assert observed == {*range(32), 127}
        assert f"D3-FOLDED-{field}" in identifiers
    assert all(case["token_file"] for case in inputs
               if case["field"] == "bearer" and "\0" in case["value"])


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("field", ["agent_id", "credential", "bearer"])
def test_refusal_keeps_raw_oracle_and_rejects_each_output_and_effect_mutation(field, newline):
    case = {"field": field, "value": "bad\x7fvalue"}
    oracle = {"exit_code": 75, "stdout_b64": "", "stderr_b64": "cmF3",
              "post_files_b64": {"instance.id": "ZXhhY3Q="}}
    original = copy.deepcopy(oracle)
    pre = {"instance.id": "ZXhhY3Q="}
    candidate = refusal_response(field, pre, newline)
    requests = [] if field == "bearer" else ["/api/coordination/register"]
    assert comparison(case, oracle, candidate, pre, newline, requests)
    assert controls(case, oracle, candidate, pre, newline, requests) == [True] * 6
    assert oracle == original
    changed = copy.deepcopy(candidate)
    changed["stderr_b64"] = base64.b64encode(base64.b64decode(candidate["stderr_b64"])[:-1]).decode()
    assert not comparison(case, oracle, changed, pre, newline, requests)
    assert not comparison(case, oracle, candidate, pre, newline,
                          ["/api/coordination/lease", *requests])


@pytest.mark.parametrize("value", ["ordinary", "é", "\ud800"])
def test_ordinary_nonascii_and_surrogate_are_exact_raw_comparisons(value):
    case = {"field": "agent_id", "value": value}
    raw = {"exit_code": 127, "stdout_b64": "", "stderr_b64": "cmF3", "post_files_b64": {}}
    assert expected(case, raw, {}, b"\n") is raw
    assert comparison(case, raw, copy.deepcopy(raw), {}, b"\n", [])
    assert not comparison(case, raw, {**raw, "exit_code": 1}, {}, b"\n", [])


def test_private_file_admission_and_decoder_newline_are_outside_substitution():
    raw = {"exit_code": 127, "stdout_b64": "", "stderr_b64": "cmF3", "post_files_b64": {}}
    for case in cases():
        if case.get("missing_file") or case.get("file_terminator"):
            assert expected(case, raw, {}, b"\n") is raw


def test_combined_url_and_partial_reply_cases_preserve_fatal_and_ordinary_boundaries():
    inputs = {case["id"]: case for case in cases()}
    raw = {"exit_code": 3, "stdout_b64": "", "stderr_b64": "cmF3", "post_files_b64": {}}
    for action in ("run", "check", "list"):
        for source in ("env", "file"):
            fatal = inputs[f"combined-url-{action}-{source}-forbidden"]
            assert expected(fatal, raw, {}, b"\n") == refusal_response("bearer", {}, b"\n")
            assert expected(inputs[f"combined-url-{action}-{source}-valid"], raw, {}, b"\n") is raw
        for tag in ("credential-error", "private-check"):
            assert expected(inputs[f"combined-url-{action}-{tag}"], raw, {}, b"\n") is raw
    for field in ("agent_id", "credential"):
        fatal = inputs[f"combined-partial-{field}-forbidden"]
        assert expected(fatal, raw, {}, b"\n") == refusal_response(field, {}, b"\n")
        for tag in ("empty", "nonascii", "surrogate"):
            assert expected(inputs[f"combined-partial-{field}-{tag}"], raw, {}, b"\n") is raw
        assert expected(inputs[f"combined-missing-{field}-forbidden"], raw, {}, b"\n") == refusal_response(field, {}, b"\n")
        assert expected(inputs[f"combined-missing-{field}-valid"], raw, {}, b"\n") is raw
