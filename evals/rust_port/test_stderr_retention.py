"""Differential receipts preserve the process evidence they reject."""
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

from evals.rust_port.stdio import capture
from evals.rust_port.stdio_judge import StdioPolicy, judge


def transcript(stderr=b"", *, arm="oracle"):
    return {"stdout_frames_b64": [base64.b64encode(b'{"id":1}\n').decode()],
            "stderr_b64": base64.b64encode(stderr).decode(), "exit_code": 0,
            "era": "2025-11-25", "arm": arm, "capture_kind": "process", "pid": 123,
            "captured_at_utc": "2026-10-05T00:00:00.123456Z"}


@pytest.mark.parametrize("field", ["stderr_b64", "stdout_frames_b64", "exit_code"])
def test_mutated_candidate_retains_both_captured_stderr_arms(field, tmp_path):
    oracle = transcript(b"oracle diagnostic\x00\xff\r\n")
    candidate = transcript(b"candidate diagnostic\x00\xfe\r\n", arm="candidate")
    if field != "stderr_b64":
        candidate["stderr_b64"] = oracle["stderr_b64"]
        candidate[field] = (1 if field == "exit_code" else
                            [base64.b64encode(b'{ "id":1}\n').decode()])
    differences = judge(oracle, candidate, StdioPolicy())
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps({"differences": differences}))
    retained = json.loads(receipt_path.read_text())["differences"][0]["stderr_evidence"]
    for arm, observed in (("oracle", oracle), ("candidate", candidate)):
        assert retained[arm]["stderr_b64"] == observed["stderr_b64"]
        assert retained[arm]["captured_at_utc"] == observed["captured_at_utc"]
        assert retained[arm]["pid"] == observed["pid"]
        assert retained[arm]["era"] == observed["era"]
        assert retained[arm]["arm"] == arm
        assert retained[arm]["truncated"] is False


@pytest.mark.parametrize("length", [0, 65535, 65536, 65537, 131072])
def test_retention_limit_is_per_arm_and_marks_truncation(length):
    oracle = transcript(b"O" * length)
    candidate = transcript(b"C" * length, arm="candidate")
    candidate["exit_code"] = 1
    retained = judge(oracle, candidate, StdioPolicy())[0]["stderr_evidence"]
    for arm, byte in (("oracle", b"O"), ("candidate", b"C")):
        assert base64.b64decode(retained[arm]["stderr_b64"], validate=True) == byte * min(length, 65536)
        assert retained[arm]["byte_count"] == length
        assert retained[arm]["truncated"] is (length > 65536)


@pytest.mark.parametrize("policy", [StdioPolicy(eof_orders=(("allowed",),)),
                                  StdioPolicy(non_eof_orders=(("open", "A", "B"),))])
def test_unobserved_frame_order_still_retains_stderr(policy):
    differences = judge(transcript(b"oracle"), transcript(b"candidate", arm="candidate"), policy)
    assert differences[0]["path"] == "/frames"
    assert base64.b64decode(differences[0]["stderr_evidence"]["candidate"]["stderr_b64"]) == b"candidate"


@pytest.mark.parametrize("field", ["stderr_b64", "pid", "captured_at_utc", "era"])
def test_missing_capture_evidence_is_incomplete(field):
    from evals.rust_port.phase1_receipts import receipt_status
    oracle, candidate = transcript(b"oracle"), transcript(b"candidate", arm="candidate")
    del candidate[field]
    differences = judge(oracle, candidate, StdioPolicy())
    assert receipt_status({"differences": differences, "coverage_complete": True}) == "incomplete"


