"""Boundary rejections retain both process arms before a receipt can complete."""
import base64
from contextlib import contextmanager
import copy
import json
import sys

import pytest

from evals.rust_port.phase1_receipts import receipt_status
from evals.rust_port.stdio_judge import StdioPolicy, judge_with_evidence


CASES = [
    (b'{"id":1,"result":{"isError":true}', "/", "boundary_error"),
    (b'{"id":1,"id":1}\n', "/", "duplicate_json_key"),
    (b'\xff\n', "/", "boundary_error"),
    (b'{"id":1,"result":{"isError":false}}\n',
     "/json/0/result/isError", "tool_error_envelope"),
]


def transcript(frame, stderr, arm):
    return {"stdout_frames_b64": [base64.b64encode(frame).decode("ascii")],
            "stderr_b64": base64.b64encode(stderr).decode("ascii"), "exit_code": 0,
            "era": "2025-11-25", "arm": arm, "capture_kind": "process", "pid": 123,
            "captured_at_utc": "2026-10-05T00:00:00.123456Z"}


def rejected_pair(frame):
    oracle = transcript(b'{"id":1,"result":{"isError":true}}\n',
                        b"oracle diagnostic\x00\xff\r\n", "oracle")
    candidate = transcript(frame, b"candidate diagnostic\x00\xfe\r\n", "candidate")
    return oracle, candidate


@pytest.mark.parametrize("frame,path,reason", CASES)
def test_boundary_rejection_retains_both_arms_byte_for_byte(frame, path, reason):
    oracle, candidate = rejected_pair(frame)
    before = copy.deepcopy((oracle, candidate))
    differences = judge_with_evidence(oracle, candidate, StdioPolicy())
    row = next(row for row in differences if row["path"] == path)
    assert row["reason"] == reason
    # Exercise the artifact encoding too: NULs, non-UTF-8 and CRLF must survive.
    retained = json.loads(json.dumps(row))["stderr_evidence"]
    for arm, captured in (("oracle", oracle), ("candidate", candidate)):
        record = retained[arm]
        assert record["stderr_b64"] == captured["stderr_b64"]
        assert base64.b64decode(record["stderr_b64"], validate=True) == base64.b64decode(captured["stderr_b64"])
        assert record["byte_count"] == len(base64.b64decode(captured["stderr_b64"]))
        assert record["truncated"] is False
        for field in ("arm", "era", "pid", "captured_at_utc", "capture_kind"):
            assert record[field] == captured[field]
    assert (oracle, candidate) == before
    assert receipt_status({"differences": differences, "coverage_complete": True}) == "failed"


@pytest.mark.parametrize("path", ["/", "/json", "/json/0/result/isError"])
@pytest.mark.parametrize("group", ["differences", "process_controls"])
def test_boundary_row_without_retained_evidence_makes_receipt_incomplete(path, group):
    rows = [{"path": path, "reason": "boundary_error"}]
    receipt = {"differences": rows if group == "differences" else [], "coverage_complete": True}
    if group == "process_controls":
        receipt[group] = {"controls": {"boundary": {"differences": rows}}}
    assert receipt_status(receipt) == "incomplete"


@pytest.mark.parametrize("frame,path,reason", CASES)
@pytest.mark.parametrize("arm", ["oracle", "candidate"])
def test_missing_either_boundary_arm_bytes_makes_receipt_incomplete(frame, path, reason, arm):
    differences = judge_with_evidence(*rejected_pair(frame), StdioPolicy())
    row = next(row for row in differences if row["path"] == path)
    assert row["reason"] == reason
    del row["stderr_evidence"][arm]["stderr_b64"]
    assert receipt_status({"differences": differences, "coverage_complete": True}) == "incomplete"


