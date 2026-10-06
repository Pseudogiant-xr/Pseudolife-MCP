import base64
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from evals.rust_port import stdio_process_controls


def transcript():
    raw = (json.dumps({"jsonrpc": "2.0", "id": "open", "result": {
        "protocolVersion": "2025-11-25", "serverInfo": {"name": "fixture", "version": "1"}
    }}, separators=(",", ":")) + "\n").encode()
    return {"stdout_frames_b64": [base64.b64encode(raw).decode()],
            "stderr_b64": "", "exit_code": 0}


def changed(original, fault):
    result = deepcopy(original)
    raw = base64.b64decode(result["stdout_frames_b64"][0])
    if fault == "exit":
        result["exit_code"] = 1
    elif fault == "stderr":
        result["stderr_b64"] = base64.b64encode(b"unrelated diagnostic\n").decode()
    elif fault == "frame":
        raw = raw.replace(b'"version":"1"', b'"version":"different"')
    elif fault == "raw-whitespace":
        raw = b" " + raw
    elif fault == "wrong-protocol":
        raw = raw.replace(b'"2025-11-25"', b'"synthetic-invalid"')
    elif fault == "duplicate-key":
        raw = b'{"jsonrpc":"2.0",' + raw[1:]
    else:
        raise AssertionError("unknown fixture fault")
    result["stdout_frames_b64"][0] = base64.b64encode(raw).decode()
    return result


@pytest.fixture
def control_runner(monkeypatch, tmp_path):
    from evals.rust_baseline import daemon

    @contextmanager
    def private_directory():
        yield str(tmp_path)

    class Fixture:
        url = "http://127.0.0.1:1"

        def __init__(self, fault):
            assert fault == "healthy"
            self.closed = False

        def close(self):
            self.closed = True
            return {"server_stopped": True}

    monkeypatch.setattr(daemon, "private_directory", private_directory)
    monkeypatch.setattr(stdio_process_controls, "HangingFixture", Fixture)

    def run(*, faulty_control=None, fault=None):
        oracle = transcript()

        def capture(command, **kwargs):
            if command[-1] == "pseudolife_memory.cli" or command[-1] == "identity":
                if faulty_control == "identity" and command[-1] == "identity":
                    return changed(oracle, fault)
                return deepcopy(oracle)
            control = command[-1]
            return changed(oracle, fault if control == faulty_control else control)

        monkeypatch.setattr(stdio_process_controls, "capture", capture)
        return stdio_process_controls.run(tmp_path)

    return run


@pytest.mark.parametrize("control", ["wrong-protocol", "duplicate-key"])
@pytest.mark.parametrize("fault", ["exit", "stderr", "frame", "raw-whitespace"])
def test_unrelated_difference_cannot_satisfy_a_named_broken_control(control_runner, control, fault):
    with pytest.raises(RuntimeError, match="forwarding candidate control failed: " + control):
        control_runner(faulty_control=control, fault=fault)


@pytest.mark.parametrize("fault", ["exit", "stderr", "frame"])
def test_identity_control_still_requires_zero_differences(control_runner, fault):
    with pytest.raises(RuntimeError, match="forwarding candidate control failed: identity"):
        control_runner(faulty_control="identity", fault=fault)


def test_named_defects_are_required_through_the_actual_stdio_judge(control_runner):
    result = control_runner()
    controls = result["controls"]
    assert controls["identity"]["differences"] == []
    assert any(row["path"] == "/stderr" for row in controls["stderr"]["differences"])
    assert controls["stderr"]["differences"][0]["stderr_evidence"]["candidate"]["stderr_b64"] == changed(transcript(), "stderr")["stderr_b64"]
    assert {"path": "/json/0/result/protocolVersion", "reason": "value"} in controls["wrong-protocol"]["differences"]
    assert controls["duplicate-key"]["differences"] == [{"path": "/", "reason": "duplicate_json_key"}]
    assert all(item["passed"] and item["policy"] == "stdio-raw-compared" for item in controls.values())
    assert result["fixture_cleanup"]["server_stopped"]
