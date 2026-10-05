"""Owned briefing inputs and byte controls stay outside the timed CLI cell."""
import base64
from pathlib import Path

import pytest

from evals.rust_baseline import cli_measurement
from evals.rust_baseline.test_cli_measurement import installed_instrument
from evals.rust_baseline.transport import TOKEN


def briefing_instrument(tmp_path, monkeypatch):
    args, _, _, _, calls = installed_instrument(tmp_path, monkeypatch)
    args.mode = "briefing"
    args.argv_json = None
    case = {"id": "briefing-authenticated-one-synthetic-world-fact", "mode": "briefing",
            "argv": ["briefing"], "stdin_b64": "", "pre_files_b64": {}, "normalizations": [],
            "environment_deltas": {"PSEUDOLIFE_MCP_TOKEN": TOKEN}, "timeout_seconds": 20}
    return args, case, calls


def test_briefing_uses_fixture_inputs_reset_before_timer_and_existing_floors(tmp_path, monkeypatch):
    args, case, calls = briefing_instrument(tmp_path, monkeypatch)
    url = "http://127.0.0.1:19873"
    prepared = []
    clocks = []
    fixture_env = cli_measurement.fixture_env

    def environment(home, commands, owned_url):
        assert not home.exists()
        env = fixture_env(home, commands, owned_url)
        prepared.append(home)
        return env

    def observed(command, argv, **kwargs):
        env = kwargs["env"]
        home = Path(env["HOME"])
        assert not (home / "captured-state").exists()
        assert env["PSEUDOLIFE_MCP_DAEMON_URL"] == url
        assert env["PSEUDOLIFE_MCP_TOKEN"] == TOKEN
        assert Path(env["PSEUDOLIFE_MCP_TOKEN_FILE"]).read_bytes() == TOKEN.encode("ascii")
        assert env["PSEUDOLIFE_MCP_NO_SPAWN"] == "1" and env["CUDA_VISIBLE_DEVICES"] == "-1"
        assert env["OMP_NUM_THREADS"] == env["MKL_NUM_THREADS"] == "1"
        assert kwargs["stdin"] == b"" and kwargs["timeout"] == 20
        (home / "captured-state").write_bytes(b"fixed poststate")
        calls.append(list(argv))
        return {"exit_code": 0, "stdout_b64": base64.b64encode(b"verified fixture\r\n").decode(),
                "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "fixture_env", environment)
    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: clocks.append(len(prepared)) or 1.0)
    receipt = cli_measurement.measure(args, {}, case=case, fixture_url=url)
    assert len(calls) == len(prepared) == 62 and len(set(prepared)) == 1
    assert clocks[::2] == list(range(3, 63))
    assert receipt["prepared_case"] == case and receipt["fixture_url"] == url
    assert all(len(rows) == 30 for rows in receipt["runs"].values())
    assert receipt["runs"]["python"][0]["arm_order"] == ["python", "rust"]
    assert receipt["runs"]["python"][1]["arm_order"] == ["rust", "python"]
    assert all(row["files_and_environment_match_control"] for rows in receipt["runs"].values() for row in rows)
    assert all(receipt["metrics"][arm]["cold_start_to_exit_ms"]["p50"]["noise_floor_abs"] is not None
               for arm in ("python", "rust"))


@pytest.mark.parametrize("mutation", ["auth", "extra_env", "argv", "stdin", "files", "normalization", "url"])
def test_briefing_rejects_unaccepted_inputs_before_launch(tmp_path, monkeypatch, mutation):
    args, case, calls = briefing_instrument(tmp_path, monkeypatch)
    url = "http://127.0.0.1:19873"
    if mutation == "auth":
        case["environment_deltas"] = {}
    elif mutation == "extra_env":
        case["environment_deltas"]["PSEUDOLIFE_MCP_NO_SPAWN"] = "0"
    elif mutation == "argv":
        args.argv_json = '["briefing", "--json"]'
        case["argv"].append("--json")
    elif mutation == "stdin":
        case["stdin_b64"] = "YQ=="
    elif mutation == "files":
        case["pre_files_b64"] = {"outside": "YQ=="}
    elif mutation == "normalization":
        case["normalizations"] = ["paths"]
    else:
        url = "https://example.com"
    with pytest.raises(ValueError):
        cli_measurement.measure(args, {}, case=case, fixture_url=url)
    assert calls == []


@pytest.mark.parametrize("field", ["exit_code", "stdout_b64", "stderr_b64", "post_files"])
def test_briefing_control_mutations_refuse_before_timing(tmp_path, monkeypatch, field):
    args, case, calls = briefing_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        calls.append(argv)
        result = {"exit_code": 0, "stdout_b64": "YQ0K", "stderr_b64": ""}
        if len(command) == 1:
            if field == "post_files":
                Path(kwargs["env"]["HOME"], "captured-state").write_bytes(b"changed")
            else:
                result[field] = 1 if field == "exit_code" else "Yg=="
        return result

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="byte control failed"):
        cli_measurement.measure(args, {}, case=case, fixture_url="http://127.0.0.1:19873")
    assert len(calls) == 2


def test_briefing_empty_fallback_refuses_before_timing(tmp_path, monkeypatch):
    args, case, calls = briefing_instrument(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_measurement.time, "perf_counter", lambda: pytest.fail("must not time"))
    with pytest.raises(RuntimeError, match="nonempty stdout"):
        cli_measurement.measure(args, {}, case=case, fixture_url="http://127.0.0.1:19873")
    assert len(calls) == 2


def test_briefing_preparation_cannot_replace_owned_url(tmp_path, monkeypatch):
    args, case, calls = briefing_instrument(tmp_path, monkeypatch)

    def redirect(case, home, env, command, commands):
        env["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:8765"
        return command

    with pytest.raises(ValueError, match="retain owned daemon"):
        cli_measurement.measure(args, {}, case=case, prepare=redirect, fixture_url="http://127.0.0.1:19873")
    assert calls == []


def test_briefing_environment_mutation_during_capture_is_rejected(tmp_path, monkeypatch):
    args, case, calls = briefing_instrument(tmp_path, monkeypatch)

    def observed(command, argv, **kwargs):
        calls.append(argv)
        kwargs["env"]["PSEUDOLIFE_MCP_TOKEN"] = "changed"
        return {"exit_code": 0, "stdout_b64": "YQ==", "stderr_b64": ""}

    monkeypatch.setattr(cli_measurement, "run_cli", observed)
    with pytest.raises(RuntimeError, match="effective environment changed"):
        cli_measurement.measure(args, {}, case=case, fixture_url="http://127.0.0.1:19873")
    assert len(calls) == 1


def test_briefing_preparation_cannot_change_inputs_between_samples(tmp_path, monkeypatch):
    args, case, calls = briefing_instrument(tmp_path, monkeypatch)

    def changed(case, home, env, command, commands):
        case["environment_deltas"]["PSEUDOLIFE_MCP_TOKEN"] = "changed"
        return command

    with pytest.raises(ValueError, match="recorded briefing inputs"):
        cli_measurement.measure(args, {}, case=case, prepare=changed, fixture_url="http://127.0.0.1:19873")
    assert calls == [] and case["environment_deltas"] == {"PSEUDOLIFE_MCP_TOKEN": TOKEN}
