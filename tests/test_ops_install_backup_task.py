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
* ``-ScriptCheckout`` runs another checkout's copy instead (the main one
  can lag master for days): a worktree only once it is locked, and never
  a checkout that would receive the dumps itself. Dumps and the log stay
  in the main checkout's ``data/backups``; each run logs the script
  checkout's HEAD;
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
import re
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


def _flat(text: str) -> str:
    """pwsh's error view colors a message and wraps it at the console width
    behind a '|' gutter; flatten it so a phrase can be matched."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return " ".join(text.replace("|", " ").split())


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

def _fake_docker(tmp_path: Path, rc: int) -> tuple[dict, Path]:
    """A ``docker`` first on PATH that logs its arguments and exits ``rc``,
    so the task's readiness probe never reaches a real Docker."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    calls = tmp_path / "docker-calls.log"
    (fakebin / "docker.cmd").write_text(
        f'@echo %* >> "{calls}"\r\n@exit /b {rc}\r\n', encoding="ascii")
    sh = fakebin / "docker"
    sh.write_text(f'#!/bin/sh\necho "$@" >> "{calls.as_posix()}"\nexit {rc}\n',
                  encoding="ascii", newline="\n")
    sh.chmod(0o755)
    return {"PATH": str(fakebin) + os.pathsep + os.environ.get("PATH", "")}, calls


def _execute(registered: dict, env: dict | None = None):
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-EncodedCommand",
         _encoded(registered)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, **(env or {})})


