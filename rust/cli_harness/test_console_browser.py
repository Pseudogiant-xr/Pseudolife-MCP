"""Console cases isolate every browser path, including a wrong sign-in."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cli_harness.rows import console  # noqa: E402


def test_every_console_case_keeps_program_lookup_in_its_home():
    for case in console.cases():
        assert case.env.get("BROWSER"), case.id
        assert case.env.get("PATH") == console.BIN, case.id


def test_browser_guard_rejects_real_program_even_without_a_daemon():
    with pytest.raises(AssertionError, match="stand-in"):
        console.case("wrong-signin", ["--list"], env={
            "BROWSER": "/usr/bin/firefox %s &", "PATH": console.BIN,
        })


@pytest.mark.parametrize("value", [
    r"{HOME}\..\outside.exe",
    r"'{HOME}\bin\..\outside.exe' %s &",
    "windows-default",
])
def test_windows_browser_guard_rejects_escape_and_default(monkeypatch, value):
    monkeypatch.setattr(console.core, "WINDOWS", True)
    monkeypatch.setattr(console, "BIN", r"{HOME}\bin")
    monkeypatch.setattr(console, "SEP", "\\")
    with pytest.raises(AssertionError, match="stand-in"):
        console._check_browser_env("escape", {"BROWSER": value, "PATH": console.BIN})


def test_windows_browser_guard_preserves_unquoted_backslashes(monkeypatch):
    monkeypatch.setattr(console.core, "WINDOWS", True)
    monkeypatch.setattr(console, "BIN", r"{HOME}\bin")
    monkeypatch.setattr(console, "SEP", "\\")
    console._check_browser_env("safe", {
        "BROWSER": r"{HOME}\bin\standin.exe %s &", "PATH": console.BIN,
    })


def test_bare_browser_case_really_names_a_bare_program():
    case = next(case for case in console.cases() if case.id == "defer-browser-bare-name")
    assert "{HOME}" not in case.env["BROWSER"]
    assert "%s" in case.env["BROWSER"]
    assert not any(char in case.env["BROWSER"].split()[0] for char in "/\\:")


def test_console_goldens_with_standin_browsers(tmp_path):
    """The hosted stress entry: replay all Console cases, never a real browser."""
    binary = os.environ.get("PSEUDOLIFE_RUST_BINARY")
    if not binary:
        pytest.skip("set PSEUDOLIFE_RUST_BINARY to exercise the native command")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "rust" / "cli_harness"), "--row", "console",
         "--golden", "--candidate", binary, "--out", str(tmp_path / "replay.json")],
        cwd=root, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for value in ["0", "640", "4300", "invalid"]:
        env = {key: str(tmp_path) for key in ["HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"]}
        env.update(BROWSER=console.NO_BROWSER.replace("{HOME}", str(tmp_path)),
                   PATH=str(tmp_path), COLUMNS="80", PYTHONINTMAXSTRDIGITS=value,
                   PSEUDOLIFE_MCP_DAEMON_URL="http://127.0.0.1:1")
        if "SystemRoot" in os.environ:
            env["SystemRoot"] = os.environ["SystemRoot"]
        result = subprocess.run([binary, "console", "--help"], env=env,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout
