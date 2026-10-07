"""Rejecting controls for the named finite UTF-8 lease reply policy."""
import base64
import copy

import pytest

from evals.rust_port.lease_headers import comparison, controls, expected
from evals.rust_port.lease_policy_preparation import refusal_response


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("field,other", [("agent_id", "credential"), ("credential", "agent_id")])
def test_malformed_reply_preempts_field_admission_but_parsed_forbidden_keeps_r2(field, other, newline):
    pre = {"instance.id": "ZXhhY3Q="}
    oracle = {"exit_code": 3, "stdout_b64": "", "stderr_b64": "cmF3", "post_files_b64": {**pre, "child-ran": "cmFu"}}
    case = {"field": field, "value": "bad\x7fvalue", "registration_overrides": {other: "surrogate-\ud800"}}
    original = copy.deepcopy(oracle)
    reply_refusal = {"exit_code": 1, "stdout_b64": "", "stderr_b64": base64.b64encode(b"lease: HTTP_REPLY_NOT_UNDERSTOOD" + newline).decode(), "post_files_b64": pre}
    requests = ["/api/coordination/register"]
    assert expected(case, oracle, pre, newline) == reply_refusal
    assert comparison(case, oracle, reply_refusal, pre, newline, requests)
    assert controls(case, oracle, reply_refusal, pre, newline, requests) == [True] * 6
    assert not comparison(case, oracle, refusal_response(field, pre, newline), pre, newline, requests)
    parsed = {**case, "registration_overrides": {other: ""}}
    header_refusal = refusal_response(field, pre, newline)
    assert expected(parsed, oracle, pre, newline) == header_refusal
    assert comparison(parsed, oracle, header_refusal, pre, newline, requests)
    assert not comparison(parsed, oracle, reply_refusal, pre, newline, requests)
    assert oracle == original


def test_ordinary_nonascii_diagnostic_changes_only_the_named_bytes():
    pre = {"instance.id": "ZXhhY3Q="}
    raw = {"exit_code": 3, "stdout_b64": "", "stderr_b64": base64.b64encode(b"lease: board skipped: the board failed unexpectedly (UnicodeEncodeError); exact tail\n").decode(), "post_files_b64": {**pre, "child-ran": "cmFu"}}
    case = {"field": "agent_id", "value": "é"}
    target = expected(case, raw, pre, b"\n")
    assert base64.b64decode(target["stderr_b64"]) == b"lease: board skipped: registration headers are not understood; exact tail\n"
    assert target["exit_code"] == raw["exit_code"]
    assert target["post_files_b64"] == raw["post_files_b64"]
    assert target["stdout_b64"] == raw["stdout_b64"]
    assert not comparison(case, raw, {**target, "exit_code": 1}, pre, b"\n", ["/api/coordination/register"])


def test_cli_native_diagnostic_retains_every_unrelated_byte_and_control():
    from evals.rust_port.cli_process import candidate_controls
    from evals.rust_port.lease_corpus import expected_response
    raw = {"exit_code": 0, "stdout_b64": base64.b64encode(b'{"reason": "the board failed unexpectedly (UnicodeEncodeError)", "held": false}\n').decode(), "stderr_b64": "", "post_files_b64": {"instance.id": "ZXhhY3Q="}}
    original = copy.deepcopy(raw)
    target = expected_response("lease-check-nonascii-bearer", raw)
    assert target == {**raw, "stdout_b64": base64.b64encode(b'{"reason": "registration headers are not understood", "held": false}\n').decode()}
    assert expected_response("lease-check-other", raw) is raw
    record = {"id": "lease-check-nonascii-bearer", "mode": "lease-check", "oracle": {"response": raw}, "candidate": {"response": target}}
    assert all(control["rejected"] for control in candidate_controls([record]))
    assert raw == original
