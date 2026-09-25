#Requires -Version 7.2
# Register a daily bank backup (Windows Task Scheduler), so the bank is
# backed up whether or not a deploy happens.
#
#   ops\install-backup-task.ps1              # daily 03:00
#   ops\install-backup-task.ps1 -At 02:15
#   <dir>\ops\install-backup-task.ps1 -ScriptCheckout <dir>   # <dir>: a locked
#                                            # worktree of the main checkout
#   ops\install-backup-task.ps1 -Uninstall   # remove it
#
# Deploys (ops\update.ps1) back up first, but nothing else did on a
# schedule: from 2026-09-14 13:28 to 09-20 12:18 no dump existed anywhere,
# and nothing said so. The task runs ops\backup.ps1 from the MAIN checkout
# (or -ScriptCheckout) with its defaults (7-day rotation, the
# PSEUDOLIFE_BACKUP_MIRROR mirror, the row-count gate), writes into the main
# checkout's data\backups, and appends every run to backup-task.log there.
# A run that stops happening shows as a growing last_backup age in /health.
# [CmdletBinding()] makes a misspelled parameter an error: without it, a
# typo in -ScriptCheckout would silently register the main checkout.
[CmdletBinding()]
param(
    [string]$At = "03:00",
    # How long a run waits for Docker + Postgres before trying the backup
    # anyway: a catch-up run fires at logon, often before Docker Desktop is up.
    [ValidateRange(0, 3600)][int]$DockerWaitSeconds = 600,
    # Run ops\backup.ps1 from this checkout instead of the main one. The main
    # checkout can lag master for days while it holds uncommitted work: on
    # 2026-09-24 and 09-25 the task ran a backup.ps1 from before the row-count
    # gate. A dedicated worktree of master avoids that; dumps and the log
    # still go to the main checkout's data\backups. The worktree must be
    # locked (git worktree lock) so worktree cleanup cannot delete it, and
    # must not be the checkout that would receive the dumps.
    [string]$ScriptCheckout = "",
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

function Assert-LockedIfWorktree([string]$Dir) {
    # A linked worktree's git dir is <common dir>\worktrees\<id>, and
    # `git worktree lock` marks it with a file named "locked" there: that
    # file is what keeps `git worktree prune` and `remove` away from the
    # backup.ps1 the task runs. A main checkout or a separate clone has
    # git dir == common dir; outside git there is nothing to check.
    try {
        $gitDir = git -C $Dir rev-parse --path-format=absolute --git-dir 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $gitDir) { return }
        $common = git -C $Dir rev-parse --path-format=absolute --git-common-dir 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $common) { return }
    } catch {
        return   # git missing entirely: same answer as "not a checkout".
    }
    if ([IO.Path]::GetFullPath($gitDir) -eq [IO.Path]::GetFullPath($common)) { return }
    if (Test-Path -LiteralPath (Join-Path $gitDir "locked")) { return }
    throw ("$Dir is a git worktree that is not locked, so worktree cleanup could " +
           "delete the backup.ps1 the task runs. Lock it first: " +
           "git worktree lock --reason ""daily backup task"" ""$Dir""")
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
$checkout = $repo
if ($ScriptCheckout) {
    $checkout = (Resolve-Path -LiteralPath $ScriptCheckout).ProviderPath
}
# Separator-free joins: the tests drive this script under pwsh on Linux CI.
$script = Join-Path $checkout "ops" "backup.ps1"
if (-not (Test-Path $script)) { throw "not found: $script" }
if ($ScriptCheckout) {
    # The data home is the checkout this installer resolves, so a separate
    # clone's own installer would route the dumps into that clone, where
    # restore and the replica push never look.
    $sep = [IO.Path]::DirectorySeparatorChar
    if ([IO.Path]::GetFullPath($checkout).TrimEnd($sep) -eq [IO.Path]::GetFullPath($repo).TrimEnd($sep)) {
        throw ("-ScriptCheckout $checkout is also where the dumps would go " +
               "($(Join-Path $repo 'data' 'backups')). Make $checkout a worktree of the " +
               "main checkout and run its installer: <worktree>\ops\install-backup-task.ps1 " +
               "-ScriptCheckout <worktree>. Or run this installer from the main checkout.")
    }
    Assert-LockedIfWorktree $checkout
}
# Always the main checkout's folder, passed explicitly: backup.ps1 would
# otherwise default to the data\backups beside itself, which for a
# -ScriptCheckout is a folder that restore.ps1 and the replica push never read.
$outDir = Join-Path $repo "data" "backups"
$log = Join-Path $outDir "backup-task.log"
# The task runs whatever that checkout holds. One that has not been
# updated yet still backs up daily, so warn rather than refuse, but loudly.
if (-not (Select-String -LiteralPath $script -SimpleMatch "AcceptRowDrop" -Quiet)) {
    Write-Warning ("$script predates the row-count gate: until $checkout is updated, " +
        "the daily run backs up without it (a wipe followed by a few runs can " +
        "rotate the good copies away) and /health shows no last_backup.")
}

# The task's view of success is the process exit code, and its window is
# hidden: log every run, and turn a thrown backup into exit 1 with the
# reason in the log. Before the backup, wait (bounded) until Postgres
# answers: a catch-up run fires at logon, when Docker Desktop is often
# still starting, and would otherwise fail every morning after a
# powered-off night. Each run logs the script checkout's HEAD, so one that
# has fallen behind master shows in the log instead of going unnoticed.
# Base64 -EncodedCommand survives Task Scheduler's single argument string;
# single quotes are doubled for paths like C:\Users\O'Brien\...
$template = @'
$log = '@LOG@'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
"==> $(Get-Date -Format s) scheduled backup" | Out-File -Append -FilePath $log
$checkout = '@CHECKOUT@'
$head = try { git -C $checkout rev-parse --short HEAD 2>$null } catch { $null }
"backup.ps1 from $checkout, checkout HEAD $(if ($head) { $head } else { 'unknown' })" |
    Out-File -Append -FilePath $log
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
try { & '@SCRIPT@' -OutDir '@OUTDIR@' *>> $log }
catch { "FAILED: $_" | Out-File -Append -FilePath $log; exit 1 }
'@
$inner = $template.Replace('@LOG@', ($log -replace "'", "''")).
    Replace('@CHECKOUT@', ($checkout -replace "'", "''")).
    Replace('@SCRIPT@', ($script -replace "'", "''")).
    Replace('@OUTDIR@', ($outDir -replace "'", "''")).
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
# session, and PSEUDOLIFE_BACKUP_MIRROR is a User-scope variable. DOMAIN\user
# from [Environment] (WindowsIdentity throws off Windows, where CI runs these
# tests).
$principal = New-ScheduledTaskPrincipal `
    -UserId "$([Environment]::UserDomainName)\$([Environment]::UserName)" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Daily pg_dump + state tar of the Pseudolife bank via $script into $outDir" | Out-Null

Write-Host "Registered '$taskName' (daily $At) -> $script"
Write-Host "Backups   :  $outDir"
Write-Host "Log       :  $log"
Write-Host "Run now   :  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove    :  ops\install-backup-task.ps1 -Uninstall"
