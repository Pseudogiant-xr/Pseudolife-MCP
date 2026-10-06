"""Owned empty-board lease list retains exact process inputs and state."""
import base64
import copy
import json
from pathlib import Path
import sys

import pytest

from evals.rust_baseline import cli_measurement
from evals.rust_baseline.test_cli_measurement import installed_instrument
from evals.rust_baseline.transport import TOKEN


CASE = {"id": "lease-list-empty-json", "mode": "lease-list",
        "argv": ["lease", "list", "--json"], "stdin_b64": "", "environment_deltas": {},
        "pre_files_b64": {".pseudolife-mcp/locks/instance.id": "MDEyMzQ1Njc4OWFiCg=="},
        "normalizations": [], "timeout_seconds": 20}
URL = "http://127.0.0.1:19873"


def list_instrument(tmp_path, monkeypatch):
    args, _, _, _, calls = installed_instrument(tmp_path, monkeypatch)
    args.mode = "lease-list"
    args.argv_json = json.dumps(CASE["argv"])
    return args, copy.deepcopy(CASE), calls


def response(env, *, mutation=None):
    state = {"board": {"url": URL, "available": True, "reason": None,
                       "truncated": False, "leases": []},
             "lock_dir": str(Path(env["HOME"]) / ".pseudolife-mcp/locks"),
             "local": [], "test_suite_lock": None}
    if mutation == "unavailable":
        state["board"]["available"] = False
    elif mutation == "url":
        state["board"]["url"] += "/wrong"
    elif mutation == "lock_dir":
        state["lock_dir"] += "/wrong"
    elif mutation == "local":
        state["local"] = [{"file": "lease-extra.lock", "state": "free"}]
    elif mutation == "truncated":
        state["board"]["truncated"] = True
    newline = "\r\n" if sys.platform == "win32" else "\n"
    raw = (newline.join(json.dumps(state, indent=2).split("\n")) + newline).encode()
    return {"exit_code": 0, "stdout_b64": base64.b64encode(raw).decode(), "stderr_b64": ""}


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_exact_list_case_reset_outside_clock_and_native_line_endings(tmp_path, monkeypatch, platform):
    args, case, calls = list_instrument(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "platform", platform)
    clocks = []

    def observed(command, argv, **kwargs):
        env = kwargs["env"]
        home = Path(env["HOME"])
        assert "PSEUDOLIFE_MCP_TOKEN" not in env and env["PSEUDOLIFE_MCP_DAEMON_URL"] == URL
        assert Path(env["PSEUDOLIFE_MCP_TOKEN_FILE"]).read_bytes() == TOKEN.encode()
        assert (home / ".pseudolife-mcp/locks/instance.id").read_bytes() == b"0123456789ab\n"
        assert sorted(p.name for p in (home / ".pseudolife-mcp/locks").iterdir()) == ["instance.id"]
        assert env["CUDA_VISIBLE_DEVICES"] == "-1" and env["PSEUDOLIFE_MCP_NO_SPAWN"] == "1"
        assert kwargs["stdin"] == b"" and kwargs["timeout"] == 20
        calls.append(argv)
        return response(env)

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: clocks.append(len(calls)) or 1.0)
    receipt = cli_measurement.measure(args, {}, case=case, fixture_url=URL)
    assert cli_measurement.lease_list_case() == receipt["prepared_case"] == CASE
    assert len(calls) == 62 and clocks[::2] == list(range(2, 62))
    assert all(argv == CASE["argv"] for argv in calls)
    assert all(len(rows) == 30 for rows in receipt["runs"].values())
    assert receipt["runs"]["python"][1]["arm_order"] == ["rust", "python"]
    assert all(cell["pre_files_b64"] == cell["post_files_b64"] for cell in receipt["prepared_controls"].values())
    assert all(receipt["metrics"][arm]["cold_start_to_exit_ms"]["p50"]["noise_floor_abs"] is not None
               for arm in ("python", "rust"))


@pytest.mark.parametrize("mutation", ["id", "argv", "token", "files", "stdin", "normalization", "timeout", "run"])
def test_list_admission_refuses_other_cases_before_launch(tmp_path, monkeypatch, mutation):
    args, case, calls = list_instrument(tmp_path, monkeypatch)
    if mutation == "id":
        case["id"] = "lease-list-free-json"
    elif mutation == "argv":
        case["argv"].append("--help")
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
    else:
        args.mode = case["mode"] = "lease-run"
        case["argv"] = ["lease", "run", "sample", "--", "child"]
    args.argv_json = json.dumps(case["argv"])
    with pytest.raises(ValueError):
        cli_measurement.measure(args, {}, case=case, fixture_url=URL)
    assert calls == []


@pytest.mark.parametrize("mutation", ["unavailable", "url", "lock_dir", "local", "truncated", "poststate"])
def test_equal_but_unexpected_list_controls_refuse_before_timing(tmp_path, monkeypatch, mutation):
    args, case, calls = list_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        calls.append(argv)
        if mutation == "poststate":
            Path(kwargs["env"]["HOME"], "unexpected").write_bytes(b"same unexpected state")
        return response(kwargs["env"], mutation=mutation)

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="control.*(owned board|preserve)"):
        cli_measurement.measure(args, {}, case=case, fixture_url=URL)
    assert len(calls) == 2


def test_list_timed_stream_change_refuses_after_exact_controls(tmp_path, monkeypatch):
    args, case, calls = list_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        calls.append(argv)
        result = response(kwargs["env"])
        if len(calls) == 3:
            result["stdout_b64"] = base64.b64encode(base64.b64decode(result["stdout_b64"]) + b"x").decode()
        return result

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    with pytest.raises(RuntimeError, match="CLI bytes changed"):
        cli_measurement.measure(args, {}, case=case, fixture_url=URL)
    assert len(calls) == 3