@pytest.mark.parametrize("body, rc, expect", [
    ("Write-Output 'backup ran'", 0, "backup ran"),
    ("throw 'pg_dump failed inside container'", 1, "FAILED"),
])
def test_every_run_is_logged_and_a_failure_exits_non_zero(tmp_path, body, rc, expect):
    """Run the exact -EncodedCommand the task would, with Task Scheduler's
    view of success: the process exit code."""
    plain = _checkout(tmp_path / "plain", backup_body=body)
    proc, registered, _ = _run(tmp_path, plain / "ops" / SCRIPT.name,
                               "-DockerWaitSeconds", "0",
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    run = _execute(registered)
    assert run.returncode == rc, (run.stdout, run.stderr)
    log = plain / "data" / "backups" / "backup-task.log"
    assert log.exists(), "the run left no log"
    text = log.read_text(encoding="utf-8-sig")
    assert "scheduled backup" in text and expect in text, text
    if rc:
        assert "pg_dump failed inside container" in text, text


def test_a_catch_up_run_waits_for_docker_first(tmp_path):
    """StartWhenAvailable fires at the next logon, when Docker Desktop is
    often still starting: probe Postgres before the backup, not after it
    has already failed."""
    plain = _checkout(tmp_path / "plain")
    proc, registered, _ = _run(tmp_path, plain / "ops" / SCRIPT.name,
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    env, calls = _fake_docker(tmp_path, rc=0)
    run = _execute(registered, env)
    assert run.returncode == 0, (run.stdout, run.stderr)
    probes = calls.read_text(encoding="utf-8", errors="replace")
    assert "exec pseudolife-mcp-postgres pg_isready" in probes, probes
    text = (plain / "data" / "backups" / "backup-task.log").read_text(encoding="utf-8-sig")
    assert "backup ran" in text and "not ready" not in text, text


def test_a_docker_that_never_comes_up_still_gets_a_logged_attempt(tmp_path):
    plain = _checkout(tmp_path / "plain")
    proc, registered, _ = _run(tmp_path, plain / "ops" / SCRIPT.name,
                               "-DockerWaitSeconds", "1",
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    env, _ = _fake_docker(tmp_path, rc=1)
    run = _execute(registered, env)
    assert run.returncode == 0, (run.stdout, run.stderr)   # the stand-in succeeds
    text = (plain / "data" / "backups" / "backup-task.log").read_text(encoding="utf-8-sig")
    assert "not ready after 1s" in text and "backup ran" in text, text


# ----------------------------------------------------------------------
# -ScriptCheckout: the backup script from a dedicated checkout
# ----------------------------------------------------------------------

def _main_with_worktree(tmp_path: Path, *, lock: bool) -> tuple[Path, Path]:
    main = _checkout(tmp_path / "main",
                     backup_body='param([string]$OutDir)\nWrite-Output "main copy into $OutDir"')
    _git("init", "-q", "-b", "master", cwd=main)
    _git("add", ".", cwd=main)
    _git("commit", "-q", "-m", "init", cwd=main)
    ops = tmp_path / "ops"
    _git("worktree", "add", "-q", "--detach", str(ops), cwd=main)
    if lock:
        _git("worktree", "lock", "--reason", "daily backup task", str(ops), cwd=main)
    return main, ops


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_a_script_checkout_runs_its_own_backup_into_the_main_data_folder(tmp_path):
    """The main checkout can lag master for days while it holds someone's
    uncommitted work: on 2026-09-24 and 09-25 the task ran a backup.ps1
    from before the row-count gate. -ScriptCheckout runs another checkout's
    copy, but dumps and the log stay in the main checkout's data/backups,
    where restore and the replica push look for them."""
    main, ops = _main_with_worktree(tmp_path, lock=True)
    (ops / "ops" / "backup.ps1").write_text(
        'param([string]$OutDir)\nWrite-Output "ops copy into $OutDir"\n', encoding="utf-8")
    proc, registered, _ = _run(tmp_path, ops / "ops" / SCRIPT.name,
                               "-ScriptCheckout", f"'{ops}'", "-DockerWaitSeconds", "0",
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert registered is not None, "Register-ScheduledTask was never called"
    run = _execute(registered)
    assert run.returncode == 0, (run.stdout, run.stderr)
    text = (main / "data" / "backups" / "backup-task.log").read_text(encoding="utf-8-sig")
    assert "ops copy into" in text and "main copy" not in text, text
    out_dir = text.split("ops copy into ", 1)[1].splitlines()[0].strip()
    assert _norm(out_dir) == _norm(main / "data" / "backups"), text
    assert not (ops / "data").exists(), "the run wrote into the script checkout"
    # A script checkout that falls behind must show in the log: each run
    # names the commit it ran.
    head = subprocess.run([GIT, "-C", str(ops), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert head and head in text, text


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_an_unlocked_worktree_is_refused_as_the_script_checkout(tmp_path):
    """A worktree's copy disappears with the worktree, which is why the
    default is the main checkout. `git worktree lock` is what keeps
    `git worktree prune` and `remove` away from it."""
    _, ops = _main_with_worktree(tmp_path, lock=False)
    # The driver's `& script` swallows the installer's exit code, so the
    # contract is read off what it did: nothing registered, and why.
    proc, registered, _ = _run(tmp_path, ops / "ops" / SCRIPT.name,
                               "-ScriptCheckout", f"'{ops}'",
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert registered is None, "an unlocked worktree was registered"
    assert "git worktree lock" in _flat(proc.stderr), proc.stderr


def test_a_misspelled_parameter_fails_instead_of_installing_the_default(tmp_path):
    """Without [CmdletBinding()] an unknown parameter lands in $args and the
    run registers the main checkout: a typo in -ScriptCheckout would quietly
    reinstall the stale copy it was meant to replace."""
    proc, registered, _ = _run(tmp_path, SCRIPT, "-ScriptChekout", "'x'")
    assert registered is None, "a misspelled parameter still registered a task"
    assert "ScriptChekout" in proc.stderr, proc.stderr


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_a_separate_clone_needs_no_lock(tmp_path):
    """A clone's git dir is its own common dir: nothing prunes it."""
    main = _checkout(tmp_path / "main")
    _git("init", "-q", "-b", "master", cwd=main)
    _git("add", ".", cwd=main)
    _git("commit", "-q", "-m", "init", cwd=main)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(main), str(clone), cwd=tmp_path)
    proc, registered, _ = _run(tmp_path, main / "ops" / SCRIPT.name,
                               "-ScriptCheckout", f"'{clone}'",
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert registered is not None, proc.stderr
    inner = _norm(_inner(registered))
    assert _norm(clone / "ops" / "backup.ps1") in inner, inner
    assert _norm(main / "data" / "backups" / "backup-task.log") in inner, inner


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_a_script_checkout_that_is_also_the_data_home_is_refused(tmp_path):
    """The data home is the checkout the installer itself resolves. A
    separate clone's own installer resolves the clone, so
    `<clone>\\ops\\install-backup-task.ps1 -ScriptCheckout <clone>` would put
    the dumps in <clone>/data/backups, where restore and the replica push
    never look - with nothing to say so."""
    clone = _checkout(tmp_path / "clone")
    _git("init", "-q", "-b", "master", cwd=clone)
    proc, registered, _ = _run(tmp_path, clone / "ops" / SCRIPT.name,
                               "-ScriptCheckout", f"'{clone}'",
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert registered is None, "the dumps were routed into the script checkout"
    assert "worktree of the main checkout" in _flat(proc.stderr), proc.stderr


# ----------------------------------------------------------------------
# A main checkout that predates the gate
# ----------------------------------------------------------------------

def test_installing_against_a_pre_gate_backup_warns(tmp_path):
    """The task runs whatever the main checkout holds. Before that checkout
    is updated, its backup.ps1 has no row-count gate and writes no
    last_backup record: still a daily backup, so warn rather than refuse,
    but loudly."""
    plain = _checkout(tmp_path / "plain")
    proc, registered, _ = _run(tmp_path, plain / "ops" / SCRIPT.name,
                               env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert registered is not None
    assert "predates the row-count gate" in proc.stdout + proc.stderr, proc.stdout


def test_installing_against_a_gated_backup_does_not_warn(tmp_path):
    plain = _checkout(tmp_path / "plain",
                      backup_body="param([switch]$AcceptRowDrop)\nWrite-Output 'ok'")
    proc, _, _ = _run(tmp_path, plain / "ops" / SCRIPT.name,
                      env={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert "predates" not in proc.stdout + proc.stderr, proc.stdout


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
