"""Installer contract for ``ops/install-worktree-retention.ps1``.

Same harness as ``test_ops_install_cache_retention.py``: drive the real
script under pwsh with the ScheduledTasks cmdlets stubbed as global
functions, so the registration is pinned without touching the host's task
store. The script is copied into a temp git checkout, because it refuses to
register from a linked worktree (which the task itself would later remove).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not on PATH")

_STUBS = """
function global:Get-ScheduledTask {
    param([string]$TaskName, [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    if ($env:INSTALL_TEST_EXISTING) {
        return [pscustomobject]@{ TaskName = $TaskName; Description = $env:INSTALL_TEST_EXISTING }
    }
    return $null
}
function global:Unregister-ScheduledTask {
    param([string]$TaskName, [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    'unregistered' | Set-Content -Path $env:INSTALL_TEST_LOG -Encoding utf8
}
function global:New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [pscustomobject]@{ Execute = $Execute; Argument = $Argument }
}
function global:New-ScheduledTaskTrigger {
    param([switch]$Weekly, $DaysOfWeek, $At,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [pscustomobject]@{ DaysOfWeek = "$DaysOfWeek"; At = "$At" }
}
function global:New-ScheduledTaskSettingsSet {
    param([Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [pscustomobject]@{}
}
function global:Register-ScheduledTask {
    param([string]$TaskName, $Action, $Trigger, $Settings, [switch]$Force,
          [string]$Description,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    @{ TaskName = $TaskName; Execute = $Action.Execute; Argument = $Action.Argument;
       Description = $Description; Days = $Trigger.DaysOfWeek; At = $Trigger.At } |
        ConvertTo-Json | Set-Content -Path $env:INSTALL_TEST_LOG -Encoding utf8
    [pscustomobject]@{ TaskName = $TaskName }
}
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    """A main checkout holding copies of the installer and the script."""
    repo = tmp_path / "checkout"
    (repo / "ops").mkdir(parents=True)
    _git(repo, "init", "-q")
    for name in ("install-worktree-retention.ps1", "prune_worktrees.py"):
        shutil.copy2(REPO / "ops" / name, repo / "ops" / name)
    return repo


def _run(tmp_path: Path, checkout: Path, *args: str, existing: str = ""):
    log = tmp_path / "registered.json"
    log.unlink(missing_ok=True)
    driver = tmp_path / "driver.ps1"
    script = checkout / "ops" / "install-worktree-retention.ps1"
    driver.write_text(f"{_STUBS}\n& '{script}' {' '.join(args)}\nexit $LASTEXITCODE\n",
                      encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "INSTALL_TEST_LOG": str(log), "INSTALL_TEST_EXISTING": existing,
           "USERPROFILE": str(home), "HOME": str(home)}
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(driver)],
                          capture_output=True, text=True, timeout=120, env=env)
    text = log.read_text(encoding="utf-8-sig") if log.exists() else ""
    registered = json.loads(text) if text.strip().startswith("{") else text.strip() or None
    return proc, registered


def test_registers_absolute_pwsh_running_the_script_with_apply(tmp_path, checkout):
    proc, registered = _run(tmp_path, checkout, "-Python", f"'{sys.executable}'")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert isinstance(registered, dict), proc.stdout + proc.stderr
    assert registered["TaskName"] == "Pseudolife-MCP worktree retention"
    assert Path(registered["Execute"]).is_absolute()
    assert Path(registered["Execute"]).exists()
    encoded = registered["Argument"].split("-EncodedCommand ", 1)[1].split()[0]
    inner = base64.b64decode(encoded).decode("utf-16-le")
    assert str(checkout / "ops" / "prune_worktrees.py") in inner
    assert "--apply" in inner
    assert "--mine" not in inner
    assert os.path.normcase(sys.executable) in os.path.normcase(inner)
    assert "No roots configured yet" in proc.stdout


def test_rerun_with_nothing_changed_is_quiet(tmp_path, checkout):
    first, registered = _run(tmp_path, checkout, "-Python", f"'{sys.executable}'")
    assert first.returncode == 0 and isinstance(registered, dict)
    again, second = _run(tmp_path, checkout, "-Python", f"'{sys.executable}'",
                         existing=registered["Description"])
    assert again.returncode == 0, again.stdout + again.stderr
    assert second is None, "a rerun with nothing changed must not re-register"
    assert "nothing changed" in again.stdout
    assert len(again.stdout.strip().splitlines()) == 1


def test_changed_schedule_re_registers(tmp_path, checkout):
    _, registered = _run(tmp_path, checkout, "-Python", f"'{sys.executable}'")
    proc, second = _run(tmp_path, checkout, "-Python", f"'{sys.executable}'",
                        "-DayOfWeek", "Wednesday", existing=registered["Description"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert isinstance(second, dict) and second["Days"] == "Wednesday"


def test_refuses_to_register_from_a_linked_worktree(tmp_path, checkout):
    (checkout / "f.txt").write_text("x", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-qm", "init")
    linked = tmp_path / "linked"
    _git(checkout, "worktree", "add", "-q", str(linked))
    proc, registered = _run(tmp_path, linked, "-Python", f"'{sys.executable}'")
    assert proc.returncode != 0
    assert registered is None
    assert "linked git worktree" in proc.stdout
    assert "Nothing was registered" in proc.stdout


def test_refuses_without_a_usable_python(tmp_path, checkout):
    proc, registered = _run(tmp_path, checkout, "-Python", f"'{tmp_path / 'nope.exe'}'")
    assert proc.returncode != 0
    assert registered is None
    assert "-Python" in proc.stdout


def test_unregister(tmp_path, checkout):
    proc, registered = _run(tmp_path, checkout, "-Unregister", existing="x")
    assert proc.returncode == 0, proc.stderr
    assert registered == "unregistered"
    proc, registered = _run(tmp_path, checkout, "-Unregister")
    assert proc.returncode == 0
    assert registered is None
    assert "already not registered" in proc.stdout
