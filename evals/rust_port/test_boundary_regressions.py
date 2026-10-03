"""Executable-boundary and normalized-clock counterexamples from review."""
import os
from pathlib import Path
import sys

import pytest

from evals.rust_port.full_bank import Normalizer, normalize_payload
from evals.rust_port.harness import capture_platform, Policy, compare, execute, main, replay


def cli_case(code):
    return {"id": "termination", "surface": "cli", "request": {"argv": ["-c", code]}}


def cli_options(tmp_path):
    return dict(cli_prefix=[sys.executable], base_url=None, cwd=Path.cwd(),
                home=tmp_path / "home", timeout=5)


def crash_code():
    return ("import os; os._exit(-1073741819)" if os.name == "nt" else
            "import os,signal; os.kill(os.getpid(),signal.SIGKILL)")


def test_real_crash_is_boundary_error_and_cannot_be_its_own_oracle(tmp_path):
    records = execute([cli_case(crash_code())], **cli_options(tmp_path))
    assert records[0]["response"] == {"boundary_error": "AbnormalTermination"}
    with pytest.raises(ValueError, match="oracle transcript"):
        replay({"capture_platform": capture_platform(), "records": records}, **cli_options(tmp_path))


def test_real_candidate_crash_fails_even_when_exit_value_is_ignored(tmp_path):
    case = cli_case(crash_code())
    case["policy"] = {"ignored_values": ["/exit_code"]}
    transcript = {"capture_platform": capture_platform(), "records": [{**case, "response": {
        "exit_code": 0, "stdout_b64": "", "stderr_b64": ""}}]}
    result = replay(transcript, **cli_options(tmp_path))
    assert not result["passed"]
    assert result["differences"] == [{"case": "termination", "path": "/",
                                       "reason": "boundary_error"}]


def test_record_command_returns_failure_and_retains_crash_evidence(tmp_path, monkeypatch):
    import json
    payload, output = tmp_path / "cases.json", tmp_path / "record.json"
    payload.write_text(json.dumps({"cases": [cli_case(crash_code())]}))
    monkeypatch.setattr(sys, "argv", ["harness", "record", "--input", str(payload),
        "--out", str(output), "--cli-json", json.dumps([sys.executable])])
    assert main() == 1
    result = json.loads(output.read_text())
    assert result["passed"] is False
    assert result["records"][0]["response"] == {"boundary_error": "AbnormalTermination"}


@pytest.mark.parametrize("status", [-9, -1073741819, 0xC0000005, 0x40000015,
                                    256, True, 1.0, None])
def test_raw_abnormal_or_malformed_oracle_exit_cannot_be_admitted(tmp_path, status):
    transcript = {"capture_platform": capture_platform(), "records": [{**cli_case("raise SystemExit(0)"), "response": {
        "exit_code": status, "stdout_b64": "", "stderr_b64": ""}}]}
    with pytest.raises(ValueError, match="oracle transcript"):
        replay(transcript, **cli_options(tmp_path))


@pytest.mark.parametrize("status", [0, 1, 2, 4, 255])
def test_supported_cli_error_codes_keep_stdout_stderr_and_replay(tmp_path, status):
    case = cli_case(f"import sys; print('out'); print('err',file=sys.stderr); sys.exit({status})")
    records = execute([case], **cli_options(tmp_path))
    assert records[0]["response"]["exit_code"] == status
    assert records[0]["response"]["stdout_b64"] and records[0]["response"]["stderr_b64"]
    assert replay({"capture_platform": capture_platform(), "records": records}, **cli_options(tmp_path))["passed"]


@pytest.mark.parametrize("expected,actual", [(10.0, 10), (10, 10.0)])
def test_registration_clock_numeric_type_change_survives_normalization(expected, actual):
    def safe(value):
        return normalize_payload(Normalizer(), {"created_at": value, "last_activity": value,
            "agent_id": "a" * 32, "credential": "s" * 43}, "register-sender")
    differences = compare(safe(expected), safe(actual), Policy())
    assert {difference["path"] for difference in differences} == {"/created_at", "/last_activity"}


@pytest.mark.parametrize("expected,actual", [(10.0, 10), (10, 10.0)])
def test_captured_clock_type_is_retained_between_independent_arms(expected, actual):
    rules = {"captures": {"/clock": {"name": "clock", "kind": "epoch"}}}
    a = Normalizer().apply({"clock": expected}, rules)
    b = Normalizer().apply({"clock": actual}, rules)
    assert compare(a, b, Policy())


@pytest.mark.parametrize("expected,actual", [(10.0, 10), (10, 10.0)])
def test_nullable_epoch_numeric_type_change_survives_normalization(expected, actual):
    rules = {"times": {"/clock": "nullable_epoch"}}
    a = Normalizer().apply({"clock": expected}, rules)
    b = Normalizer().apply({"clock": actual}, rules)
    assert compare(a, b, Policy())


@pytest.mark.parametrize("expected,actual", [(10.0, 10), (10, 10.0)])
@pytest.mark.parametrize("reference", [False, True])
def test_clock_recapture_and_reference_require_exact_type(expected, actual, reference):
    normal = Normalizer()
    normal.capture("clock", "epoch", expected)
    rules = ({"references": {"/clock": "clock"}} if reference else
             {"captures": {"/clock": {"name": "clock", "kind": "epoch"}}})
    with pytest.raises(ValueError):
        normal.apply({"clock": actual}, rules)


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("kind", ["epoch", "nullable_epoch"])
def test_boolean_is_never_an_epoch(value, kind):
    with pytest.raises(ValueError, match="invalid epoch"):
        Normalizer().apply({"clock": value}, {"times": {"/clock": kind}})


def test_boolean_cannot_alias_a_captured_integer_epoch():
    normal = Normalizer()
    normal.capture("clock", "epoch", 1)
    with pytest.raises(ValueError):
        normal.apply({"clock": True}, {"references": {"/clock": "clock"}})


def test_clock_value_changes_still_normalize_when_types_match():
    for kind, values in (("epoch", (10, 20)), ("epoch", (10.0, 20.0)),
                         ("nullable_epoch", (None, None))):
        rules = {"times": {"/clock": kind}}
        assert Normalizer().apply({"clock": values[0]}, rules) == Normalizer().apply({"clock": values[1]}, rules)
