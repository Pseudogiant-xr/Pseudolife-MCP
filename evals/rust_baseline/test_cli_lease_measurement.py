"""The retained owned-board check stays exact before any timing is accepted."""
import base64
import copy
import json
from pathlib import Path
import sys

import pytest

from evals.rust_baseline import cli_measurement
from evals.rust_baseline.test_cli_measurement import installed_instrument
from evals.rust_baseline.transport import TOKEN


CASE = {"id": "lease-check-absent-json", "mode": "lease-check",
        "argv": ["lease", "check", "sample", "--json"], "stdin_b64": "",
        "environment_deltas": {}, "pre_files_b64": {".pseudolife-mcp/locks/instance.id": "MDEyMzQ1Njc4OWFiCg=="},
        "normalizations": [], "timeout_seconds": 20}
STATE = {"name": "sample", "held": False, "local": {"file": "lease-sample.lock", "state": None},
         "board": {"available": True, "reason": None, "holder": None, "expected_end": None,
                   "stale": False, "queued": 0, "queue": []}}


def lease_instrument(tmp_path, monkeypatch):
    args, _, _, _, calls = installed_instrument(tmp_path, monkeypatch)
    args.mode = "lease-check"
    args.argv_json = json.dumps(CASE["argv"])
    return args, copy.deepcopy(CASE), calls


def response(state=None):
    newline = "\r\n" if sys.platform == "win32" else "\n"
    return {"exit_code": 0, "stdout_b64": base64.b64encode(
        (newline.join(json.dumps(STATE if state is None else state, indent=2).split("\n")) + newline).encode()).decode(),
        "stderr_b64": ""}


def test_owned_lease_check_retains_instance_token_file_and_pre_timer_reset(tmp_path, monkeypatch):
    args, case, calls = lease_instrument(tmp_path, monkeypatch)
    url = "http://127.0.0.1:19873"
    prepared = []
    clocks = []
    fixture_env = cli_measurement.fixture_env
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "unowned-parent-setting")

    def environment(home, commands, owned_url):
        assert not home.exists()
        env = fixture_env(home, commands, owned_url)
        prepared.append(home)
        return env

    def observed(command, argv, **kwargs):
        env = kwargs["env"]
        home = Path(env["HOME"])
        assert env["PSEUDOLIFE_MCP_DAEMON_URL"] == url and "PSEUDOLIFE_MCP_TOKEN" not in env
        assert Path(env["PSEUDOLIFE_MCP_TOKEN_FILE"]).read_bytes() == TOKEN.encode("ascii")
        assert (home / ".pseudolife-mcp/locks/instance.id").read_bytes() == b"0123456789ab\n"
        assert not (home / ".pseudolife-mcp/locks/lease-sample.lock").exists()
        assert env["PSEUDOLIFE_MCP_NO_SPAWN"] == "1" and env["CUDA_VISIBLE_DEVICES"] == "-1"
        assert env["OMP_NUM_THREADS"] == env["MKL_NUM_THREADS"] == "1"
        assert kwargs["stdin"] == b"" and kwargs["timeout"] == 20
        calls.append(list(argv))
        return response()

    monkeypatch.setattr(cli_measurement, "fixture_env", environment)
    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: clocks.append(len(prepared)) or 1.0)
    receipt = cli_measurement.measure(args, {}, case=case, fixture_url=url)
    assert len(calls) == len(prepared) == 62 and len(set(prepared)) == 1
    assert clocks[::2] == list(range(3, 63))
    assert receipt["prepared_case"] == CASE and receipt["fixture_url"] == url
    assert all(row == CASE["argv"] for row in calls)
    assert all(len(rows) == 30 for rows in receipt["runs"].values())
    assert receipt["runs"]["python"][0]["arm_order"] == ["python", "rust"]
    assert receipt["runs"]["python"][1]["arm_order"] == ["rust", "python"]
    assert all(cell["pre_files_b64"] == cell["post_files_b64"] for cell in receipt["prepared_controls"].values())
    assert all(receipt["metrics"][arm]["cold_start_to_exit_ms"]["p50"]["noise_floor_abs"] is not None
               for arm in ("python", "rust"))


