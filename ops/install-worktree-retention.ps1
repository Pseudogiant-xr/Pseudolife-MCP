#Requires -Version 7.2
# Register a weekly worktree and build-cache retention task (Windows Task Scheduler).
#
#   ops\install-worktree-retention.ps1                       # Sunday 04:00
#   ops\install-worktree-retention.ps1 -DayOfWeek Wednesday -At 21:00
#   ops\install-worktree-retention.ps1 -Unregister           # remove it
#
# The task runs `python ops\prune_worktrees.py --apply`, which reads its
# roots from ~/.pseudolife-mcp/retention.toml and does nothing until roots
# are configured there. Sessions clean up after themselves with
# `prune_worktrees.py --mine --apply` as their work merges; this weekly
# sweep is the backstop for the ones that did not. Runbook:
# docs/dev/worktree-retention.md.
param(
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$DayOfWeek = "Sunday",
    [string]$At = "04:00",
    [string]$Python,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$taskName = "Pseudolife-MCP worktree retention"

if ($Unregister) {
    if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        Write-Host "'$taskName' is already not registered."
        return
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Unregistered '$taskName'."
    return
}

$script = Join-Path $PSScriptRoot "prune_worktrees.py"
if (-not (Test-Path $script)) { throw "not found: $script" }

# The registered path must outlive the task: a linked worktree is exactly
# what this task removes once its branch merges, after which every run
# would fail silently.
$gitDir = (& git -C $PSScriptRoot rev-parse --path-format=absolute --git-dir 2>$null)
$commonDir = (& git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
if ($gitDir -and $commonDir -and ($gitDir -ne $commonDir)) {
    Write-Host "Refusing: $PSScriptRoot is in a linked git worktree, which this task would later remove. Nothing was registered."
    Write-Host "Run it from the main checkout:  <main checkout>\ops\install-worktree-retention.ps1"
    exit 1
}

function Resolve-PwshForTaskScheduler {
    # Same resolution as ops\install-cache-retention.ps1 (see its comment):
    # Task Scheduler resolves a bare "pwsh.exe" against the MACHINE path,
    # which a Store/MSIX install is absent from (0x80070002 on every run,
    # observed 2026-08-20), and the versioned MSIX package directory changes
    # on every Store update, so the per-user app-execution alias wins.
    $cmd = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $source = if ($cmd) { $cmd.Source } else { [Environment]::ProcessPath }
    if ($source -match '\\WindowsApps\\Microsoft\.PowerShell_' -and $env:LOCALAPPDATA) {
        $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
        if (Test-Path $alias) { return $alias }
    }
    return $source
}

function Resolve-Python {
    # Ask the interpreter for its own path: a bare "python" may be the
    # Store's app-execution stub, which opens the Store instead of running.
    param([string]$Candidate)
    if (-not $Candidate) {
        $cmd = Get-Command python, py -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $cmd) { return $null }
        $Candidate = $cmd.Source
    }
    try {
        $exe = & $Candidate -c "import sys; assert sys.version_info >= (3, 10); print(sys.executable)" 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $exe) { return $null }
    return ($exe | Select-Object -Last 1).Trim()
}

$pythonExe = Resolve-Python $Python
if (-not $pythonExe) {
    Write-Host "Refusing: no Python 3.10+ found$(if ($Python) { " at '$Python'" } else { ' on PATH' }). Nothing was registered."
    Write-Host "Pass one explicitly:  ops\install-worktree-retention.ps1 -Python <path to python.exe>"
    exit 1
}

$stateDir = Join-Path $HOME ".pseudolife-mcp"
$log = Join-Path $stateDir "retention-last.log"
$config = Join-Path $stateDir "retention.toml"

# Base64 -EncodedCommand, as ops\install-cache-retention.ps1 does; embedded
# single quotes are doubled so an O'Brien path cannot break the command.
function Quote([string]$s) { "'" + ($s -replace "'", "''") + "'" }
$inner = "New-Item -ItemType Directory -Force $(Quote $stateDir) | Out-Null; " +
    "& $(Quote $pythonExe) $(Quote $script) --apply *>&1 | Out-File -Encoding utf8 $(Quote $log); " +
    "exit `$LASTEXITCODE"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
$execute = Resolve-PwshForTaskScheduler
$argument = "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"

# The description carries a signature of what is registered, so a rerun
# with nothing changed says so in one line instead of re-registering.
$signature = "$execute|$argument|$DayOfWeek|$At"
$hash = [Convert]::ToHexString(
    [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($signature))).Substring(0, 16)
$description = "Remove merged, clean, idle git worktrees and stale build caches " +
    "(ops\prune_worktrees.py --apply; roots from ~\.pseudolife-mcp\retention.toml). [sig $hash]"

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -eq $description) {
    Write-Host "'$taskName' is already registered as requested; nothing changed."
    return
}

$action = New-ScheduledTaskAction -Execute $execute -Argument $argument
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At
# StartWhenAvailable: a desktop is often off at the trigger time, and a
# retention run that silently never happens is the failure to avoid.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Force -Description $description | Out-Null

Write-Host "Registered '$taskName' ($DayOfWeek $At) -> $script"
if (-not (Test-Path $config)) {
    Write-Host "No roots configured yet: the task does nothing until $config names them (docs/dev/worktree-retention.md)."
}
Write-Host "Last run's output: $log    Removals: $(Join-Path $stateDir 'retention-ledger.jsonl')"
Write-Host "Run now with:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove with :  ops\install-worktree-retention.ps1 -Unregister"
