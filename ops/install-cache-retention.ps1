#Requires -Version 7.2
# Register a weekly Docker build-cache retention task (Windows Task Scheduler).
#
#   ops\install-cache-retention.ps1                       # Sunday 03:00, defaults
#   ops\install-cache-retention.ps1 -DayOfWeek Wednesday -At 21:00
#   ops\install-cache-retention.ps1 -Unregister           # remove it
#
# The deploy path (ops\update.ps1) already prunes after every healthy deploy;
# this covers the other gap — stretches with no deploys at all, which is how
# 51.87GB accumulated by 2026-07-28.
param(
    [ValidateRange(0, 876000)][int]$MaxAgeHours = 168,
    [ValidateRange(0, 100000)][int]$MaxUsedSpaceGB = 20,
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$DayOfWeek = "Sunday",
    [string]$At = "03:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$taskName = "Pseudolife-MCP Docker cache retention"

if ($Unregister) {
    if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        Write-Host "'$taskName' is already not registered."
        return
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Unregistered '$taskName'."
    return
}

$script = Join-Path $PSScriptRoot "prune-build-cache.ps1"
if (-not (Test-Path $script)) { throw "not found: $script" }

function Resolve-PwshForTaskScheduler {
    # Task Scheduler resolves a bare "pwsh.exe" against the MACHINE path,
    # which a Store/MSIX install of PowerShell 7 is absent from: the task
    # registers fine, reports Ready, fires on schedule — and every run dies
    # with 0x80070002 (file not found) before the retention script starts.
    # Observed live 2026-08-20: no scheduled run had ever succeeded and the
    # cache sat at 23.6GB against the 20GB ceiling. Register an absolute
    # path instead. For an MSIX install Get-Command resolves into the
    # versioned package directory (...\WindowsApps\Microsoft.PowerShell_
    # <ver>_...), which changes on every Store update and would re-plant
    # the same failure one update later; the per-user app-execution alias
    # is the stable spelling of the same executable, so it wins when
    # present.
    $cmd = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $source = if ($cmd) { $cmd.Source } else { [Environment]::ProcessPath }
    if ($source -match '\\WindowsApps\\Microsoft\.PowerShell_' -and $env:LOCALAPPDATA) {
        $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
        if (Test-Path $alias) { return $alias }
    }
    return $source
}

# Base64 -EncodedCommand, as ops\install-autostart.ps1 does: it survives the
# quoting round-trip through Task Scheduler's single argument string.
# Escape embedded single quotes (PowerShell's quoting convention: '' inside
# a single-quoted string) so a path like C:\Users\O'Brien\... doesn't break
# the decoded command — which would fail silently until Task Scheduler
# actually invokes it, unattended.
$escapedScript = $script -replace "'", "''"
$inner = "& '$escapedScript' -MaxAgeHours $MaxAgeHours -MaxUsedSpaceGB $MaxUsedSpaceGB"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute (Resolve-PwshForTaskScheduler) `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At
# StartWhenAvailable is the point of this task: a desktop is often off at
# 03:00 Sunday, and a retention run that silently never happens is the
# failure mode being fixed.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "Prune Docker build cache older than $MaxAgeHours h (ceiling ${MaxUsedSpaceGB}GB), then fstrim the WSL disk." | Out-Null

Write-Host "Registered '$taskName' ($DayOfWeek $At) -> $script"
Write-Host "Run now with:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove with :  ops\install-cache-retention.ps1 -Unregister"
