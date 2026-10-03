"""Phase 0b comparator controls and capture-platform admission."""
import json

import pytest

from evals.rust_port import harness, full_bank, provenance


def test_duplicate_json_keys_are_rejected_before_parsing():
    with pytest.raises(harness.DuplicateJSONKey):
        harness.strict_json_loads('{"count":3,"count":3}')


def test_epoch_milliseconds_and_seconds_have_different_normalized_symbols():
    normal = full_bank.Normalizer()
    left = normal.apply({"timestamp": 1790000000.0}, {"times": {"/timestamp": "epoch"}})
    right = normal.apply({"timestamp": 1790000000000.0}, {"times": {"/timestamp": "epoch"}})
    assert harness.compare(left, right, harness.Policy())[0]["reason"] == "epoch_unit"


def test_source_descriptions_normalize_lf_by_named_policy():
    left = {"body": {"result": {"tools": [{"description": "first\r\nsecond"}]}}}
    right = {"body": {"result": {"tools": [{"description": "first\nsecond"}]}}}
    assert harness.compare(left, right, harness.Policy()) == []


def test_capture_platform_is_required_and_replay_platform_must_match():
    with pytest.raises(ValueError, match="platform"):
        harness.require_capture_platform({})
    with pytest.raises(ValueError, match="platform"):
        harness.require_capture_platform({"capture_platform": {"os": "Other", "architecture": "synthetic"}})


@pytest.mark.parametrize("control,reason", [
    ("missing-key", "field_presence"), ("ranking-order", "ranking_order"),
    ("epoch-milliseconds", "epoch_unit"), ("integer-float", "type"),
    ("error-as-success", "tool_error_envelope"), ("tool-description", "value"),
    ("duplicate-key", "duplicate_json_key"),
])
def test_each_proxy_control_is_load_bearing(control, reason):
    from evals.rust_port.controls import CONTROL_CASES, mutate, control_difference
    cases = {
        "search-http": {"status": 200, "headers": {}, "body": {
            "count": 3, "entries": [{"id": i, "score": .9 - i / 10, "timestamp": 1790000000.0}
                                     for i in (1, 2, 3)]}},
        "unknown-tool": {"status": 200, "headers": {}, "body": {
            "jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": []}}},
        "list-tools": {"status": 200, "headers": {}, "body": {
            "jsonrpc": "2.0", "id": 1, "result": {"tools": [{"description": "original"}]}}},
    }
    original = cases[CONTROL_CASES[control]]
    raw = json.dumps(original["body"]).encode()
    mutated, applied = mutate(control, raw, "application/json", CONTROL_CASES[control])
    assert applied
    differences = control_difference(original, mutated, CONTROL_CASES[control])
    assert reason in {d["reason"] for d in differences}


def test_phase_oracle_pin_preserves_historical_identity():
    assert provenance.ORACLE_HEAD == "3691f5cb75487d3fda54a6bde6fab35dcf32c681"
    assert provenance.ORACLE_SCHEMA == 53
    assert provenance.HISTORICAL_SCHEMA == 52


def test_deliberate_headers_and_framing_exclusion_are_recorded_and_checked():
    assert "x-pl-board" in harness.HTTP_HEADER_ALLOWLIST
    expected = {"headers": {"content-length": "3", "x-pl-board": "available"}, "framing": "fixed"}
    assert harness.compare(expected, {"headers": {"x-pl-board": "available"}, "framing": "chunked"}, harness.Policy()) == []
    assert harness.compare(expected, {"headers": {"x-pl-board": "available"}, "framing": "fixed"}, harness.Policy())
    assert harness.compare(expected, {"headers": {"content-length": "3"}, "framing": "fixed"}, harness.Policy())


def test_acceptance_summary_refuses_receipt_without_capture_platform():
    from evals.rust_port.acceptance import summarize
    with pytest.raises(ValueError, match="platform"):
        summarize({}, {}, {})