@pytest.mark.parametrize("mutation", ["mode", "argv", "token", "files", "stdin", "normalization", "timeout", "url", "prepare"])
def test_owned_lease_check_rejects_other_inputs_before_launch(tmp_path, monkeypatch, mutation):
    args, case, calls = lease_instrument(tmp_path, monkeypatch)
    url = "http://127.0.0.1:19873"
    prepare = None
    if mutation == "mode":
        args.mode = case["mode"] = "lease-list"
        case["argv"] = ["lease", "list", "--json"]
        args.argv_json = json.dumps(case["argv"])
    elif mutation == "argv":
        case["argv"].append("--help")
        args.argv_json = json.dumps(case["argv"])
    elif mutation == "token":
        case["environment_deltas"] = {"PSEUDOLIFE_MCP_TOKEN": TOKEN}
    elif mutation == "files":
        case["pre_files_b64"][".pseudolife-mcp/locks/instance.id"] = "YQ=="
    elif mutation == "stdin":
        case["stdin_b64"] = "YQ=="
    elif mutation == "normalization":
        case["normalizations"] = ["paths"]
    elif mutation == "timeout":
        case["timeout_seconds"] = 1
    elif mutation == "prepare":
        prepare = lambda *arguments: pytest.fail("unrecorded callback must not run")
    else:
        url = "https://example.com"
    with pytest.raises(ValueError):
        cli_measurement.measure(args, {}, case=case, prepare=prepare, fixture_url=url)
    assert calls == []


@pytest.mark.parametrize("field", ["exit_code", "stdout_b64", "stderr_b64", "post_files"])
def test_owned_lease_check_control_mutations_refuse_before_timing(tmp_path, monkeypatch, field):
    args, case, calls = lease_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        calls.append(argv)
        result = response()
        if len(command) == 1:
            if field == "post_files":
                Path(kwargs["env"]["HOME"], "captured-state").write_bytes(b"changed")
            else:
                result[field] = 1 if field == "exit_code" else "Yg=="
        return result

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="control.*(failed|preserve)"):
        cli_measurement.measure(args, {}, case=case, fixture_url="http://127.0.0.1:19873")
    assert len(calls) == 2


def test_owned_lease_check_rejects_equal_unavailable_board_before_timing(tmp_path, monkeypatch):
    args, case, calls = lease_instrument(tmp_path, monkeypatch)
    state = copy.deepcopy(STATE)
    state["board"]["available"] = False

    def observed(command, argv, **kwargs):
        calls.append(argv)
        return response(state)

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="available empty owned board"):
        cli_measurement.measure(args, {}, case=case, fixture_url="http://127.0.0.1:19873")
    assert len(calls) == 2


def test_owned_lease_check_rejects_equal_unexpected_poststate_before_timing(tmp_path, monkeypatch):
    args, case, calls = lease_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        calls.append(argv)
        Path(kwargs["env"]["HOME"], "captured-state").write_bytes(b"same unexpected state")
        return response()

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="preserve the recorded home bytes"):
        cli_measurement.measure(args, {}, case=case, fixture_url="http://127.0.0.1:19873")
    assert len(calls) == 2


def test_owned_lease_check_effective_environment_cannot_change_during_capture(tmp_path, monkeypatch):
    args, case, calls = lease_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        calls.append(argv)
        kwargs["env"]["PSEUDOLIFE_MCP_TOKEN"] = "changed"
        return response()

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    with pytest.raises(RuntimeError, match="effective environment changed"):
        cli_measurement.measure(args, {}, case=case, fixture_url="http://127.0.0.1:19873")
    assert len(calls) == 1