@pytest.mark.parametrize("mutation", ["absent", "invalid-base64", "wrong-truncated", "missing-arm", "invalid-utc"])
def test_emitted_difference_evidence_is_checked_again_before_receipt_status(mutation):
    from evals.rust_port.phase1_receipts import receipt_status
    differences = judge(transcript(b"oracle"), transcript(b"candidate", arm="candidate"), StdioPolicy())
    if mutation == "absent":
        del differences[0]["stderr_evidence"]
    else:
        evidence = differences[0]["stderr_evidence"]
        if mutation == "missing-arm":
            del evidence["oracle"]
        elif mutation == "invalid-base64":
            evidence["candidate"]["stderr_b64"] = "!!!"
        elif mutation == "invalid-utc":
            evidence["candidate"]["captured_at_utc"] = "invented"
        else:
            evidence["candidate"]["truncated"] = True
    assert receipt_status({"differences": differences, "coverage_complete": True}) == "incomplete"


def test_real_capture_records_process_pid_and_capture_wall_clock(tmp_path):
    before = datetime.now(timezone.utc)
    observed = capture([sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'disposable diagnostic\\xff'); print('{\"id\":1}')"],
        cwd=tmp_path, home=tmp_path / "home", url="http://127.0.0.1:1",
        exercise=lambda wire: wire.response(1))
    after = datetime.now(timezone.utc)
    assert type(observed["pid"]) is int and observed["pid"] > 0
    assert observed["capture_kind"] == "process"
    assert before <= datetime.fromisoformat(observed["captured_at_utc"].replace("Z", "+00:00")) <= after
    assert base64.b64decode(observed["stderr_b64"]) == b"disposable diagnostic\xff"
    assert all(observed["cleanup"].values())


@pytest.mark.parametrize("missing_bytes", [False, True])
def test_actual_phase1_public_receipt_retains_evidence_and_exits_one(tmp_path, monkeypatch, missing_bytes):
    from evals.rust_port import full_bank, phase1, stdio_faults, stdio_process_controls, stdio_scenarios
    oracle, candidate = transcript(b"oracle"), transcript(b"mutated candidate", arm="candidate")
    differences = judge(oracle, candidate, StdioPolicy())
    if missing_bytes:
        del differences[0]["stderr_evidence"]["candidate"]["stderr_b64"]
    monkeypatch.setattr(phase1, "require_phase1_source", lambda root: {})
    monkeypatch.setattr(phase1, "runtime_metadata", lambda root: {})
    monkeypatch.setattr(phase1, "candidate_identity", lambda *args: {})
    monkeypatch.setattr(phase1, "candidate_bindings", lambda *args: {})
    monkeypatch.setattr(phase1, "process_tests", lambda *args: {"passed": True})
    monkeypatch.setattr(phase1, "corpus", lambda *args: {"arms": [oracle, candidate], "differences": differences})
    monkeypatch.setattr(phase1, "eof", lambda *args: {"cells": [], "differences": []})
    monkeypatch.setattr(stdio_faults, "run", lambda *args: {"cells": [], "differences": []})
    monkeypatch.setattr(stdio_scenarios, "run", lambda *args: {
        "startup": {"cells": []}, "concurrent": {"cells": []}, "differences": []})
    monkeypatch.setattr(stdio_process_controls, "run", lambda *args: {"controls": {}})
    monkeypatch.setattr(full_bank, "full_corpus", lambda: {})
    monkeypatch.setattr(full_bank, "run", lambda *args, **kwargs: ({}, {
        "status": "passed", "cases": [], "differences": [], "identity_proxy_validation": {},
        "oracle_source_check": {}, "graded_controls": {}}))
    raw, public = tmp_path / "raw.json", tmp_path / "public.json"
    monkeypatch.setattr(sys, "argv", ["phase1", "--oracle-root", str(tmp_path),
                                    "--out", str(raw), "--public-out", str(public)])
    assert phase1.main() == 1
    receipt = json.loads(public.read_text())
    assert receipt["status"] == ("incomplete" if missing_bytes else "failed")
    assert receipt["differences"] == json.loads(raw.read_text())["differences"]
    assert "arms" not in receipt
    if not missing_bytes:
        retained = receipt["differences"][0]["stderr_evidence"]
        assert base64.b64decode(retained["candidate"]["stderr_b64"]) == b"mutated candidate"
        assert retained["candidate"]["captured_at_utc"] == candidate["captured_at_utc"]


