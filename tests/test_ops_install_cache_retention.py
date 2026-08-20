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


def _run(tmp_path: Path, extra_stubs: str = "", env: dict | None = None,
         *args: str):
    log = tmp_path / "registered.json"
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        f"$env:INSTALL_TEST_LOG = '{log}'\n{_STUBS}\n{extra_stubs}\n"
        f'& "{SCRIPT}" {" ".join(args)}\n',
        encoding="utf-8",
    )
    import os
    merged = {**os.environ, **(env or {})}
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
    proc, registered = _run(tmp_path, get_command_stub,
                            {"LOCALAPPDATA": str(fake_local)})
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