@pytest.mark.parametrize("missing_arm", [None, "oracle", "candidate"])
def test_public_process_control_receipt_retains_boundaries_and_refuses_missing_bytes(
        tmp_path, monkeypatch, missing_arm):
    from evals.rust_baseline import daemon
    from evals.rust_port import full_bank, phase1, stdio_faults, stdio_process_controls, stdio_scenarios

    @contextmanager
    def private_directory():
        yield str(tmp_path)

    class Fixture:
        url = "http://127.0.0.1:1"

        def __init__(self, fault):
            assert fault == "healthy"

        def close(self):
            return {"server_stopped": True}

    frame = b'{"id":1,"result":{"protocolVersion":"2025-11-25"}}\n'
    oracle_bytes, candidate_bytes = b"oracle\x00\xff\r\n", b"candidate\x00\xfe\r\n"

    def capture(command, **kwargs):
        control = command[-1]
        raw = frame
        stderr = oracle_bytes if control in ("pseudolife_memory.cli", "identity") else candidate_bytes
        if control == "wrong-protocol":
            raw = frame.replace(b"2025-11-25", b"invalid")
        elif control == "duplicate-key":
            raw = b'{"id":1,' + frame[1:]
        return transcript(raw, stderr, "oracle" if control == "pseudolife_memory.cli" else "candidate")

    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(stdio_process_controls, "HangingFixture", Fixture)
    monkeypatch.setattr(stdio_process_controls, "capture", capture)
    actual_controls = stdio_process_controls.run

    def controls(root):
        result = actual_controls(root)
        if missing_arm == "oracle":
            del result["oracle_observed"]["stderr_b64"]
        elif missing_arm == "candidate":
            del result["controls"]["duplicate-key"]["observed"]["stderr_b64"]
        return result

    monkeypatch.setattr(stdio_process_controls, "run", controls)
    monkeypatch.setattr(phase1, "require_phase1_source", lambda root: {})
    monkeypatch.setattr(phase1, "runtime_metadata", lambda root: {})
    monkeypatch.setattr(phase1, "candidate_identity", lambda *args: {})
    monkeypatch.setattr(phase1, "candidate_bindings", lambda *args: {})
    monkeypatch.setattr(phase1, "process_tests", lambda *args: {"passed": True})
    monkeypatch.setattr(phase1, "corpus", lambda *args: {"arms": [], "differences": []})
    monkeypatch.setattr(phase1, "eof", lambda *args: {"cells": [], "differences": []})
    monkeypatch.setattr(stdio_faults, "run", lambda *args: {"cells": [], "differences": []})
    monkeypatch.setattr(stdio_scenarios, "run", lambda *args: {
        "startup": {"cells": []}, "concurrent": {"cells": []}, "differences": []})
    monkeypatch.setattr(full_bank, "full_corpus", lambda: {})
    monkeypatch.setattr(full_bank, "run", lambda *args, **kwargs: ({}, {
        "status": "passed", "cases": [], "differences": [], "identity_proxy_validation": {},
        "oracle_source_check": {}, "graded_controls": {}}))
    raw_path, public_path = tmp_path / "raw.json", tmp_path / "public.json"
    monkeypatch.setattr(sys, "argv", ["phase1", "--oracle-root", str(tmp_path),
                                    "--out", str(raw_path), "--public-out", str(public_path)])
    assert phase1.main() == (1 if missing_arm else 0)
    public, private = json.loads(public_path.read_text()), json.loads(raw_path.read_text())
    assert public["status"] == ("incomplete" if missing_arm else "passed")
    assert "oracle_observed" not in public["process_controls"]
    for control, path in (("duplicate-key", "/"), ("wrong-protocol", "/json/0/result/protocolVersion")):
        row = next(row for row in public["process_controls"][control]["differences"] if row["path"] == path)
        assert row in private["process_controls"]["controls"][control]["differences"]
        for arm, stderr in (("oracle", oracle_bytes), ("candidate", candidate_bytes)):
            retained = row["stderr_evidence"][arm]
            if missing_arm == arm and (arm == "oracle" or control == "duplicate-key"):
                assert retained["missing_stderr_bytes"] is True
            else:
                assert base64.b64decode(retained["stderr_b64"], validate=True) == stderr