def test_eof_uses_live_oracle_capture_metadata_without_changing_frozen_order_policy(tmp_path, monkeypatch):
    from evals.rust_baseline import daemon
    from evals.rust_port import phase1, stdio_eof
    captured = []

    @contextmanager
    def private_directory():
        yield str(tmp_path)

    def observe(root, home, era, case, *, command=None, validate_oracle=True):
        captured.append((era, case, command is None))
        result = transcript(b"oracle" if command is None else b"candidate")
        result.update(era=era, case=case)
        return result

    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(stdio_eof, "observe", observe)
    monkeypatch.setattr(phase1, "eof_policy", lambda evidence, era, case: StdioPolicy())
    monkeypatch.setattr(phase1, "capture_platform", lambda: {"os": "different-platform"})
    result = phase1.eof(tmp_path, ["disposable candidate"])
    historical = json.loads((Path(phase1.__file__).parent / "stdio_eof_orders.json").read_text())
    assert sum(oracle for _, _, oracle in captured) == len({(cell["era"], cell["case"]) for cell in historical["cells"]})
    assert sum(not oracle for _, _, oracle in captured) == len(historical["cells"])
    assert result["evidence_sha256"] == historical["evidence_sha256"]
    assert all(row["stderr_evidence"]["oracle"]["captured_at_utc"] == "2026-10-05T00:00:00.123456Z"
               for row in result["differences"])


def test_startup_contract_difference_retains_both_live_arms(tmp_path, monkeypatch):
    from evals.rust_baseline import daemon
    from evals.rust_port import stdio_scenarios

    @contextmanager
    def private_directory():
        yield str(tmp_path)

    class Fixture:
        url = "http://127.0.0.1:1"
        records = []

        def __init__(self, health):
            pass

        def close(self):
            return {"server_stopped": True}

    def observed(command, **kwargs):
        result = transcript(b"candidate" if kwargs["boundary_errors"] else b"oracle")
        result["stdout_frames_b64"] = []
        return result

    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(stdio_scenarios, "ScenarioFixture", Fixture)
    monkeypatch.setattr(stdio_scenarios, "capture", observed)
    contract = {"cases": [{"case": "synthetic", "stderr_lf_template": "expected",
                           "exit_code": 0, "stdout_frame_count": 0}]}
    result = stdio_scenarios.startup(tmp_path, ["candidate"], contract)
    contract_rows = [row for row in result["differences"] if row["reason"] == "startup_stderr_bytes"]
    assert len(contract_rows) == 2
    for row in contract_rows:
        assert row["stderr_evidence"]["oracle"]["era"] == "2025-11-25"
        assert base64.b64decode(row["stderr_evidence"]["oracle"]["stderr_b64"]) == b"oracle"
        assert base64.b64decode(row["stderr_evidence"]["candidate"]["stderr_b64"]) == b"candidate"


@pytest.mark.parametrize("drift", ["stderr", "frames"])
def test_identical_live_eof_drift_still_fails_the_same_platform_frozen_bytes(tmp_path, monkeypatch, drift):
    from evals.rust_baseline import daemon
    from evals.rust_port import phase1, stdio_eof
    from evals.rust_port.phase1_receipts import receipt_status
    evidence = json.loads((Path(phase1.__file__).parent / "stdio_eof_orders.json").read_text())
    frozen = {(cell["era"], cell["case"]): cell for cell in evidence["cells"]}

    @contextmanager
    def private_directory():
        yield str(tmp_path)

    def observe(root, home, era, case, *, command=None, validate_oracle=True):
        cell = copy.deepcopy(frozen[era, case])
        cell.update(pid=111 if command is None else 222, capture_kind="process",
                    captured_at_utc="2026-10-05T00:00:00.123456Z")
        if drift == "stderr":
            cell["stderr_b64"] = base64.b64encode(b"identical live warning\n").decode()
        else:
            raw = base64.b64decode(cell["stdout_frames_b64"][0])
            cell["stdout_frames_b64"][0] = base64.b64encode(b" " + raw).decode()
        return cell

    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(stdio_eof, "observe", observe)
    monkeypatch.setattr(phase1, "capture_platform", lambda: evidence["capture_platform"])
    result = phase1.eof(tmp_path, ["candidate"])
    assert result["differences"]
    assert any(row["path"] == "/stderr" if drift == "stderr" else row["path"].startswith("/frames")
               for row in result["differences"])
    # An unverified frozen record has no genuine capture PID or clock to lend.
    retained = result["differences"][0]["stderr_evidence"]["oracle"]
    assert retained["pid"] is None
    assert retained["captured_at_utc"] is None
    assert receipt_status({"differences": result["differences"], "coverage_complete": True}) == "incomplete"


