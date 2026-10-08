"""Current identity bytes may change; positive outcomes and rejecting controls stay."""
import base64
from contextlib import contextmanager
import copy
import json
from pathlib import Path

import pytest

from . import phase1, stdio_eof, stdio_scenarios
from .stdio_scenarios import expected_stderr, startup_difference


@pytest.fixture
def selected_eof(monkeypatch, tmp_path):
    from evals.rust_baseline import daemon
    evidence = json.loads(Path(phase1.__file__).with_name("stdio_eof_orders.json").read_text())
    historical = {(cell["era"], cell["case"]): cell for cell in evidence["cells"]}
    state = {"fault": None}

    @contextmanager
    def private_directory():
        yield str(tmp_path)

    def observe(root, home, era, case, *, command=None, validate_oracle=True):
        cell = copy.deepcopy(historical[era, case])
        cell.update(pid=111 if command is None else 222, capture_kind="process",
                    captured_at_utc="2026-10-09T00:00:00.123456Z")
        raw = base64.b64decode(cell["stdout_frames_b64"][0])
        # Different current serialization is an informational historical drift.
        cell["stdout_frames_b64"][0] = base64.b64encode(b" " + raw).decode()
        if state["fault"] == "failed-exit":
            cell["exit_code"] = 3
        elif state["fault"] == "stderr":
            cell["stderr_b64"] = base64.b64encode(b"both failed\n").decode()
        elif state["fault"] == "missing-frame":
            cell["stdout_frames_b64"].pop()
        elif state["fault"] == "candidate-bytes" and command is not None:
            cell["stdout_frames_b64"][0] = base64.b64encode(b"  " + raw).decode()
        return cell

    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(stdio_eof, "observe", observe)
    monkeypatch.setattr(phase1, "capture_platform", lambda: evidence["capture_platform"])
    return state, lambda: phase1.eof(tmp_path, ["candidate"], selected_source={"oracle_head": "a" * 40})


def test_current_eof_pair_uses_complete_live_bytes_and_retains_historical_drift(selected_eof):
    state, capture = selected_eof
    result = capture()
    assert result["differences"] == []
    assert result["oracle_head"] == "a" * 40
    assert all(row["informational_only"] and row["differences"] for row in result["historical_comparisons"])


@pytest.mark.parametrize("fault,reason", [
    ("failed-exit", "eof_required_exit"), ("stderr", "eof_required_silence"),
    ("missing-frame", "eof_required_frame_count"),
])
def test_matching_failed_eof_arms_cannot_pass(selected_eof, fault, reason):
    state, capture = selected_eof
    state["fault"] = fault
    assert any(row["reason"] == reason for row in capture()["differences"])


def test_current_candidate_byte_mutation_still_fails(selected_eof):
    state, capture = selected_eof
    state["fault"] = "candidate-bytes"
    assert any(row["path"].startswith("/frames") for row in capture()["differences"])


def startup_case():
    contract = json.loads(Path(phase1.__file__).with_name("stdio_startup_contract.json").read_text())
    stderr = expected_stderr(contract, "version-mismatch", "fixture-origin")
    result = {"stderr_b64": base64.b64encode(stderr.replace(b"0.17.0", b"0.18.0")).decode(),
              "stdout_frames_b64": ["initialize-frame"], "exit_code": 0}
    return contract, result, [{"method": "GET", "path": "/health"}]


def test_current_startup_keeps_positive_contract_but_historical_version_bytes_are_information():
    contract, result, records = startup_case()
    assert startup_difference(result, contract, "version-mismatch", "fixture-origin", None, records) == [
        {"path": "/stderr", "reason": "startup_stderr_bytes"}]
    assert startup_difference(result, contract, "version-mismatch", "fixture-origin", None, records,
                              compare_stderr=False) == []


@pytest.mark.parametrize("fault,reason", [
    ("failed-exit", "startup_exit"), ("missing-frame", "startup_frame_count"),
    ("no-health", "startup_health_not_observed"),
    ("no-diagnostic", "startup_required_diagnostic_presence"),
])
def test_selected_startup_cannot_pass_matching_failed_outcomes(fault, reason):
    contract, result, records = startup_case()
    if fault == "failed-exit":
        result["exit_code"] = 3
    elif fault == "missing-frame":
        result["stdout_frames_b64"] = []
    elif fault == "no-health":
        records = []
    else:
        result["stderr_b64"] = ""
    differences = startup_difference(result, contract, "version-mismatch", "fixture-origin", None, records,
                                     compare_stderr=False)
    assert any(row["reason"] == reason for row in differences)


def test_selected_startup_refusal_cannot_send_traffic():
    contract, _, _ = startup_case()
    case = "preframe-refusal"
    result = {"stderr_b64": base64.b64encode(expected_stderr(contract, case, "fixture-origin")).decode(),
              "stdout_frames_b64": [], "exit_code": 1}
    records = [{"method": "GET", "path": "/health"}, {"method": "POST", "path": "/mcp"}]
    assert any(row["reason"] == "startup_refusal_sent_traffic" for row in startup_difference(
        result, contract, case, "fixture-origin", None, records, compare_stderr=False))


@pytest.mark.parametrize("candidate_stderr", [b"current diagnostic\n", b"current diagnostic!\n"])
def test_selected_startup_direct_pair_rejects_one_byte_stderr_difference(
        monkeypatch, tmp_path, candidate_stderr):
    from evals.rust_baseline import daemon

    @contextmanager
    def private_directory():
        yield str(tmp_path)

    class Fixture:
        url = "http://127.0.0.1:1"

        def __init__(self, health):
            self.records = []

        def close(self):
            return {"server_stopped": True}

    calls = []
    oracle_stderr = b"current diagnostic\n"

    def observe(command, **kwargs):
        calls.append(command)
        candidate = kwargs["boundary_errors"]
        return {"stdout_frames_b64": [base64.b64encode(b'{"id":1}\n').decode()],
                "stderr_b64": base64.b64encode(candidate_stderr if candidate else oracle_stderr).decode(),
                "exit_code": 0, "capture_kind": "process", "pid": 222 if candidate else 111,
                "captured_at_utc": "2026-10-09T00:00:00.123456Z"}

    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(stdio_scenarios, "ScenarioFixture", Fixture)
    monkeypatch.setattr(stdio_scenarios, "capture", observe)
    contract = {"cases": [{"case": "synthetic", "stderr_lf_template": "historical diagnostic\n",
                           "exit_code": 0, "stdout_frame_count": 1}]}
    result = stdio_scenarios.startup(tmp_path, ["native-candidate"], contract,
                                     selected_source={"oracle_head": "a" * 40})
    assert len(calls) == 2 and calls[1] == ["native-candidate"]
    assert calls[0][1:] == ["-m", "pseudolife_memory.cli"]
    assert result["oracle_head"] == "a" * 40
    assert all(row["informational_only"] and row["differences"] for row in result["historical_comparisons"])
    if candidate_stderr == oracle_stderr:
        assert result["differences"] == []
    else:
        assert len(candidate_stderr) == len(oracle_stderr) + 1
        assert len(result["differences"]) == 1
        difference = result["differences"][0]
        assert difference["path"] == "/stderr"
        retained = difference["stderr_evidence"]
        assert base64.b64decode(retained["oracle"]["stderr_b64"]) == oracle_stderr
        assert base64.b64decode(retained["candidate"]["stderr_b64"]) == candidate_stderr
