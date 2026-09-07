#Requires -Version 7
# Register the Claude extractor shim to start at logon (Windows Task
# Scheduler). (Install under pwsh 7 — the ternary below needs it; the
# scheduled task itself runs fine under powershell.exe 5.1.)
#
# Task Scheduler refuses per-user task registration from an UNELEVATED
# administrator account (Access is denied - probed 2026-09-02: fresh task,
# Limited principal, root folder and a subfolder alike), so run this from
# an ELEVATED PowerShell. Open that PowerShell fresh from the Start menu
# ("Windows PowerShell" -> Run as administrator). Do NOT request the UAC
# elevation from a shell running inside Claude Desktop or any other
# Store-packaged app (Store-installed pwsh 7 included): Windows' Application
# Information service then keeps a handle to that app's container job, and
# the app's next update fails to launch ("Another program is currently
# using this file") until a reboot - anthropics/claude-code#61635,
# reproduced live 2026-09-02; the most likely trigger was the Codex twin
# of this script being elevated from a Claude Desktop session on
# 2026-08-31 (inferred from timing, not proven).
#
#   ops\install-shim-autostart.ps1              # default port 8082, v5 prompt, opus
#   ops\install-shim-autostart.ps1 -Model claude-sonnet-5   # pick the served model
#
# The shim wraps the Max-plan `claude` CLI as an OpenAI-compatible endpoint on
# 127.0.0.1 for the daemon's dream pass (primary extractor; the in-stack E4B
# container is the fallback — see docs/superpowers/specs/
# 2026-07-11-sonnet-sidecar-cutover-design.md). Requires a logged-in CLI.
# -Model default is claude-opus-5 per the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json: cortex 0.885 vs 0.821, 5/0).
# -PromptFile default is sonnet_extractor_v5.md since 2026-09-07: the
# v2 body with its two pre-rule worked examples re-cut on invented names (the
# same re-cut the daemon's v12 base took on 2026-09-07), plus the
# assistant-facts blocks that shipped in dream.py on 2026-09-05.
# --system-prompt-file REPLACES the shipped prompt prefix, so on this path a
# daemon-side change alone never reaches the model: this file is what the
# shim actually sends. Gated on the ladder opus-5 rung (v4 vs v5, two
# replicates per arm): evals/results/ladder-shimv5-paired-verdict-threshold.json
# — gold 1.0, stale 0.0 and 16/16 claims on every run, tokens 14.1-15.7
# across both arms. The v2 -> v4 step (the assistant-facts blocks) rests on
# the earlier evals/results/ladder-shimprompt-rule2-paired-verdict-threshold.json
# (v2 vs v4, tokens 14.0-15.5 across both arms); its rule-v1 predecessor
# (ladder-shimprompt-paired-verdict-threshold.json, tokens 14.0-16.1) is
# superseded evidence and stays in the tree.
param(
    [string]$PythonExe = "",
    [int]$Port = 8082,
    [string]$Model = "claude-opus-5",
    [string]$PromptFile = "evals\prompts\sonnet_extractor_v5.md",
    [string]$LogFile = "$env:USERPROFILE\.pseudolife-mcp\claude-shim.log",
    # How long to wait for the started shim to bind the port. The shim warms
    # its health cache with one real `claude -p` call BEFORE it binds, so a
    # cold start is normally 10-30 s and a slow CLI login check can take
    # longer; 90 s covers both with margin (2026-09-06 restart: 11 s).
    [int]$StartupTimeoutSec = 90
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if (-not $PythonExe) {
    $venv = Join-Path $repo ".venv\Scripts\python.exe"
    $PythonExe = (Test-Path $venv) ? $venv : (Get-Command python).Source
}
$promptPath = Join-Path $repo $PromptFile
if (-not (Test-Path $promptPath)) { throw "prompt file not found: $promptPath" }
# Absolute up front: the task's cmd.exe would otherwise resolve a relative
# -LogFile against its WorkingDirectory ($repo) while the verification below
# resolves it against this shell's location.
$LogFile = [IO.Path]::GetFullPath($LogFile, (Get-Location).ProviderPath)
New-Item -ItemType Directory -Force (Split-Path -Parent $LogFile) | Out-Null

# Every process whose command line names claude_shim.py AND this --port. The
# task launches a three-layer tree (cmd.exe running the `>> log` redirect ->
# the .venv python.exe launcher -> the base interpreter that owns the socket)
# and no layer's death propagates to the others, so the whole set is what
# "the running shim" means here. Keyed on the port too: an A/B shim serving
# another port from the same script must survive an install.
function Get-ShimProcess {
    param([int]$ShimPort)
    $pattern = "claude_shim\.py.*--port\s+$ShimPort(\s|$)"
    @(Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern) })
}
function Get-PortListener {
    param([int]$ShimPort)
    Get-NetTCPConnection -LocalPort $ShimPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}
