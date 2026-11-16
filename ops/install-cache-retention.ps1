#Requires -Version 7.2
# Register a weekly Docker build-cache retention task (Windows Task Scheduler).
#
#   ops\install-cache-retention.ps1                       # Sunday 03:00, defaults
#   ops\install-cache-retention.ps1 -DayOfWeek Wednesday -At 21:00
#   ops\install-cache-retention.ps1 -Check                # is the registered task able to run?
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
    [switch]$Check,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$taskName = "Pseudolife-MCP Docker cache retention"
# Each run's output and exit code. A run that cannot find its retention
# script says so here; a run whose pwsh is gone never starts at all, which
# only -Check (reading the registration) can see.
$stateDir = Join-Path $HOME ".pseudolife-mcp"
$runLog = Join-Path $stateDir "cache-retention-last.log"

# Task Scheduler's result for "the file to run was not found" (0x80070002).
$fileNotFound = 2147942402

function Get-RegisteredScriptPath([string]$Argument) {
    $encoded = ($Argument -split '-EncodedCommand ')[-1].Trim()
    try {
        $cmd = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
    } catch {
        return $null
    }
    $m = [regex]::Match($cmd, "& '((?:[^']|'')+\.ps1)'")
    if (-not $m.Success) { return $null }
    return $m.Groups[1].Value -replace "''", "'"
}

if ($Check) {
    # Read-only: it inspects the registration and changes nothing.
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "'$taskName' is not registered. Register it with: ops\install-cache-retention.ps1"
        exit 1
    }
    $problems = @()
    $execute = $task.Actions[0].Execute
    if (-not $execute -or -not (Test-Path -LiteralPath $execute)) {
        $problems += "its pwsh is missing: $execute (PowerShell was moved or reinstalled)"
    }
    $registeredScript = Get-RegisteredScriptPath $task.Actions[0].Arguments
    if (-not $registeredScript) {
        $problems += "its command names no retention script"
    } elseif (-not (Test-Path -LiteralPath $registeredScript -PathType Leaf)) {
        $problems += "its retention script is missing: $registeredScript"
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    if ($info -and [int64]$info.LastTaskResult -eq $fileNotFound) {
        $problems += "its last run ($($info.LastRunTime)) could not start: the file to run was not found (0x80070002)"
    }
    if ($problems.Count) {
        Write-Host "'$taskName' cannot run as registered:"
        foreach ($p in $problems) { Write-Host "  - $p" }
        Write-Host "Fix: re-run ops\install-cache-retention.ps1 from the permanent checkout; it re-registers in place."
        exit 1
    }
    $when = if ($info) { " Last run: $($info.LastRunTime), result $($info.LastTaskResult)." } else { "" }
    Write-Host "'$taskName' can run: $execute -> $registeredScript.$when"
    if (Test-Path -LiteralPath $runLog) { Write-Host "Last run's output: $runLog" }
    exit 0
}

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
    # path instead.
    #
    # The MSI install's path wins when it exists: it stays the same across
    # every 7.x update and does not depend on a Store package. Observed
    # 2026-11-16: a task registered with the per-user Store alias failed
    # with 0x80070002 again after the host replaced the Store install with
    # the MSI one, because the alias left with the Store package.
    if ($env:ProgramFiles) {
        $msi = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
        if (Test-Path -LiteralPath $msi -PathType Leaf) { return $msi }
    }
    # For an MSIX install Get-Command resolves into the versioned package
    # directory (...\WindowsApps\Microsoft.PowerShell_<ver>_...), which
    # changes on every Store update; the per-user app-execution alias is the
    # stable spelling of the same executable, so it wins when present.
    $cmd = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $source = if ($cmd) { $cmd.Source } else { [Environment]::ProcessPath }
    if ($source -match '\\WindowsApps\\Microsoft\.PowerShell_' -and $env:LOCALAPPDATA) {
        $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
        if (Test-Path -LiteralPath $alias) { return $alias }
    }
    return $source
}

$execute = Resolve-PwshForTaskScheduler
if (-not $execute -or -not (Test-Path -LiteralPath $execute)) {
    Write-Host "Refusing: the pwsh to register ($execute) does not exist, so every scheduled run would fail with 0x80070002. Nothing was registered."
    Write-Host "Install PowerShell 7 (https://aka.ms/powershell) or run this script from the pwsh you want the task to use."
    exit 1
}

# Base64 -EncodedCommand, as ops\install-autostart.ps1 does: it survives the
# quoting round-trip through Task Scheduler's single argument string.
# Escape embedded single quotes (PowerShell's quoting convention: '' inside
# a single-quoted string) so a path like C:\Users\O'Brien\... doesn't break
# the decoded command — which would fail silently until Task Scheduler
# actually invokes it, unattended.
function Quote([string]$s) { "'" + ($s -replace "'", "''") + "'" }
# The run writes its output and exit code to $runLog, and refuses in that
# log (exit 2) when the retention script it was registered with is gone,
# such as after the checkout moved, instead of failing with nothing written.
$missing = "FAILED: the retention script is missing: $script. Re-run ops\install-cache-retention.ps1 from the permanent checkout."
$inner = "New-Item -ItemType Directory -Force $(Quote $stateDir) | Out-Null; " +
    "if (-not (Test-Path -LiteralPath $(Quote $script) -PathType Leaf)) { " +
    "((Get-Date -Format o) + ' ' + $(Quote $missing)) | Out-File -Encoding utf8 $(Quote $runLog); exit 2 }; " +
    "& $(Quote $script) -MaxAgeHours $MaxAgeHours -MaxUsedSpaceGB $MaxUsedSpaceGB -PruneDanglingImages *>&1 | Out-File -Encoding utf8 $(Quote $runLog); " +
    "`$code = `$LASTEXITCODE; ((Get-Date -Format o) + ' exit ' + `$code) | Out-File -Append -Encoding utf8 $(Quote $runLog); exit `$code"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute $execute `
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
    -Description "Prune Docker build cache older than $MaxAgeHours h (ceiling ${MaxUsedSpaceGB}GB) and dangling images, then fstrim the WSL disk." | Out-Null

Write-Host "Registered '$taskName' ($DayOfWeek $At) -> $script"
Write-Host "Runs with  : $execute"
Write-Host "Last run's output: $runLog"
Write-Host "Check with :  ops\install-cache-retention.ps1 -Check"
Write-Host "Run now with:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove with :  ops\install-cache-retention.ps1 -Unregister"
