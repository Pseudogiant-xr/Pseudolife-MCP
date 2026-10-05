"""Executable byte checks preserve the declared Python runtime invocation."""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import venv

import pytest

from evals.rust_port import cli_process


def spec():
    return {"id": "runtime-invocation", "mode": "fixture", "argv": [],
            "stdin_b64": "", "environment_deltas": {}, "pre_files_b64": {},
            "normalizations": []}


@pytest.mark.skipif(os.name != "posix", reason="Unix virtualenv executable symlink")
def test_symlink_venv_preserves_actual_python_prefix(tmp_path):
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    executable = runtime / "bin/python"
    assert executable.is_symlink()
    code = "import json,sys; print(json.dumps({'prefix':sys.prefix,'base':sys.base_prefix}))"
    commands = {"oracle": [str(executable), "-c", code]}
    observed = cli_process.observe(spec(), commands["oracle"], commands, root=tmp_path,
                                   home=tmp_path / "home", url="http://127.0.0.1:49152")
    response = observed["response"]
    actual = json.loads(base64.b64decode(response["stdout_b64"]))
    assert response["exit_code"] == 0 and response["stderr_b64"] == ""
    assert Path(actual["prefix"]) == runtime
    assert actual["prefix"] != actual["base"]
    execution = observed["execution"]
    assert execution["selected_prefix"] == commands["oracle"]
    assert execution["invoked_executable"] == str(executable)
    assert execution["resolved_executable"] == str(executable.resolve())
    assert execution["command_identity"]["executable_sha256"] == \
        cli_process.command_identity(commands["oracle"], tmp_path)["executable_sha256"]
    changed = {**observed, "execution": {}}
    assert cli_process.byte_payload(changed) == cli_process.byte_payload(observed)


