"""Fully mocked PowerShell controls for the installed-proof absence assertion."""
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch


def controls(harness):
    with tempfile.TemporaryDirectory(prefix="r6-absence-controls-") as directory:
        root = Path(directory)
        scripts = []

        def capture(argv, **kwargs):
            if "-File" in argv:
                scripts.append(Path(argv[argv.index("-File") + 1]).read_text())
            else:
                scripts.append(argv[argv.index("-Command") + 1])

        with patch.object(harness, "checked", side_effect=capture):
            harness.windows_cleanup("r6-mocked", root / "native.exe", root)
            harness.windows_assert_task_absent("r6-mocked")

        mock = r'''
$ErrorActionPreference = 'Stop'
function Unregister-ScheduledTask { param($TaskName, $Confirm, $ErrorAction) }
function Get-CimInstance { param($ClassName, $Filter) }
function Stop-Process { throw 'unexpected process stop' }
function Get-Date { $global:r6AbsenceNow }
function Start-Sleep { param($Milliseconds) $global:r6AbsenceNow = $global:r6AbsenceNow.AddMilliseconds($Milliseconds); $global:r6AbsenceSleeps++ }
function Get-ScheduledTask {
    [CmdletBinding()]
    param($TaskName)
    $global:r6AbsenceQueries++
    if ($TaskName -ne 'r6-mocked') { throw 'wrong task name' }
    if ($global:r6AbsenceCase -eq 'Surviving' -or ($global:r6AbsenceCase -eq 'TransientSurviving' -and $global:r6AbsenceQueries -gt 2)) {
        return [pscustomobject]@{ TaskName=$TaskName }
    }
    if ($global:r6AbsenceCase -eq 'EmptyResult') { return }
    $id = 'CmdletizationQuery_NotFound_TaskName'
    if ($global:r6AbsenceCase -eq 'Persistent' -or ($global:r6AbsenceCase -like 'Transient*' -and $global:r6AbsenceQueries -le 2)) { $id = 'HRESULT 0x80070002' }
    if ($global:r6AbsenceCase -eq 'AccessDenied') { $id = 'HRESULT 0x80070005' }
    if ($global:r6AbsenceCase -eq 'NearMiss') { $id = 'CmdletizationQuery_NotFound_TaskName_Other' }
    $record = [Management.Automation.ErrorRecord]::new([InvalidOperationException]::new('mock query error'), $id, [Management.Automation.ErrorCategory]::NotSpecified, $TaskName)
    $PSCmdlet.WriteError($record)
}
$failures = @()
'''
        for index, script in enumerate(scripts):
            source = root / f"assertion-{index}.ps1"
            source.write_text(script)
            source_literal = str(source).replace("'", "''")
            mock += f"\n$assertion = '{source_literal}'\n" + r'''
foreach ($case in @('TransientAbsent','Surviving','TransientSurviving','Persistent','NamedAbsent','AccessDenied','NearMiss','EmptyResult')) {
    $global:r6AbsenceCase=$case; $global:r6AbsenceQueries=0; $global:r6AbsenceSleeps=0; $global:r6AbsenceNow=[datetime]'2026-01-01'
    $failed=$false
    try { & $assertion 'r6-mocked' 'mock-native' 'mock-repo' }
    catch { $failed=$true }
    $expectedFailure = $case -notin @('TransientAbsent','NamedAbsent')
    $expectedQueries = if ($case -like 'Transient*') { 3 } elseif ($case -eq 'Persistent') { 76 } else { 1 }
    $expectedSleeps = if ($case -like 'Transient*') { 2 } elseif ($case -eq 'Persistent') { 75 } else { 0 }
    if ($failed -ne $expectedFailure -or $global:r6AbsenceQueries -ne $expectedQueries -or $global:r6AbsenceSleeps -ne $expectedSleeps) {
        $failures += "$case failed=$failed queries=$global:r6AbsenceQueries sleeps=$global:r6AbsenceSleeps"
    } else { Write-Output "PASS $case queries=$global:r6AbsenceQueries sleeps=$global:r6AbsenceSleeps" }
}
'''
        mock += "\nif ($failures.Count) { throw ($failures -join \"`n\") }\n"
        driver = root / "controls.ps1"
        driver.write_text(mock)
        result = subprocess.run(["pwsh", "-NoProfile", "-File", str(driver)],
                                capture_output=True)
        print(result.stdout.decode(errors="replace"), end="", flush=True)
        assert result.returncode == 0, result.stderr.decode(errors="replace")


if __name__ == "__main__":
    import autostart
    controls(autostart)
