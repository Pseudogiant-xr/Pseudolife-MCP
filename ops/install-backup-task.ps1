#Requires -Version 7.2
# Register a daily bank backup (Windows Task Scheduler), so the bank is
# backed up whether or not a deploy happens.
#
#   ops\install-backup-task.ps1              # daily 03:00
#   ops\install-backup-task.ps1 -At 02:15
#   ops\install-backup-task.ps1 -Uninstall   # remove it
#
# Deploys (ops\update.ps1) back up first, but nothing else did on a
# schedule: from 2026-09-14 13:28 to 09-20 12:18 no dump existed anywhere,
# and nothing said so. The task runs ops\backup.ps1 from the MAIN checkout
# with its defaults (7-day rotation, the PSEUDOLIFE_BACKUP_MIRROR mirror,
# the row-count gate) and appends every run to data\backups\backup-task.log.
# A run that stops happening shows as a growing last_backup age in /health.
param(
    [string]$At = "03:00",
    # How long a run waits for Docker + Postgres before trying the backup
    # anyway: a catch-up run fires at logon, often before Docker Desktop is up.
    [ValidateRange(0, 3600)][int]$DockerWaitSeconds = 600,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$taskName = "Pseudolife-MCP daily backup"

if ($Uninstall) {
    if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        Write-Host "'$taskName' is already not registered."
        return
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Unregistered '$taskName'."
    return
}

function Resolve-MainCheckout {
    # A worktree's own ops\backup.ps1 disappears with the worktree, and its
    # data\backups is a folder nothing else reads: 15 of the 19 deploy
    # dumps from 2026-09-11..22 ended up in such folders. git's common dir
    # is the main checkout's .git from any worktree. Outside a git checkout
    # (an extracted release) this script's own checkout is the only one.
    $here = Split-Path -Parent $PSScriptRoot
    try {
        $common = git -C $here rev-parse --path-format=absolute --git-common-dir 2>$null
        if ($LASTEXITCODE -eq 0 -and $common -and (Split-Path -Leaf $common) -eq ".git") {
            return [IO.Path]::GetFullPath((Split-Path -Parent $common))
        }
    } catch {
        # git missing entirely: same answer as "not a checkout".
    }
    return $here
}

function Resolve-PwshForTaskScheduler {
    # Same resolution as ops\install-cache-retention.ps1, which records why:
    # a bare "pwsh.exe" dies with 0x80070002 on a Store/MSIX install, and
    # the versioned package path moves on every Store update, so the
    # per-user app-execution alias wins when present.
    $cmd = Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $source = if ($cmd) { $cmd.Source } else { [Environment]::ProcessPath }
    if ($source -match '\\WindowsApps\\Microsoft\.PowerShell_' -and $env:LOCALAPPDATA) {
        $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
        if (Test-Path $alias) { return $alias }
    }
    return $source
}

$repo = Resolve-MainCheckout
$script = Join-Path $repo "ops\backup.ps1"
if (-not (Test-Path $script)) { throw "not found: $script" }
$log = Join-Path $repo "data\backups\backup-task.log"
# The task runs whatever the main checkout holds. One that has not been
# updated yet still backs up daily, so warn rather than refuse, but loudly.
if (-not (Select-String -LiteralPath $script -SimpleMatch "AcceptRowDrop" -Quiet)) {
    Write-Warning ("$script predates the row-count gate: until $repo is updated, " +
        "the daily run backs up without it (a wipe followed by a few runs can " +
        "rotate the good copies away) and /health shows no last_backup.")
}

# The task's view of success is the process exit code, and its window is
# hidden: log every run, and turn a thrown backup into exit 1 with the
# reason in the log. Before the backup, wait (bounded) until Postgres
# answers: a catch-up run fires at logon, when Docker Desktop is often
# still starting, and would otherwise fail every morning after a
# powered-off night. Base64 -EncodedCommand survives Task Scheduler's
# single argument string; single quotes are doubled for paths like
# C:\Users\O'Brien\...
$template = @'
$log = '@LOG@'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
"==> $(Get-Date -Format s) scheduled backup" | Out-File -Append -FilePath $log
$deadline = (Get-Date).AddSeconds(@WAIT@)
$ready = $false
while ((Get-Date) -lt $deadline) {
    $global:LASTEXITCODE = 1
    docker exec pseudolife-mcp-postgres pg_isready -q *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    $left = ($deadline - (Get-Date)).TotalMilliseconds
    Start-Sleep -Milliseconds ([Math]::Max(100, [Math]::Min(10000, $left)))
}
if (-not $ready -and @WAIT@ -gt 0) {
    "Docker/Postgres not ready after @WAIT@s; trying the backup anyway" |
        Out-File -Append -FilePath $log
}
try { & '@SCRIPT@' *>> $log }
catch { "FAILED: $_" | Out-File -Append -FilePath $log; exit 1 }
'@
$inner = $template.Replace('@LOG@', ($log -replace "'", "''")).
    Replace('@SCRIPT@', ($script -replace "'", "''")).
    Replace('@WAIT@', [string]$DockerWaitSeconds)
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute (Resolve-PwshForTaskScheduler) `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded"
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable is the point: a desktop is often off at 03:00, and a
# missed run must happen at the next boot or logon, not vanish (the replica
# push lost 2026-09-18 and 09-19 that way).
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
# The logged-on user, interactively: Docker Desktop only runs in a user
# session, and PSEUDOLIFE_BACKUP_MIRROR is a User-scope variable.
$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Daily pg_dump + state tar of the Pseudolife bank via $script" | Out-Null

Write-Host "Registered '$taskName' (daily $At) -> $script"
Write-Host "Log       :  $log"
Write-Host "Run now   :  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove    :  ops\install-backup-task.ps1 -Uninstall"
