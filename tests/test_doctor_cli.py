"""Doctor failure reports must retain safe, actionable recovery advice."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pseudolife_memory import doctor_cli, shim


# --- Git Bash on Windows -----------------------------------------------------
#
# Claude Code runs the plugin's Bash hook commands through Git Bash on
# Windows, found by its own rules, not by `bash` on PATH: CLAUDE_CODE_GIT_BASH_PATH,
# then the default Git for Windows install directories, then the `git` on
# PATH's own bin\bash.exe (code.claude.com/docs/en/troubleshoot-install.md;
# verified on 2.1.280 on 2026-09-27 with every Git directory stripped from
# PATH). Without one, hooks fall back to PowerShell, where every plugin hook
# command fails, so doctor fails loudly. `bash` on PATH being the WSL launcher
# (System32 or the Store alias) does not affect Claude Code and is a warning.

def _git_layout(tmp_path):
    for name in ("cmd/git.exe", "bin/bash.exe", "mingw64/bin/git.exe"):
        path = tmp_path / "Git" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return tmp_path / "Git"


def test_git_bash_is_found_the_way_claude_code_finds_it(tmp_path):
    git = _git_layout(tmp_path)
    defaults = (str(tmp_path / "absent/Git/bin/bash.exe"),)
    # 1. CLAUDE_CODE_GIT_BASH_PATH, when it names an existing bash/sh binary.
    env = {"CLAUDE_CODE_GIT_BASH_PATH": str(git / "bin/bash.exe")}
    assert doctor_cli.find_git_bash(env, defaults=defaults, which=lambda _: None) == str(git / "bin/bash.exe")
    for bad in (str(git / "git-bash.exe"), str(tmp_path / "missing/bash.exe")):
        assert doctor_cli.find_git_bash({"CLAUDE_CODE_GIT_BASH_PATH": bad},
                                        defaults=defaults, which=lambda _: None) is None
    # 2. The default install directories.
    assert doctor_cli.find_git_bash({}, defaults=(str(git / "bin/bash.exe"),),
                                    which=lambda _: None) == str(git / "bin/bash.exe")
    # 3. bin\bash.exe two directories up from the git on PATH: Claude Code
    # joins the git path with `..`, `..`, `bin`, `bash.exe`, so Git's cmd\
    # (the directory the installer puts on PATH) resolves and mingw64\bin\
    # does not.
    found = doctor_cli.find_git_bash({}, defaults=defaults,
                                     which=lambda name: str(git / "cmd/git.exe") if name == "git" else None)
    assert Path(found) == git / "bin/bash.exe"
    assert doctor_cli.find_git_bash({}, defaults=defaults,
                                    which=lambda name: str(git / "mingw64/bin/git.exe") if name == "git" else None) is None
    assert doctor_cli.find_git_bash({}, defaults=defaults, which=lambda _: None) is None


def test_git_bash_report_flags_the_wsl_launcher_on_path(tmp_path):
    git = _git_layout(tmp_path)
    system32 = tmp_path / "Windows/System32/bash.exe"
    system32.parent.mkdir(parents=True)
    system32.write_bytes(b"")
    which = {"git": str(git / "cmd/git.exe"), "bash": str(system32)}.get
    report = doctor_cli.git_bash_report({}, defaults=(), which=which)
    assert Path(report["git_bash"]) == git / "bin/bash.exe"
    assert report["bash_on_path"] == str(system32)
    assert report["bash_on_path_is_wsl_launcher"] is True
    assert "Claude Code" in report["git_bash_recovery"] and "PATH" in report["git_bash_recovery"]
    clean = doctor_cli.git_bash_report({}, defaults=(), which={"git": str(git / "cmd/git.exe"),
                                                                "bash": str(git / "bin/bash.exe")}.get)
    assert clean["bash_on_path_is_wsl_launcher"] is False and "git_bash_recovery" not in clean


def test_doctor_fails_loudly_without_git_bash_on_windows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: {"status": "ok", "version": "1"})
    monkeypatch.setattr(doctor_cli, "_handshake", AsyncMock(return_value={
        "instructions_present": True, "tool_count": 3, "tools_missing_annotations": []}))
    monkeypatch.setattr(doctor_cli, "_windows", lambda: True)
    monkeypatch.setattr(doctor_cli, "git_bash_report", lambda env: {
        "git_bash": None, "bash_on_path": None, "bash_on_path_is_wsl_launcher": False,
        "git_bash_recovery": "Install Git for Windows."})
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    report = json.loads(capsys.readouterr().out)
    assert exit_info.value.code == 1
    assert report["ok"] is False and report["error"] == "GitBashMissing"
    assert report["git_bash"] is None and "Git for Windows" in report["recovery"]


def test_doctor_reports_git_bash_only_on_windows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: None)
    monkeypatch.setattr(doctor_cli, "_windows", lambda: False)
    with pytest.raises(SystemExit):
        doctor_cli.run_doctor()
    assert "git_bash" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("failure,hint", [
    ("unreachable", "Start the intended daemon"),
    ("timeout", "--timeout"),
    ("transport", "exact registered interpreter"),
])
def test_doctor_reports_specific_safe_recovery(monkeypatch, capsys, failure, hint):
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw:
                        None if failure == "unreachable" else {"status": "ok"})
    handshake = AsyncMock(side_effect=TimeoutError() if failure == "timeout"
                          else RuntimeError("fixture-secret-must-not-leak"))
    monkeypatch.setattr(doctor_cli, "_handshake", handshake)
    with pytest.raises(SystemExit) as exit_info:
        doctor_cli.run_doctor()
    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["ok"] is False
    assert hint in report["recovery"]
    assert "fixture-secret-must-not-leak" not in output
    if failure == "unreachable":
        handshake.assert_not_called()
