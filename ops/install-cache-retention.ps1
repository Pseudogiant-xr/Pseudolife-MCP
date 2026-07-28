#Requires -Version 7
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
    [int]$MaxAgeHours = 168,
    [int]$MaxUsedSpaceGB = 20,
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$DayOfWeek = "Sunday",
    [string]$At = "03:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$taskName = "Pseudolife-MCP Docker cache retention"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Unregistered '$taskName'."
    return
}

$script = Join-Path $PSScriptRoot "prune-build-cache.ps1"
if (-not (Test-Path $script)) { throw "not found: $script" }

# Base64 -EncodedCommand, as ops\install-autostart.ps1 does: it survives the
# quoting round-trip through Task Scheduler's single argument string.
$inner = "& '$script' -MaxAgeHours $MaxAgeHours -MaxUsedSpaceGB $MaxUsedSpaceGB"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute "pwsh.exe" `
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