@pytest.mark.skipif(os.name != "posix", reason="Unix executable symlink")
def test_equal_byte_symlink_cannot_relocate_runtime_outside_home(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.write_bytes(b"native fixture")
    commands = {"candidate": [str(original)]}

    def prepare(case, home, env, command, all_commands):
        link = tmp_path / "alias"
        link.symlink_to(original)
        return [str(link)]

    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="inside the home"):
        cli_process.observe(spec(), commands["candidate"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152", prepare=prepare)


@pytest.mark.skipif(os.name != "posix", reason="Unix executable symlink")
def test_snapshot_cannot_retarget_equal_byte_invocation_before_launch(tmp_path, monkeypatch):
    original = tmp_path / "original-runtime/python"
    other = tmp_path / "other-runtime/python"
    original.parent.mkdir()
    other.parent.mkdir()
    original.write_bytes(b"native fixture")
    other.write_bytes(original.read_bytes())
    link = tmp_path / "invoked"
    link.symlink_to(original)
    commands = {"candidate": [str(link)]}

    def retarget(home):
        link.unlink()
        link.symlink_to(other)
        return {}

    monkeypatch.setattr(cli_process, "snapshot", retarget)
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="command changed"):
        cli_process.observe(spec(), commands["candidate"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152")


@pytest.mark.skipif(os.name != "posix", reason="Unix executable symlink")
def test_preparer_cannot_retarget_original_equal_byte_runtime(tmp_path, monkeypatch):
    original = tmp_path / "original-runtime/python"
    other = tmp_path / "other-runtime/python"
    original.parent.mkdir()
    other.parent.mkdir()
    original.write_bytes(b"native fixture")
    other.write_bytes(original.read_bytes())
    link = tmp_path / "invoked"
    link.symlink_to(original)
    commands = {"candidate": [str(link)]}

    def retarget(case, home, env, command, all_commands):
        link.unlink()
        link.symlink_to(other)
        return command

    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="original executable target"):
        cli_process.observe(spec(), commands["candidate"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152", prepare=retarget)


def test_preserved_invocation_still_rejects_changed_prefix_arguments(tmp_path, monkeypatch):
    commands = {"oracle": [sys.executable, "-c", "pass"]}
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="original arm prefix"):
        cli_process.observe(spec(), commands["oracle"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152",
                            prepare=lambda case, home, env, command, all_commands: [*command, "extra"])


@pytest.mark.skipif(os.name != "posix", reason="Unix executable symlink")
def test_owned_copy_cannot_hide_original_equal_byte_runtime_retarget(tmp_path, monkeypatch):
    original = tmp_path / "original-runtime/python"
    other = tmp_path / "other-runtime/python"
    original.parent.mkdir()
    other.parent.mkdir()
    original.write_bytes(b"native fixture")
    other.write_bytes(original.read_bytes())
    link = tmp_path / "invoked"
    link.symlink_to(original)
    commands = {"candidate": [str(link)]}
    launched = []

    def prepare(case, home, env, command, all_commands):
        copy = home / "owned-runtime/python"
        copy.parent.mkdir(parents=True)
        copy.write_bytes(original.read_bytes())
        link.unlink()
        link.symlink_to(other)
        return [str(copy)]

    def observe_launch(*args, **kwargs):
        launched.append(True)
        return {"stdout_b64": "", "stderr_b64": "", "exit_code": 0}

    monkeypatch.setattr(cli_process, "run_cli", observe_launch)
    with pytest.raises(ValueError, match="original executable target"):
        cli_process.observe(spec(), commands["candidate"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152", prepare=prepare)
    assert not launched


@pytest.mark.skipif(os.name != "posix", reason="Unix executable symlink")
@pytest.mark.parametrize("boundary", ["prelaunch", "postcapture"])
def test_owned_copy_keeps_original_target_bound_through_capture(tmp_path, monkeypatch, boundary):
    original = tmp_path / "original-runtime/python"
    other = tmp_path / "other-runtime/python"
    original.parent.mkdir()
    other.parent.mkdir()
    original.write_bytes(b"native fixture")
    other.write_bytes(original.read_bytes())
    link = tmp_path / "invoked"
    link.symlink_to(original)
    commands = {"candidate": [str(link)]}
    snapshot = cli_process.snapshot
    launched = []

    def prepare(case, home, env, command, all_commands):
        copy = home / "owned-runtime/python"
        copy.parent.mkdir(parents=True)
        copy.write_bytes(original.read_bytes())
        return [str(copy)]

    def retarget():
        link.unlink()
        link.symlink_to(other)

    def before_capture(home):
        if boundary == "prelaunch":
            retarget()
        return snapshot(home)

    def observe_launch(*args, **kwargs):
        launched.append(True)
        if boundary == "postcapture":
            retarget()
        return {"stdout_b64": "", "stderr_b64": "", "exit_code": 0}

    monkeypatch.setattr(cli_process, "snapshot", before_capture)
    monkeypatch.setattr(cli_process, "run_cli", observe_launch)
    error = ValueError if boundary == "prelaunch" else RuntimeError
    with pytest.raises(error, match="command changed|executable changed"):
        cli_process.observe(spec(), commands["candidate"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152", prepare=prepare)
    assert len(launched) == (0 if boundary == "prelaunch" else 1)


@pytest.mark.skipif(os.name != "posix", reason="Unix executable symlink")
def test_context_cannot_retarget_equal_byte_invocation_before_launch(tmp_path, monkeypatch):
    original = tmp_path / "original-runtime/python"
    other = tmp_path / "other-runtime/python"
    original.parent.mkdir()
    other.parent.mkdir()
    original.write_bytes(b"native fixture")
    other.write_bytes(original.read_bytes())
    link = tmp_path / "invoked"
    link.symlink_to(original)
    commands = {"candidate": [str(link)]}

    @contextmanager
    def retarget(case, home):
        link.unlink()
        link.symlink_to(other)
        yield

    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="command changed"):
        cli_process.observe(spec(), commands["candidate"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:49152", process_scope=retarget)
