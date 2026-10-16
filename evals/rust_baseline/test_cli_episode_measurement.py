"""Episode closure is checked independently outside each timed public CLI cell."""
import base64
import copy
import json
from pathlib import Path

import pytest

from evals.rust_baseline import cli_measurement
from evals.rust_baseline.test_cli_measurement import installed_instrument
from evals.rust_baseline.transport import TOKEN
from evals.rust_port.episode_corpus import cases


def episode_instrument(tmp_path, monkeypatch):
    args, _, _, _, calls = installed_instrument(tmp_path, monkeypatch)
    case = next(row for row in cases() if row["id"] == "episode-key-7-episode-end")
    case["environment_deltas"] = {"PSEUDOLIFE_MCP_TOKEN": TOKEN}
    args.mode = "episode-end"
    args.argv_json = json.dumps(case["argv"])
    args.layout = "bare"
    state = {"open": False, "history": [], "events": []}

    def prepare(recorded, home, env, command, commands):
        assert recorded == case and not state["open"]
        assert env["PSEUDOLIFE_MCP_TOKEN"] == TOKEN
        state["open"] = True
        state["events"].append("seed")
        return command

    def observed(command, argv, **kwargs):
        assert state["open"]
        assert kwargs["stdin"] == b'{"session_id": 42}'
        assert argv == ["episode-end", "--help", "ignored"]
        assert kwargs["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == "http://127.0.0.1:19873"
        state["open"] = False
        state["events"].append("public-cli")
        calls.append(list(argv))
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    def verify(recorded, home, env, command, commands, response):
        assert not state["open"], "seeded root was not closed"
        state["events"].append("readback")
        raw = {"root_id": "synthetic-" + str(len(state["history"])),
               "ended_at": 123 + len(state["history"]), "end_reason": "end"}
        state["history"].append(raw)
        return raw

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    return args, case, prepare, verify, calls, state


def measure(args, case, prepare, verify):
    return cli_measurement.measure(args, {}, case=case, prepare=prepare, verify=verify,
                                   fixture_url="http://127.0.0.1:19873")


def test_episode_setup_and_readback_are_untimed_and_existing_floors_remain(tmp_path, monkeypatch):
    args, case, prepare, verify, calls, state = episode_instrument(tmp_path, monkeypatch)
    clocks = []

    def clock():
        clocks.append((len(calls), list(state["events"][-2:])))
        state["events"].append("clock")
        return 1.0

    monkeypatch.setattr(cli_measurement.time, "perf_counter", clock)
    receipt = measure(args, case, prepare, verify)
    assert len(calls) == len(state["history"]) == 62
    assert len(clocks) == 120
    assert all(events[-1] == "seed" for _, events in clocks[::2])
    assert all(events[-1] == "public-cli" for _, events in clocks[1::2])
    assert clocks[0][0] == 2 and clocks[1][0] == 3
    assert all(len(rows) == 30 for rows in receipt["runs"].values())
    assert receipt["runs"]["python"][0]["arm_order"] == ["python", "rust"]
    assert receipt["runs"]["python"][1]["arm_order"] == ["rust", "python"]
    assert [row["timed"] for row in receipt["independent_state_checks"]] == [False, False] + [True] * 60
    assert [row["readback"] for row in receipt["independent_state_checks"]] == state["history"]
    assert receipt["database_byte_equality_claimed"] is False
    assert all(cell["pre_files_b64"] == cell["post_files_b64"] for cell in receipt["prepared_controls"].values())
    assert all(receipt["metrics"][arm]["cold_start_to_exit_ms"]["p50"]["noise_floor_abs"] is not None
               for arm in ("python", "rust"))


@pytest.mark.parametrize("mutation", ["case", "mode", "argv", "stdin", "auth", "extra_env", "files", "normalization", "layout", "prepare", "verify", "url"])
def test_episode_rejects_other_fixture_inputs_before_launch(tmp_path, monkeypatch, mutation):
    args, case, prepare, verify, calls, _ = episode_instrument(tmp_path, monkeypatch)
    url = "http://127.0.0.1:19873"
    if mutation == "case":
        case["id"] = "episode-blank-episode-end"
    elif mutation == "mode":
        args.mode = "episode-start"
        args.argv_json = '["episode-start"]'
    elif mutation == "argv":
        case["argv"] = ["episode-end"]
        args.argv_json = json.dumps(case["argv"])
    elif mutation == "stdin":
        case["stdin_b64"] = base64.b64encode(b'{"session_id": 42}\n').decode()
    elif mutation == "auth":
        case["environment_deltas"] = {}
    elif mutation == "extra_env":
        case["environment_deltas"]["PSEUDOLIFE_MCP_NO_SPAWN"] = "0"
    elif mutation == "files":
        case["pre_files_b64"] = {"state": "YQ=="}
    elif mutation == "normalization":
        case["normalizations"] = ["clock"]
    elif mutation == "layout":
        args.layout = "installed"
    elif mutation == "prepare":
        prepare = None
    elif mutation == "verify":
        verify = None
    else:
        url = "https://example.com"
    with pytest.raises(ValueError):
        cli_measurement.measure(args, {}, case=case, prepare=prepare, verify=verify, fixture_url=url)
    assert calls == []


@pytest.mark.parametrize("field", ["exit_code", "stdout_b64", "stderr_b64", "home"])
def test_episode_equal_arm_wrong_result_is_rejected_before_timing(tmp_path, monkeypatch, field):
    args, case, prepare, verify, calls, state = episode_instrument(tmp_path, monkeypatch)

    def wrong(command, argv, **kwargs):
        calls.append(argv)
        state["open"] = False
        response = {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}
        if field == "home":
            Path(kwargs["env"]["HOME"], "captured-state").write_bytes(b"same wrong bytes")
        else:
            response[field] = 1 if field == "exit_code" else "YQ=="
        return response

    monkeypatch.setattr(cli_measurement, "run_cli", wrong)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="accepted empty streams and unchanged home"):
        measure(args, case, prepare, verify)
    assert len(calls) == len(state["history"]) == 1