# Lines the shim appended to its log after byte offset $Offset. Opened with
# FileShare ReadWrite so the running shim's own handle is undisturbed.
function Read-LogSince {
    param([string]$Path, [long]$Offset)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $full = (Resolve-Path -LiteralPath $Path).ProviderPath   # .NET resolves relative to its own cwd
    $fs = [IO.File]::Open($full, [IO.FileMode]::Open, [IO.FileAccess]::Read,
                          [IO.FileShare]::ReadWrite)
    try {
        if ($Offset -gt $fs.Length) { $Offset = 0 }   # log was rotated/truncated
        $fs.Seek($Offset, [IO.SeekOrigin]::Begin) | Out-Null
        $reader = New-Object IO.StreamReader($fs)
        @(($reader.ReadToEnd() -split "`r?`n") | Where-Object { $_ })
    } finally { $fs.Dispose() }
}

$taskName = "Pseudolife Claude Shim"
$legacyTaskName = "Pseudolife Sonnet Shim"   # pre-rename installs
# -WindowStyle Hidden only hides a console window it still allocates — on
# Windows 11 with Windows Terminal set as the default terminal app, WT
# intercepts that console-allocation moment and opens a visible (blank)
# tab anyway, for the entire lifetime of the process (confirmed live:
# 2026-07-12, a real reboot showed a persistent blank WT tab owning the
# shim as its child). CreateNoWindow via .NET ProcessStartInfo skips
# console allocation entirely, so WT has nothing to attach a tab to —
# validated standalone (detached long-running child survives its spawner
# exiting; redirected output confirmed correct) before wiring in here.
# The scheduled task launches this tiny spawner, which starts the real
# python.exe chain fully detached (CreateNoWindow, own console-less
# session) and returns immediately, so the Task-Scheduler-owned window is
# at most a sub-second flash rather than persisting for the shim's whole
# runtime.
#
# cmd.exe's `/c` argument parsing mishandles a command line containing
# MORE than one quoted segment (e.g. a quoted exe path AND a quoted script
# arg) unless the whole thing is wrapped in one extra redundant pair of
# quotes (a documented `cmd /?` workaround) — hence the doubled `""` below.
$innerCmd = "`"$PythonExe`" `"$repo\evals\claude_shim.py`" --port $Port " +
            "--model $Model --system-prompt-file `"$promptPath`""
$cmdArgs = "/c `"$innerCmd >> `"`"$LogFile`"`" 2>&1`""
$inner = @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = 'cmd.exe'
`$psi.Arguments = '$($cmdArgs -replace "'", "''")'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$psi.WorkingDirectory = '$repo'
[System.Diagnostics.Process]::Start(`$psi) | Out-Null
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
# CIM cmdlet errors here do NOT reliably terminate even under
# $ErrorActionPreference = "Stop" (observed live: an unelevated run printed
# "Access is denied" and fell through to the success message). Force it, and
# verify the task actually exists before claiming success.
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Claude extractor CLI shim (dream pass primary; E4B sidecar is fallback)" `
    -ErrorAction Stop | Out-Null
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    throw ("task '$taskName' was not registered - Task Scheduler needs an ELEVATED " +
           "PowerShell on administrator accounts. Open it fresh from the Start menu; " +
           "never elevate from a shell inside Claude Desktop or another Store-packaged " +
           "app (its next update then fails to launch until a reboot - " +
           "anthropics/claude-code#61635).")
}

