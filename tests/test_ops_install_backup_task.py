"""Installer contract for ``ops/install-backup-task.ps1``.

Why this exists: nothing backed the bank up on a schedule. Deploys dump
first (``ops/update.ps1``), and the private replica push dumped nightly
until 2026-09-13, when it began failing before its dump. From 2026-09-14
13:28 to 09-20 12:18 no dump existed anywhere, and nothing said so
(2026-09-23 fresh-eyes review). This installer registers a daily
``ops/backup.ps1`` run that does not depend on either.

What is pinned, each for a reason:

* the task runs the MAIN checkout's ``backup.ps1``, even when installed
  from a worktree: a worktree's copy disappears with the worktree, and its
  ``data/backups`` is a folder nothing else reads (15 of the 19 deploy
  dumps from 09-11..09-22 ended up in such folders);
* ``StartWhenAvailable``: a desktop is often off at 03:00, and the replica
  task's missed runs on 09-18/19 vanished without a trace because it lacks
  exactly this;
* an interactive run as the installing user: Docker Desktop only runs in a
  logged-on session, and the mirror folder (``PSEUDOLIFE_BACKUP_MIRROR``)
  is a User-scope variable;
* an absolute ``pwsh`` path (see ``test_ops_install_cache_retention.py``
  for the 0x80070002 failure a bare name causes);
* every run appends to ``data/backups/backup-task.log`` and a failed run
  exits non-zero, so the task's Last Result and the log both show it.

Same harness as the cache-retention installer: the REAL script under pwsh
with the ScheduledTasks cmdlets stubbed as global functions, so nothing
touches the host's task store.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "install-backup-task.ps1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not on PATH")

# Deliberately PERMISSIVE stubs (extra parameters bind into $Rest), so a new
# switch on the real call site cannot silently break the stub instead of the
# assertion. Register logs everything the task would be created with.
_STUBS = """
function global:New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [pscustomobject]@{ Execute = $Execute; Argument = $Argument }
}
function global:New-ScheduledTaskTrigger {
    param([switch]$Daily, [switch]$Weekly, $At,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [pscustomobject]@{ Daily = [bool]$Daily; Weekly = [bool]$Weekly; At = "$At" }
}
function global:New-ScheduledTaskSettingsSet {
    param([switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries,
          [switch]$StartWhenAvailable, $ExecutionTimeLimit,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [pscustomobject]@{
        AllowStartIfOnBatteries = [bool]$AllowStartIfOnBatteries
        DontStopIfGoingOnBatteries = [bool]$DontStopIfGoingOnBatteries
        StartWhenAvailable = [bool]$StartWhenAvailable
        ExecutionTimeLimit = "$ExecutionTimeLimit" }
}
function global:New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [pscustomobject]@{ UserId = "$UserId"; LogonType = "$LogonType"; RunLevel = "$RunLevel" }
}
function global:Register-ScheduledTask {
    param([string]$TaskName, $Action, $Trigger, $Settings, $Principal,
          [switch]$Force, [string]$Description,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    @{ TaskName = $TaskName; Execute = $Action.Execute; Argument = $Action.Argument;
       Trigger = $Trigger; Settings = $Settings; Principal = $Principal;
       Force = [bool]$Force } | ConvertTo-Json -Depth 4 |
        Set-Content -Path $env:INSTALL_TEST_LOG -Encoding utf8
    [pscustomobject]@{ TaskName = $TaskName }
}
function global:Get-ScheduledTask {
    param($TaskName, [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    if ($env:FAKE_TASK_EXISTS) { [pscustomobject]@{ TaskName = $TaskName } }
}
function global:Unregister-ScheduledTask {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param($TaskName)
    Set-Content -Path $env:INSTALL_TEST_UNREG -Value $TaskName -Encoding utf8
}
"""


def _run(tmp_path: Path, script: Path = SCRIPT, *args: str,
         env: dict | None = None):
    log = tmp_path / "registered.json"
    unreg = tmp_path / "unregistered.txt"
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        f"$env:INSTALL_TEST_LOG = '{log}'\n$env:INSTALL_TEST_UNREG = '{unreg}'\n"
        f"{_STUBS}\n& \"{script}\" {' '.join(args)}\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, **(env or {})},
    )
    registered = (json.loads(log.read_text(encoding="utf-8-sig"))
                  if log.exists() and log.stat().st_size else None)
    unregistered = (unreg.read_text(encoding="utf-8-sig").strip()
                    if unreg.exists() else None)
    return proc, registered, unregistered


def _encoded(registered: dict) -> str:
    argument = registered["Argument"]
    assert "-EncodedCommand " in argument, argument
    return argument.split("-EncodedCommand ", 1)[1].split()[0]


def _inner(registered: dict) -> str:
    return base64.b64decode(_encoded(registered)).decode("utf-16-le")


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _checkout(root: Path, backup_body: str = "Write-Output 'backup ran'") -> Path:
    """A minimal checkout: the real installer plus a stand-in backup.ps1."""
    (root / "ops").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, root / "ops" / SCRIPT.name)
    (root / "ops" / "backup.ps1").write_text(backup_body + "\n", encoding="utf-8")
    return root


def _git(*args: str, cwd: Path) -> None:
    # A throwaway repo under tmp_path: its own identity, and no dependency
    # on the machine's signing setup.
    subprocess.run(
        [GIT, "-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True)


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_a_worktree_install_registers_the_main_checkouts_backup(tmp_path):
    """THE guard. Installed from a worktree, the task must still run the
    main checkout's backup.ps1 and log into its data/backups."""
    main = _checkout(tmp_path / "main")
    _git("init", "-q", "-b", "master", cwd=main)
    _git("add", ".", cwd=main)
    _git("commit", "-q", "-m", "init", cwd=main)
    _git("worktree", "add", "-q", "-b", "wt", str(tmp_path / "wt"), cwd=main)
    worktree_script = tmp_path / "wt" / "ops" / SCRIPT.name
    assert worktree_script.exists()

    proc, registered, _ = _run(tmp_path, worktree_script,
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert registered is not None, "Register-ScheduledTask was never called"
    inner = _norm(_inner(registered))
    assert _norm(main / "ops" / "backup.ps1") in inner, inner
    assert _norm(main / "data" / "backups" / "backup-task.log") in inner, inner
    assert _norm(tmp_path / "wt") not in inner, (
        "the task points into a worktree that can be deleted: " + inner)


def test_outside_a_git_checkout_it_uses_its_own(tmp_path):
    plain = _checkout(tmp_path / "plain")
    proc, registered, _ = _run(tmp_path, plain / "ops" / SCRIPT.name,
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert _norm(plain / "ops" / "backup.ps1") in _norm(_inner(registered))


def test_the_task_catches_up_after_a_powered_off_night(tmp_path):
    proc, registered, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    settings = registered["Settings"]
    assert settings["StartWhenAvailable"] is True, settings
    assert settings["AllowStartIfOnBatteries"] is True, settings
    assert settings["DontStopIfGoingOnBatteries"] is True, settings
    assert settings["ExecutionTimeLimit"] == "01:00:00", settings
    assert registered["Force"] is True, "re-running must replace, not fail"


def test_it_runs_daily_at_the_chosen_time(tmp_path):
    proc, registered, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert registered["Trigger"]["Daily"] is True
    assert registered["Trigger"]["At"] == "03:00"
    proc, registered, _ = _run(tmp_path, SCRIPT, "-At", "02:15")
    assert proc.returncode == 0, proc.stderr
    assert registered["Trigger"]["At"] == "02:15"


def test_it_runs_as_the_logged_on_user(tmp_path):
    proc, registered, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    principal = registered["Principal"]
    assert principal["LogonType"] == "Interactive", principal
    assert principal["RunLevel"] == "Limited", principal
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    assert principal["UserId"].lower().endswith(user.lower()), principal


def test_the_registered_pwsh_is_an_absolute_existing_path(tmp_path):
    proc, registered, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    execute = registered["Execute"]
    assert Path(execute).is_absolute(), execute
    assert Path(execute).exists(), execute


# ----------------------------------------------------------------------
# The registered command itself, executed against stand-in backups
# ----------------------------------------------------------------------

@pytest.mark.parametrize("body, rc, expect", [
    ("Write-Output 'backup ran'", 0, "backup ran"),
    ("throw 'pg_dump failed inside container'", 1, "FAILED"),
])
def test_every_run_is_logged_and_a_failure_exits_non_zero(tmp_path, body, rc, expect):
    """Run the exact -EncodedCommand the task would, with Task Scheduler's
    view of success: the process exit code."""
    plain = _checkout(tmp_path / "plain", backup_body=body)
    proc, registered, _ = _run(tmp_path, plain / "ops" / SCRIPT.name,
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-EncodedCommand",
         _encoded(registered)],
        capture_output=True, text=True, timeout=120)
    assert run.returncode == rc, (run.stdout, run.stderr)
    log = plain / "data" / "backups" / "backup-task.log"
    assert log.exists(), "the run left no log"
    text = log.read_text(encoding="utf-8-sig")
    assert "scheduled backup" in text and expect in text, text
    if rc:
        assert "pg_dump failed inside container" in text, text


# ----------------------------------------------------------------------
# -Uninstall
# ----------------------------------------------------------------------

def test_uninstall_removes_the_task(tmp_path):
    proc, registered, unregistered = _run(tmp_path, SCRIPT, "-Uninstall",
                                          env={"FAKE_TASK_EXISTS": "1"})
    assert proc.returncode == 0, proc.stderr
    assert registered is None, "-Uninstall must not register anything"
    assert unregistered == "Pseudolife-MCP daily backup", unregistered


def test_uninstall_without_a_task_is_a_no_op(tmp_path):
    proc, registered, unregistered = _run(tmp_path, SCRIPT, "-Uninstall")
    assert proc.returncode == 0, proc.stderr
    assert registered is None and unregistered is None
    assert "not registered" in proc.stdout, proc.stdout