@pytest.mark.parametrize("after_controls", [False, True])
def test_episode_equal_arm_wrong_state_is_rejected(tmp_path, monkeypatch, after_controls):
    args, case, prepare, verify, calls, state = episode_instrument(tmp_path, monkeypatch)
    observed = cli_measurement.run_cli

    def wrong(command, argv, **kwargs):
        response = observed(command, argv, **kwargs)
        if not after_controls or len(calls) > 2:
            state["open"] = True
        return response

    monkeypatch.setattr(cli_measurement, "run_cli", wrong)
    if not after_controls:
        monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(AssertionError, match="seeded root was not closed"):
        measure(args, case, prepare, verify)
    assert len(calls) == (3 if after_controls else 1)


@pytest.mark.parametrize("mutation", ["case", "url", "cpu", "token", "executable"])
def test_episode_prepare_cannot_change_accepted_binding(tmp_path, monkeypatch, mutation):
    args, case, prepare, verify, calls, _ = episode_instrument(tmp_path, monkeypatch)
    original = copy.deepcopy(case)

    def changed(recorded, home, env, command, commands):
        selected = prepare(recorded, home, env, command, commands)
        if mutation == "case":
            recorded["stdin_b64"] = ""
        elif mutation == "executable":
            Path(command[0]).write_bytes(b"changed executable")
        else:
            env[{"url": "PSEUDOLIFE_MCP_DAEMON_URL", "cpu": "CUDA_VISIBLE_DEVICES", "token": "PSEUDOLIFE_MCP_TOKEN"}[mutation]] = "changed"
        return selected

    with pytest.raises(ValueError):
        measure(args, case, changed, verify)
    assert calls == [] and case == original


@pytest.mark.parametrize("mutation", ["env", "home", "executable"])
def test_episode_readback_cannot_change_capture_binding(tmp_path, monkeypatch, mutation):
    args, case, prepare, verify, calls, _ = episode_instrument(tmp_path, monkeypatch)

    def changed(recorded, home, env, command, commands, response):
        raw = verify(recorded, home, env, command, commands, response)
        if mutation == "env":
            env["CUDA_VISIBLE_DEVICES"] = "0"
        elif mutation == "home":
            (home / "captured-state").write_bytes(b"changed")
        else:
            Path(command[0]).write_bytes(b"changed executable")
        return raw

    with pytest.raises(RuntimeError, match="changed"):
        measure(args, case, prepare, changed)
    assert len(calls) == 1
