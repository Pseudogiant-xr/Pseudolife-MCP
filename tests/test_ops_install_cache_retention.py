"""Installer contract for ``ops/install-cache-retention.ps1``.

Why this exists: the weekly retention task this script registers ran with
``Execute = "pwsh.exe"`` — a bare name Task Scheduler resolves against the
MACHINE path, which a Store/MSIX install of PowerShell 7 is absent from. On
such a host every scheduled run died with 0x80070002 (file not found) before
the retention script ever started, while the task itself still reported
Ready and its trigger kept firing — a silent permanent failure, observed
live 2026-08-20 with the build cache at 23.6GB against the 20GB ceiling.

Same harness as ``test_ops_prune_build_cache.py``: drive the REAL script
under pwsh with the ScheduledTasks cmdlets stubbed as global functions
(functions shadow cmdlets in PowerShell command resolution), so the exact
registration contract is pinned without touching the host's task store.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "install-cache-retention.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not on PATH")

# Stubs for every ScheduledTasks cmdlet the installer touches. Register logs
# the action it received; the rest just echo enough structure back for the
# script to thread through. Deliberately PERMISSIVE (extra parameters bind
# into $args via ValueFromRemainingArguments) so a new switch on the real
# call site cannot silently break the stub instead of the assertion.
_STUBS = """
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
    param([switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries,
          [switch]$StartWhenAvailable, $ExecutionTimeLimit,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [pscustomobject]@{}
}
function global:Register-ScheduledTask {
    param([string]$TaskName, $Action, $Trigger, $Settings, [switch]$Force,
          [string]$Description,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    @{ TaskName = $TaskName; Execute = $Action.Execute;
       Argument = $Action.Argument } | ConvertTo-Json |
        Set-Content -Path $env:INSTALL_TEST_LOG -Encoding utf8
    [pscustomobject]@{ TaskName = $TaskName }
}
"""


def _merge_env(env: dict | None) -> dict:
    """The child's environment with ``env`` laid over it. Windows names are
    case-insensitive and ``os.environ`` holds them upper-cased, so an
    override spelled ``ProgramFiles`` must replace ``PROGRAMFILES``, not sit
    beside it (the child would see whichever comes first)."""
    import os
    merged = dict(os.environ)
    for key, value in (env or {}).items():
        if sys.platform == "win32":
            for existing in [k for k in merged if k.upper() == key.upper()]:
                del merged[existing]
        merged[key] = value
    return merged


def _run(tmp_path: Path, extra_stubs: str = "", env: dict | None = None,
         *args: str, script: Path = SCRIPT):
    log = tmp_path / "registered.json"
    driver = tmp_path / "driver.ps1"
    env = dict(env or {})
    # Windows sets %ProgramFiles% afresh in every new process, whatever the
    # parent passed (measured 16 November 2026 by this change's review), so
    # it is set inside the driver, which runs the installer in-process.
    in_process = "".join(
        f"$env:ProgramFiles = '{env.pop(key)}'\n"
        for key in [k for k in env if k.upper() == "PROGRAMFILES"])
    driver.write_text(
        f"$env:INSTALL_TEST_LOG = '{log}'\n{in_process}{_STUBS}\n{extra_stubs}\n"
        f'& "{script}" {" ".join(args)}\n'
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    merged = _merge_env(env)
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=120, env=merged,
    )
    registered = (json.loads(log.read_text(encoding="utf-8-sig"))
                  if log.exists() and log.stat().st_size else None)
    return proc, registered


def test_registered_execute_is_an_absolute_path_to_a_real_pwsh(tmp_path):
    """THE guard. A bare ``pwsh.exe`` registers a task that Task Scheduler
    cannot launch on a Store/MSIX host — 0x80070002 on every trigger,
    forever, with no visible failure. The registered Execute must be an
    absolute path to an executable that exists on this machine."""
    proc, registered = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert registered is not None, "Register-ScheduledTask was never called"
    execute = registered["Execute"]
    assert Path(execute).is_absolute(), (
        f"Execute must be an absolute path, not a bare name Task Scheduler "
        f"resolves against the machine PATH: {execute!r}")
    assert Path(execute).exists(), f"registered pwsh does not exist: {execute!r}"


@pytest.mark.skipif(sys.platform != "win32", reason="MSIX layout is Windows-only")
def test_msix_install_registers_the_stable_alias_not_the_versioned_package(tmp_path):
    """For a Store install, ``Get-Command pwsh`` resolves into the versioned
    package directory (``...\\WindowsApps\\Microsoft.PowerShell_<ver>_...``),
    whose path changes on every Store update — registering it plants the
    same time bomb one update later. The per-user app-execution alias
    (``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\pwsh.exe``) is the stable
    spelling of the same executable and must win when it exists."""
    fake_local = tmp_path / "LocalAppData"
    alias = fake_local / "Microsoft" / "WindowsApps" / "pwsh.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"")
    versioned = (r"C:\Program Files\WindowsApps"
                 r"\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe")
    get_command_stub = f"""
function global:Get-Command {{
    [CmdletBinding()]
    param([Parameter(Position = 0)]$Name, $CommandType,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    if ($Name -in @('pwsh', 'pwsh.exe')) {{
        return [pscustomobject]@{{ Source = '{versioned}' }}
    }}
    Microsoft.PowerShell.Core\\Get-Command @PSBoundParameters
}}
"""
    no_msi = tmp_path / "ProgramFiles"
    no_msi.mkdir()
    proc, registered = _run(tmp_path, get_command_stub,
                            {"LOCALAPPDATA": str(fake_local),
                             "ProgramFiles": str(no_msi)})
    assert proc.returncode == 0, proc.stderr
    assert registered is not None, "Register-ScheduledTask was never called"
    assert registered["Execute"] == str(alias), (
        f"expected the stable per-user alias {str(alias)!r}, "
        f"got {registered['Execute']!r}")


def test_encoded_command_still_carries_the_script_and_retention_parameters(tmp_path):
    """Pin the other half of the action while a test file finally exists for
    it: the ``-EncodedCommand`` payload (base64 of UTF-16LE) must invoke the
    permanent-checkout ``prune-build-cache.ps1`` with both retention knobs."""
    proc, registered = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert registered is not None
    argument = registered["Argument"]
    assert "-EncodedCommand " in argument
    encoded = argument.split("-EncodedCommand ", 1)[1].split()[0]
    inner = base64.b64decode(encoded).decode("utf-16-le")
    assert str(REPO / "ops" / "prune-build-cache.ps1") in inner
    assert "-MaxAgeHours 168" in inner
    assert "-MaxUsedSpaceGB 20" in inner
    # Decision of 16 November 2026: the weekly run also removes dangling
    # images (prune-build-cache.ps1 runs only `docker image prune --force`).
    assert "-PruneDanglingImages" in inner


def _home_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"USERPROFILE": str(home), "HOME": str(home)}


def _pwsh_found_at(path: str) -> str:
    """Make Get-Command name a pwsh at ``path`` (which may not exist)."""
    return f"""
function global:Get-Command {{
    [CmdletBinding()]
    param([Parameter(Position = 0)]$Name, $CommandType,
          [Parameter(ValueFromRemainingArguments = $true)]$Rest)
    if ($Name -in @('pwsh', 'pwsh.exe')) {{
        return [pscustomobject]@{{ Source = '{path}' }}
    }}
    Microsoft.PowerShell.Core\\Get-Command @PSBoundParameters
}}
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Program Files layout is Windows-only")
def test_msi_install_wins_over_the_store_alias(tmp_path):
    """Observed 16 November 2026: a task registered with the per-user Store
    alias failed with 0x80070002 again once the host replaced the Store
    install of PowerShell 7 with the MSI one, because the alias left with
    the Store package. The MSI path (``%ProgramFiles%\\PowerShell\\7``)
    stays the same across every 7.x update, so it wins when it exists."""
    program_files = tmp_path / "ProgramFiles"
    msi = program_files / "PowerShell" / "7" / "pwsh.exe"
    msi.parent.mkdir(parents=True)
    msi.write_bytes(b"")
    fake_local = tmp_path / "LocalAppData"
    alias = fake_local / "Microsoft" / "WindowsApps" / "pwsh.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"")
    versioned = (r"C:\Program Files\WindowsApps"
                 r"\Microsoft.PowerShell_7.6.5.0_x64__8wekyb3d8bbwe\pwsh.exe")
    proc, registered = _run(tmp_path, _pwsh_found_at(versioned),
                            {"LOCALAPPDATA": str(fake_local),
                             "ProgramFiles": str(program_files)})
    assert proc.returncode == 0, proc.stderr
    assert registered is not None, "Register-ScheduledTask was never called"
    assert registered["Execute"] == str(msi), registered


def test_refuses_to_register_a_pwsh_that_does_not_exist(tmp_path):
    """A registration whose executable does not exist fails every run with
    0x80070002 and nothing else to see. The installer must refuse, name the
    path and register nothing."""
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = str(tmp_path / "gone" / "pwsh.exe")
    proc, registered = _run(tmp_path, _pwsh_found_at(missing),
                            {"LOCALAPPDATA": str(empty),
                             "ProgramFiles": str(empty)})
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert registered is None, f"registered a missing pwsh: {registered}"
    assert "Refusing" in proc.stdout and missing in proc.stdout, proc.stdout


def _registered_copy(tmp_path: Path, body: str, parent: str = "checkout"):
    """Register (stubbed) from a copy of ops/ whose prune script is ``body``,
    so a run of the registered command can be watched without Docker."""
    ops = tmp_path / parent / "ops"
    ops.mkdir(parents=True)
    shutil.copy(SCRIPT, ops / SCRIPT.name)
    prune = ops / "prune-build-cache.ps1"
    prune.write_text(body, encoding="utf-8")
    env = _home_env(tmp_path)
    proc, registered = _run(tmp_path, "", env, script=ops / SCRIPT.name)
    assert proc.returncode == 0, proc.stderr
    assert registered is not None
    encoded = registered["Argument"].split("-EncodedCommand ", 1)[1].split()[0]
    log = tmp_path / "home" / ".pseudolife-mcp" / "cache-retention-last.log"
    return prune, encoded, env, log, registered


def test_a_path_with_a_typographic_quote_still_runs(tmp_path):
    """PowerShell reads ’ (U+2019) as a single quote, so a checkout under a
    folder like O’Brien broke the decoded command before it ran (review of
    this change). Every such quote is escaped, and -Check reads the path
    back."""
    prune, encoded, env, log, registered = _registered_copy(
        tmp_path, "Write-Output 'ran'\n", parent="O’Brien")
    proc = _run_registered(encoded, env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "ran" in log.read_text(encoding="utf-8-sig")
    check = _check(tmp_path, PWSH, "", 0, argument=registered["Argument"])
    assert check.returncode == 0, (check.stdout, check.stderr)
    assert f"-> {prune}" in check.stdout, check.stdout


def _run_registered(encoded: str, env: dict):
    return subprocess.run(
        [PWSH, "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=120, env=_merge_env(env),
    )


def test_a_run_records_its_output_and_result(tmp_path):
    """Each run leaves its output and result in
    ``~/.pseudolife-mcp/cache-retention-last.log``, so a weekly run can be
    read after the fact. The result is the script's own: a native command
    that ended non-zero inside it (fstrim, which the script only warns
    about) must not turn a successful run into a failed one (Codex review
    of this change)."""
    prune, encoded, env, log, _ = _registered_copy(
        tmp_path,
        "Write-Output \"ran with $($args -join ' ')\"\n"
        "& ([Environment]::ProcessPath) -NoProfile -Command 'exit 17'\n")
    proc = _run_registered(encoded, env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    text = log.read_text(encoding="utf-8-sig")
    assert ("ran with -MaxAgeHours 168 -MaxUsedSpaceGB 20 -PruneDanglingImages"
            in text), text
    assert text.rstrip().endswith("exit 0"), text


def test_a_run_whose_script_throws_logs_the_error(tmp_path):
    """The retention script throws on a real failure (an unparseable docker
    size, a failed prune). That error must reach the log, with exit 1,
    instead of escaping the pipeline and leaving the log empty (Codex
    review of this change)."""
    prune, encoded, env, log, _ = _registered_copy(
        tmp_path, "Write-Output 'before the failure'\nthrow 'unparseable docker size: x'\n")
    proc = _run_registered(encoded, env)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    text = log.read_text(encoding="utf-8-sig")
    assert "FAILED: unparseable docker size: x" in text, text
    assert text.rstrip().endswith("exit 1"), text


def test_a_run_whose_script_is_gone_says_so_in_its_log(tmp_path):
    """A checkout that moved after registration used to make every run fail
    with nothing written anywhere. The run must name the missing script and
    the fix in its log and exit 2."""
    prune, encoded, env, log, _ = _registered_copy(tmp_path, "exit 0\n")
    prune.unlink()
    proc = _run_registered(encoded, env)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    text = log.read_text(encoding="utf-8-sig")
    assert "FAILED: the retention script is missing" in text, text
    assert str(prune) in text, text
    assert "install-cache-retention.ps1" in text, text


def _check(tmp_path: Path, execute: str | None, script: str, result: int,
           argument: str | None = None, actions: str | None = None):
    inner = f"& '{script}' -MaxAgeHours 168 -MaxUsedSpaceGB 20"
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    if argument is None:
        argument = f"-NoProfile -WindowStyle Hidden -EncodedCommand {encoded}"
    if actions is None:
        actions = (f"@([pscustomobject]@{{ Execute = '{execute}'; "
                   f"Arguments = '{argument}' }})")
    if execute is None:
        stubs = ("function global:Get-ScheduledTask "
                 "{ param([string]$TaskName, $ErrorAction) $null }\n")
    else:
        stubs = f"""
function global:Get-ScheduledTask {{
    param([string]$TaskName, $ErrorAction)
    [pscustomobject]@{{ Actions = {actions} }}
}}
function global:Get-ScheduledTaskInfo {{
    param([string]$TaskName, $ErrorAction)
    [pscustomobject]@{{ LastRunTime = '15/11/2026 3:00:00 AM'; LastTaskResult = {result} }}
}}
"""
    return _run(tmp_path, stubs, _home_env(tmp_path), "-Check")[0]


def test_check_names_each_reason_a_registration_cannot_run(tmp_path):
    """Measured 16 November 2026 on the maintainer's host: the registered
    pwsh was gone and every run since had ended 0x80070002, while the task
    still showed Ready. ``-Check`` reads the registration (changing nothing)
    and names each reason, with the fix."""
    gone_exe = str(tmp_path / "gone" / "pwsh.exe")
    gone_script = str(tmp_path / "gone" / "prune-build-cache.ps1")
    proc = _check(tmp_path, gone_exe, gone_script, 2147942402)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert f"its pwsh is missing: {gone_exe}" in proc.stdout, proc.stdout
    assert (f"its retention script is missing: {gone_script}"
            in proc.stdout), proc.stdout
    assert "0x80070002" in proc.stdout, proc.stdout
    assert "re-run ops\\install-cache-retention.ps1" in proc.stdout, proc.stdout


def test_check_passes_a_registration_that_can_run(tmp_path):
    script = str(REPO / "ops" / "prune-build-cache.ps1")
    proc = _check(tmp_path, PWSH, script, 0)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "can run" in proc.stdout, proc.stdout


def test_check_says_when_nothing_is_registered(tmp_path):
    proc = _check(tmp_path, None, "", 0)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "is not registered" in proc.stdout, proc.stdout


def test_check_notes_a_result_left_from_before_a_repair(tmp_path):
    """Task Scheduler keeps the last run's 0x80070002 until the next run, so
    right after a repair -Check must not call a runnable registration broken
    (review of this change, on the maintainer's host): it passes and says how
    to confirm."""
    script = str(REPO / "ops" / "prune-build-cache.ps1")
    proc = _check(tmp_path, PWSH, script, 2147942402)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "can run" in proc.stdout and "0x80070002" in proc.stdout, proc.stdout
    assert "Start-ScheduledTask" in proc.stdout, proc.stdout


def test_check_reports_a_failed_last_run(tmp_path):
    script = str(REPO / "ops" / "prune-build-cache.ps1")
    proc = _check(tmp_path, PWSH, script, 1)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "ended with result 1" in proc.stdout, proc.stdout


def test_check_reads_the_command_this_installer_registers(tmp_path):
    prune, _encoded, _env, _log, registered = _registered_copy(tmp_path, "exit 0\n")
    proc = _check(tmp_path, PWSH, "", 0, argument=registered["Argument"])
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert f"-> {prune}" in proc.stdout, proc.stdout


def test_check_refuses_a_task_with_no_action(tmp_path):
    proc = _check(tmp_path, PWSH, "", 0, actions="@()")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "it has no action to run" in proc.stdout, proc.stdout
