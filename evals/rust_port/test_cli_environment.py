"""The reported environment remains bound to the admitted CLI launch."""
import base64
from contextlib import contextmanager
import json
import sys

import pytest

from evals.rust_port import cli_process


def case():
    return {"id": "fixture-environment", "mode": "fixture", "argv": [],
            "stdin_b64": "", "environment_deltas": {"CLI_FIXTURE_VALUE": "non-ascii-\u00e9"},
            "pre_files_b64": {}, "normalizations": []}


@pytest.mark.parametrize("boundary", ["capture", "context-exit", "poststate"])
@pytest.mark.parametrize("mutation", ["add", "remove", "change", "owned-url", "no-spawn"])
def test_environment_mutations_refuse_after_launch(tmp_path, monkeypatch, boundary, mutation):
    commands = {"oracle": [sys.executable]}
    saved = {}
    launches = []
    snapshot = cli_process.snapshot

    def prepare(spec, home, env, command, commands):
        saved["env"] = env
        return command

    def mutate():
        env = saved["env"]
        if mutation == "add":
            env["CLI_ADDED_VALUE"] = "added"
        elif mutation == "remove":
            env.pop("CLI_FIXTURE_VALUE")
        elif mutation == "change":
            env["CLI_FIXTURE_VALUE"] = "changed"
        elif mutation == "owned-url":
            env["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:49153"
        else:
            env["PSEUDOLIFE_MCP_NO_SPAWN"] = "0"

    @contextmanager
    def scope(spec, home):
        yield
        if boundary == "context-exit":
            mutate()

    def capture(*args, **kwargs):
        launches.append(dict(kwargs["env"]))
        if boundary == "capture":
            mutate()
        return {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}

    def collect(home):
        files = snapshot(home)
        if launches and boundary == "poststate":
            mutate()
        return files

    monkeypatch.setattr(cli_process, "run_cli", capture)
    monkeypatch.setattr(cli_process, "snapshot", collect)
    with pytest.raises(ValueError, match="effective environment changed|owned daemon"):
        cli_process.observe(case(), commands["oracle"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152",
                            prepare=prepare, process_scope=scope)
    assert len(launches) == 1
    assert launches[0]["PSEUDOLIFE_MCP_DAEMON_URL"] == "http://127.0.0.1:49152"
    assert launches[0]["PSEUDOLIFE_MCP_NO_SPAWN"] == "1"
    assert launches[0]["CLI_FIXTURE_VALUE"] == "non-ascii-\u00e9"
    assert "CLI_ADDED_VALUE" not in launches[0]


def test_success_records_actual_environment_without_callback_alias(tmp_path):
    code = ("import json,os; print(json.dumps({key:os.environ[key] for key in "
            "['CLI_FIXTURE_VALUE','PSEUDOLIFE_MCP_DAEMON_URL','PSEUDOLIFE_MCP_NO_SPAWN']}))")
    commands = {"oracle": [sys.executable, "-c", code]}
    saved = {}

    def prepare(spec, home, env, command, commands):
        saved["env"] = env
        return command

    observed = cli_process.observe(case(), commands["oracle"], commands, root=tmp_path,
                                   home=tmp_path / "home", url="http://127.0.0.1:49152", prepare=prepare)
    assert observed["response"]["exit_code"] == 0
    assert observed["response"]["stderr_b64"] == ""
    launched = json.loads(base64.b64decode(observed["response"]["stdout_b64"]))
    assert launched == {key: observed["environment"][key] for key in launched}
    assert observed["environment"] == saved["env"]
    assert observed["environment"] is not saved["env"]
    saved["env"]["CLI_FIXTURE_VALUE"] = "after acceptance"
    assert observed["environment"]["CLI_FIXTURE_VALUE"] == "non-ascii-\u00e9"
