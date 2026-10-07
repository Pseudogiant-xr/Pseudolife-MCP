"""An installed marker cannot identify a runtime without its canonical console."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.rust_port import cli_version


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_missing_console_case_retains_marker_interpreter_and_native(tmp_path, monkeypatch, platform):
    seed = tmp_path / "seed"
    seed.mkdir()
    files = {name: seed / name for name in ("python", "console", "config", "native")}
    for name, path in files.items():
        path.write_bytes(name.encode())
    monkeypatch.setattr(cli_version, "seed_context", lambda root: {
        **files, "base_interpreter": str(files["python"]), "pythonpath": str(seed), "version": "0.17.0"})
    monkeypatch.setattr(cli_version, "os", SimpleNamespace(name=platform))
    case = next(row for row in cli_version.cases() if row["id"] == "version-missing-console")
    home = tmp_path / "home"
    home.mkdir()
    env = {"LOCALAPPDATA": str(home / "local"), "XDG_DATA_HOME": str(home / "data")}
    commands = {"oracle": [str(files["python"]), "-m", "pseudolife_memory.cli"],
                "candidate": [str(files["native"])]}
    selected = cli_version.make_prepare(tmp_path, "f" * 40)(
        case, home, env, commands["candidate"], commands)
    scripts = Path(selected[0]).parent
    assert scripts.name == ("Scripts" if platform == "nt" else "bin")
    assert Path(selected[0]).read_bytes() == b"native"
    assert (scripts / ("python.exe" if platform == "nt" else "python")).read_bytes() == b"python"
    assert not (scripts / ("pseudolife-mcp.exe" if platform == "nt" else "pseudolife-mcp")).exists()
    marker = json.loads((scripts.parent / "runtime.json").read_text())
    assert marker["source_commit"] == "f" * 40
    assert set(marker) == {"version", "installed_at", "source", "source_commit", "base_interpreter"}
    assert case["expected_stdout"] == "pseudolife-mcp {version}\n"


@pytest.mark.parametrize("response", [
    {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""},
    {"exit_code": 1, "stdout_b64": "", "stderr_b64": ""},
    {"exit_code": 0, "stdout_b64": "", "stderr_b64": "YQ=="},
])
def test_missing_console_expected_fallback_gate_rejects_wrong_raw_response(tmp_path, monkeypatch, response):
    from evals.rust_port import cli_process
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.17.0"\n')
    executable = tmp_path / "native"
    executable.write_bytes(b"synthetic image")
    commands = {"oracle": [str(executable)], "candidate": [str(executable)]}
    case = next(row for row in cli_version.cases() if row["id"] == "version-missing-console")
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: response)
    with pytest.raises(RuntimeError, match="exact expected fallback"):
        cli_process.observe(case, commands["candidate"], commands, root=tmp_path,
                            home=tmp_path / "home", url="http://127.0.0.1:1")


def test_non_ascii_home_is_the_actual_owned_home_with_utf8_output_policy(tmp_path, monkeypatch):
    from evals.rust_port import cli_process
    executable = tmp_path / "native"
    executable.write_bytes(b"synthetic image")
    commands = {"oracle": [str(executable)], "candidate": [str(executable)]}
    case = next(row for row in cli_version.cases() if row["id"] == "version-non-ascii-home")
    monkeypatch.setattr(cli_process, "run_cli", lambda *a, **k: {
        "exit_code": 0, "stdout_b64": "", "stderr_b64": ""})
    observed = cli_process.observe(case, commands["candidate"], commands, root=tmp_path,
                                   home=tmp_path / "home", url="http://127.0.0.1:1")
    assert observed["environment"]["HOME"] == str(tmp_path / "home" / "home-café-☃")
    assert observed["environment"]["USERPROFILE"] == observed["environment"]["HOME"]
    assert observed["environment"]["PYTHONIOENCODING"] == "utf-8"
    assert case["normalizations"] == []