# Stop the shim that is already serving this port BEFORE starting the task.
# Re-registering over a live shim used to "succeed" while the old instance
# kept the port (2026-09-06, v2 -> v4 prompt cutover): on Windows a second
# http.server listener BINDS beside the first (allow_reuse_address is
# SO_REUSEADDR, which shares a port in LISTEN), so the new interpreter never
# hit an error to log, and its startup lines were then overwritten by the old
# shim's next log write (two `cmd >> log` opens keep independent file
# pointers). Both probed 2026-09-07. This runs only after registration
# succeeded: stopping the live shim and then failing to register would leave
# the box with no extractor at all.
$stale = @(Get-ShimProcess -ShimPort $Port)
if ($stale.Count -gt 0) {
    $pids = ($stale | ForEach-Object { "$($_.ProcessId) $($_.Name)" }) -join ", "
    Write-Host "Stopping the running claude_shim.py instance on port $Port ($pids) so the new task instance can take the port..."
    foreach ($p in $stale) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and
           (((Get-ShimProcess -ShimPort $Port).Count -gt 0) -or (Get-PortListener -ShimPort $Port))) {
        Start-Sleep -Milliseconds 500
    }
    $left = @(Get-ShimProcess -ShimPort $Port)
    if ($left.Count -gt 0) {
        throw ("could not stop the running shim (pid " +
               (($left | ForEach-Object ProcessId) -join ", ") + ") - stop it and re-run.")
    }
}
$holder = Get-PortListener -ShimPort $Port
if ($holder) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($holder.OwningProcess)" -ErrorAction SilentlyContinue
    if ($owner -and $owner.CommandLine -match 'claude_shim\.py') {
        # Launched by hand with a spelling the kill set does not parse
        # (--port=N, or the flag omitted for the default port).
        throw ("port $Port is held by a claude_shim.py this installer could not match by --port " +
               "(pid $($holder.OwningProcess)) - stop that process and re-run.")
    }
    throw ("port $Port is held by pid $($holder.OwningProcess) ($($owner.Name)), which is not " +
           "a claude_shim.py instance - free the port (or pick another with -Port) and re-run. " +
           "The task would not fail to bind beside it; it would just never receive the traffic.")
}

$logOffset = (Test-Path $LogFile) ? (Get-Item $LogFile).Length : 0
Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
Write-Host "Started '$taskName'; waiting up to ${StartupTimeoutSec}s for the shim to bind 127.0.0.1:$Port (health warm-up is one real CLI call)..."
# Affirmative verification: a listener on the port OWNED BY a claude_shim.py
# process. "The task ran" (LastTaskResult 0) only means the spawner exited.
$listener = $null
$deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
while (-not $listener -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $candidate = Get-PortListener -ShimPort $Port
    if ($candidate) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($candidate.OwningProcess)" -ErrorAction SilentlyContinue
        if ($owner -and $owner.CommandLine -match 'claude_shim\.py') { $listener = $candidate }
    }
}
$newLines = @(Read-LogSince -Path $LogFile -Offset $logOffset)
if (-not $listener) {
    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    $tail = if ($newLines.Count -gt 0) { $newLines -join "`n" } else {
        "(none - nothing reached $LogFile; check the CLI login with: claude -p hi)" }
    throw ("shim did not bind 127.0.0.1:$Port within ${StartupTimeoutSec}s " +
           "(task LastTaskResult $($info.LastTaskResult)). New log lines:`n$tail")
}
# The "serving" line is printed right after the bind; allow the flush a moment.
$settle = (Get-Date).AddSeconds(5)
while (-not @($newLines -match 'serving .* on ').Count -and (Get-Date) -lt $settle) {
    Start-Sleep -Milliseconds 500
    $newLines = @(Read-LogSince -Path $LogFile -Offset $logOffset)
}
$startup = @($newLines | Where-Object {
    $_ -match 'system prompt override from|serving .* on |health warm|Traceback|Error' })
if ($startup.Count -eq 0) {
    Write-Warning "the shim is listening (pid $($listener.OwningProcess)) but wrote no startup lines to $LogFile - the log redirect may be broken."
} else {
    foreach ($line in $startup) { Write-Host "  log: $line" }
}
Write-Host "Registered + started '$taskName' ($Model, port $Port, pid $($listener.OwningProcess), log $LogFile)."
Write-Host "Cutover env for the daemon (.env or compose override):"
Write-Host "  PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$Port/v1"
Write-Host "  PSEUDOLIFE_DREAM_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"