def sensitivity_transcript():
    frames = [{"id": "list", "result": {"tools": [{"description": "contract"}]}},
              {"id": "invalid", "result": {"isError": True}}]
    return {**transcript(), "stdout": frames, "capture_kind": "process",
            "stdout_frames_b64": [base64.b64encode((json.dumps(frame) + "\n").encode()).decode()
                                  for frame in frames]}


def test_judge_sensitivity_mutations_do_not_claim_candidate_process_provenance():
    from evals.rust_port import stdio_judge
    from evals.rust_port.phase1_receipts import receipt_status
    source = sensitivity_transcript()
    before = copy.deepcopy(source)
    controls = stdio_judge.judge_sensitivity_controls(source, StdioPolicy())
    assert source == before
    for control in controls.values():
        assert control["kind"] == "judge-sensitivity"
        assert control["source_capture"]["pid"] == source["pid"]
        assert control["source_capture"]["arm"] == "oracle"
        assert all("stderr_evidence" not in difference for difference in control["differences"])
    assert receipt_status({"differences": [], "coverage_complete": True,
                           "judge_sensitivity_controls": controls}) == "passed"


def test_synthetic_provenance_cannot_complete_a_claimed_process_difference():
    from evals.rust_port.phase1_receipts import receipt_status
    source = sensitivity_transcript()
    synthetic = copy.deepcopy(source)
    synthetic.update(capture_kind="judge-sensitivity", stderr_b64=base64.b64encode(b"invented").decode())
    differences = judge(source, synthetic, StdioPolicy())
    assert receipt_status({"differences": differences, "coverage_complete": True}) == "incomplete"


def test_verified_frozen_eof_bytes_use_genuine_live_metadata_for_candidate_failures(tmp_path, monkeypatch):
    from evals.rust_baseline import daemon
    from evals.rust_port import phase1, stdio_eof
    from evals.rust_port.phase1_receipts import receipt_status
    evidence = json.loads((Path(phase1.__file__).parent / "stdio_eof_orders.json").read_text())
    frozen = {(cell["era"], cell["case"]): cell for cell in evidence["cells"]}

    @contextmanager
    def private_directory():
        yield str(tmp_path)

    def observe(root, home, era, case, *, command=None, validate_oracle=True):
        cell = copy.deepcopy(frozen[era, case])
        cell.update(pid=111 if command is None else 222, capture_kind="process",
                    captured_at_utc="2026-10-05T00:00:00.123456Z")
        if command is not None:
            cell["stderr_b64"] = base64.b64encode(b"candidate warning\n").decode()
        return cell

    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(stdio_eof, "observe", observe)
    monkeypatch.setattr(phase1, "capture_platform", lambda: evidence["capture_platform"])
    result = phase1.eof(tmp_path, ["candidate"])
    assert all(cell["live_matches_frozen"] for cell in result["frozen_verifications"])
    assert len(result["live_oracle_captures"]) == len(frozen)
    assert result["differences"]
    for difference in result["differences"]:
        retained = difference["stderr_evidence"]
        assert retained["oracle"]["pid"] == 111
        assert retained["candidate"]["pid"] == 222
        assert retained["oracle"]["capture_kind"] == "process"
    assert receipt_status({"differences": result["differences"], "coverage_complete": True}) == "failed"
