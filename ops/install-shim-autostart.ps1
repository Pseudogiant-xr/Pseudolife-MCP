#Requires -Version 7
# Register the Claude extractor shim to start at logon (Windows Task
# Scheduler). (Install under pwsh 7 — the ternary below needs it; the
# scheduled task itself runs fine under powershell.exe 5.1.)
#
# No elevation: run this from a normal PowerShell 7 window. The task is
# registered for this user only (a logon trigger for this user, principal
# this user, Interactive, -RunLevel Limited), a shape Task Scheduler lets an
# unelevated user register, replace and remove: checked by the maintainer on
# a real desktop in a non-elevated Windows PowerShell on 16 November 2026,
# and with a UAC-style filtered token on the Windows dev VM. Earlier
# versions registered a trigger for ANY user's logon, which needs
# elevation. A task registered from an elevated shell is owned by
# Administrators and cannot be changed unelevated: this script then changes
# nothing and prints the one elevated command that removes it. Never
# request UAC elevation from a shell inside Claude Desktop or another
# Store-packaged app (Store-installed pwsh 7 included): Windows'
# Application Information service then keeps a handle to that app's
# container job, and the app's next update fails to launch ("Another
# program is currently using this file") until a reboot -
# anthropics/claude-code#61635, reproduced live 2026-09-02.
#
#   ops\install-shim-autostart.ps1              # default port 8082, v5 prompt, opus
#   ops\install-shim-autostart.ps1 -Model claude-sonnet-5   # pick the served model
#
# The task runs `ops\shim_autostart.py run claude`, which reads the model,
# prompt file, port, host, CLI path, interpreter and log file from ops/.env
# (PSEUDOLIFE_CLAUDE_SHIM_*) at every start; the flags here are written
# into that file. Changing a value later is an edit plus
# `python ops\shim_autostart.py restart claude` — no re-registration.
#   Claude models: claude-opus-5-5 (default), claude-opus-5, claude-sonnet-5-5,
#   claude-sonnet-5, claude-haiku-5-5, claude-haiku-4-5, claude-fable-5-1,
#   claude-fable-5 (the list ops\install.ps1 offers; any other
#   claude-* id passes to the shim unchanged)
#
# The shim wraps the Max-plan `claude` CLI as an OpenAI-compatible endpoint on
# 127.0.0.1 for the daemon's dream pass (primary extractor; the in-stack E4B
# container is the fallback — see docs/superpowers/specs/
# 2026-07-11-sonnet-sidecar-cutover-design.md). Requires a logged-in CLI.
# -Model default is claude-opus-5-5 since 2026-09-29: it clears the
# paired extraction-ladder gate with no regression against claude-opus-5
# (evals/results/ladder-opus55-paired-verdict-threshold.json, 2026-09-28).
# claude-opus-5 was the default from the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json: cortex 0.885 vs 0.821, 5/0), the
# judged comparison that chose Opus over Sonnet (best measured extraction quality);
# it ran on claude-opus-5.
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
    [string]$NativeExe = "",
    [string]$NativeTaskName = "",
    [int]$Port = 8082,
    [string]$Model = "claude-opus-5-5",
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
$LogFile = [IO.Path]::GetFullPath($LogFile, (Get-Location).ProviderPath)

. (Join-Path $PSScriptRoot "scheduled_task_lock.ps1")
# Native registration is shipped in the executable.
if ($NativeExe) {
    $NativeExe = (Get-Command $NativeExe -ErrorAction Stop).Source
    $nativeArgs = @("extractor-shim", "install-autostart", "claude", "--repo", $repo, "--native-exe", $NativeExe,
                    "--model", $Model, "--port", "$Port", "--host", "127.0.0.1", "--log", $LogFile)
    if ($NativeTaskName) { $nativeArgs += @("--name", $NativeTaskName) }
    $nativeArgs += @("--prompt-file", (Join-Path $repo $PromptFile), "--startup-timeout", "$StartupTimeoutSec")
    & $NativeExe @nativeArgs
    # 3: a task this shell may not change, refused before any change; the
    # native client printed the one line already.
    if ($LASTEXITCODE -eq 3) { throw (New-TaskLockError "the native client left the Claude shim autostart task unchanged (see the line above)") }
    if ($LASTEXITCODE -ne 0) { throw "native claude autostart registration failed" }
    return
}

$taskName = "Pseudolife Claude Shim"
$legacyTaskName = "Pseudolife Sonnet Shim"   # pre-rename installs
# >>> task lock check >>>
# Decide before changing anything, before the interpreter pick (which may
# build a venv) and the log directory. A task this shell cannot replace
# (one registered from an elevated shell, which Administrators own) is never
# re-registered. If it runs this checkout's ops\shim_autostart.py, enabled
# and at this user's logon, it reads ops\.env at every start: the settings
# are written there and the shim restarted, and the task keeps doing its
# job. Another account's task, and any other locked task, is refused with
# its reason and the one command that removes it. The legacy name is
# checked first: left behind beside the new task, it would start a second
# shim at logon.
$legacyLock = Get-TaskLock $legacyTaskName
if ($legacyLock) { throw (New-TaskLockError (Format-TaskLock $legacyLock -Removal)) }
$taskLock = Get-TaskLock $taskName
$followsFile = $false
if ($taskLock) {
    $mismatch = if ($taskLock.Hidden -or $taskLock.OtherUser) { '' } else { Get-TaskRunnerMismatch $taskName $repo 'claude' }
    if ($taskLock.Hidden -or $taskLock.OtherUser -or $mismatch) { throw (New-TaskLockError (Format-TaskLock $taskLock -Reason $mismatch)) }
    $followsFile = $true
}
# <<< task lock check <<<


$promptPath = Join-Path $repo $PromptFile
if (-not (Test-Path $promptPath)) { throw "prompt file not found: $promptPath" }
# >>> shim python >>>
# The task's interpreter must import pseudolife_memory with its
# dependencies (the shim imports the dream system prompt from it): the
# Linux twin registered a unit with a bare python3 that exited 1 in a
# restart loop (Debian 13, 2026-09-29). ops\shim_python.py picks one (the
# checkout's .venv, a venv it made earlier, a PATH python that imports the
# package, else a venv it creates from the checkout; never pipx's
# pseudolife-mcp venv here, which install.ps1 treats as held by a session
# while anything runs from it), verifies it the way the task will use it,
# and says which and why; -PythonExe names one to verify instead.
$helperPython = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $helperPython -and -not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "no python on PATH to run ops\shim_python.py, which picks the shim's interpreter; install Python 3.10+ and re-run."
}
$pickerArgs = @((Join-Path $PSScriptRoot "shim_python.py"), "--repo", $repo)
if ($PythonExe) { $pickerArgs += @("--python", $PythonExe) }
# The helper prints the path as UTF-8; this shell must decode it the same
# way, or a profile path with a non-ASCII character names an interpreter
# that does not exist (review, 2026-09-29).
$savedOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $picked = if ($helperPython) { & $helperPython @pickerArgs } else { & py -3 @pickerArgs }
} finally {
    [Console]::OutputEncoding = $savedOutputEncoding
}
if ($LASTEXITCODE -ne 0 -or -not $picked) {
    throw "shim autostart not registered: no interpreter imports pseudolife_memory for the task (see the message above; -PythonExe <interpreter> names one)."
}
$PythonExe = "$picked".Trim()
# <<< shim python <<<
# Absolute up front: the runner would otherwise resolve a relative -LogFile
# against its WorkingDirectory ($repo) while the verification below resolves
# it against this shell's location.
New-Item -ItemType Directory -Force (Split-Path -Parent $LogFile) | Out-Null

# Every process whose command line names claude_shim.py AND this --port. The
# shim is a two-layer tree (the .venv python.exe launcher -> the base
# interpreter that owns the socket) and neither layer's death propagates to
# the other, so the whole set is what "the running shim" means here. Keyed
# on the port too: an A/B shim serving another port from the same script
# must survive an install.
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

# -WindowStyle Hidden only hides a console window it still allocates — on
# Windows 11 with Windows Terminal set as the default terminal app, WT
# intercepts that console-allocation moment and opens a visible (blank)
# tab anyway, for the entire lifetime of the process (confirmed live:
# 2026-07-12, a real reboot showed a persistent blank WT tab owning the
# shim as its child). CreateNoWindow via .NET ProcessStartInfo skips
# console allocation entirely, so WT has nothing to attach a tab to —
# validated standalone (detached long-running child survives its spawner
# exiting; redirected output confirmed correct) before wiring in here.
# The scheduled task launches this tiny spawner, which starts the runner
# with CreateNoWindow (a hidden console the shim inherits) and returns
# immediately, so the Task-Scheduler-owned window is at most a sub-second
# flash rather than persisting for the shim's whole runtime.
#
# The task's only argument is the runner: the values live in ops/.env. The
# runner opens the log itself and starts the shim with the same hidden
# console (no `cmd >> log` layer: the process tree is the runner, gone in
# a second, then the shim's python).
if ($followsFile) {
    # Only the shim the runner recorded is stopped, proved by its PID file
    # (PID, start time, command line), never by a command-line pattern that
    # could match another checkout's or another account's shim. Anything
    # else on the port is refused before the settings are written.
    $recorded = @(Get-RecordedShim $repo 'claude')
    # What a failed restart puts back: the settings as they were.
    $envFile = Join-Path $repo 'ops\.env'
    $envBefore = if (Test-Path -LiteralPath $envFile -PathType Leaf) { [IO.File]::ReadAllBytes($envFile) } else { $null }
    # A legacy task this user owns is still removed, before the settings are
    # written, or it would start a second shim at logon.
    if (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
        if (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue) {
            throw "could not remove the legacy task '$legacyTaskName' (see the message above); nothing else was changed, and it would start a second shim at logon"
        }
    }
    $holder = Get-PortListener -ShimPort $Port
    if ($holder -and $holder.OwningProcess -notin @($recorded | ForEach-Object ProcessId)) {
        throw ("port $Port is held by pid $($holder.OwningProcess), which this run cannot prove is the shim it started " +
               "(ops\shim_autostart.py records that one in ~\.pseudolife-mcp); nothing was changed. Stop that process, then rerun")
    }
}
& $PythonExe (Join-Path $repo "ops\shim_autostart.py") config claude --model $Model --port $Port `
    --prompt-file $PromptFile --python $PythonExe --log $LogFile 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "could not write the shim settings into ops\.env (see above)" }
if ($followsFile) {
    # The registered task reads ops\.env at every start: restart the shim
    # here (no scheduler call), and leave the task as it is.
    $failure = $null
    try {
        Stop-RecordedShim $recorded
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-PortListener -ShimPort $Port) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
        if (Get-PortListener -ShimPort $Port) {
            throw "port $Port did not become free after stopping the recorded shim"
        }
        & $PythonExe (Join-Path $repo "ops\shim_autostart.py") run claude 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "the claude shim did not start (see above)" }
        # The runner returns once it has started the shim, not once it serves:
        # verify it as the registration path below does, by a listener a
        # claude_shim.py owns.
        $listener = $null
        $deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
        while (-not $listener -and (Get-Date) -lt $deadline) {
            $candidate = Get-PortListener -ShimPort $Port
            if ($candidate) {
                $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($candidate.OwningProcess)" -ErrorAction SilentlyContinue
                if ($owner -and $owner.CommandLine -match 'claude_shim\.py') { $listener = $candidate; break }
            }
            Start-Sleep -Seconds 2
        }
        if (-not $listener) {
            throw "no claude_shim.py listens on 127.0.0.1:$Port after ${StartupTimeoutSec}s; see $LogFile"
        }
    } catch { $failure = $_ }
    if ($failure) {
        # A failed restart leaves what was there: the previous settings,
        # and the previous shim started again on them.
        $restored = 'ops\.env was put back to its previous settings'
        try {
            # A shim the failed start left behind (the runner recorded it) goes
            # first, so the previous one does not bind beside it.
            Stop-RecordedShim @(Get-RecordedShim $repo 'claude')
            if ($null -ne $envBefore) { [IO.File]::WriteAllBytes($envFile, $envBefore) }
            elseif (Test-Path -LiteralPath $envFile) { Remove-Item -LiteralPath $envFile }
            if ($recorded.Count) {
                & $PythonExe (Join-Path $repo "ops\shim_autostart.py") run claude 2>&1 | Out-Host
                $restored += $(if ($LASTEXITCODE -eq 0) { ' and the previous shim was started again' } else { ', but the previous shim did not start again (see above)' })
            }
        } catch { $restored = "ops\.env could not be put back ($($_.Exception.Message)); it holds the new settings" }
        throw "$($failure.Exception.Message); $restored"
    }
    Write-Host "Restarted the Claude shim with the settings in ops\.env (port $Port, pid $($listener.OwningProcess)). $(Format-TaskLock $taskLock -Follows 'ops\.env')"
    & $PythonExe (Join-Path $repo "ops/shim_cutover.py") --repo $repo --port $Port
    if ($LASTEXITCODE -ne 0) { throw "could not report daemon cutover settings" }
    return
}
$inner = @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = '$($PythonExe -replace "'", "''")'
`$psi.Arguments = '$(("`"$repo\ops\shim_autostart.py`" run claude") -replace "'", "''")'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$psi.WorkingDirectory = '$($repo -replace "'", "''")'
[System.Diagnostics.Process]::Start(`$psi) | Out-Null
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
# This user's logon only, run as this user with a limited token: the shape
# an unelevated user may register (a trigger for any user's logon needs
# elevation).
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false -ErrorAction SilentlyContinue
# CIM cmdlet errors here do NOT reliably terminate even under
# $ErrorActionPreference = "Stop" (observed live: an unelevated run printed
# "Access is denied" and fell through to the success message). Force it, and
# verify the task actually exists before claiming success.
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Claude extractor CLI shim (dream pass primary; E4B sidecar is fallback)" `
    -ErrorAction Stop | Out-Null
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    throw ("task '$taskName' was not registered: Task Scheduler refused it (see the message above). " +
           "No elevation is needed for this per-user task; run this script again in a normal " +
           "PowerShell 7 window, and if it still fails, check that the ScheduledTasks module loads " +
           "(Get-Command Register-ScheduledTask).")
}

# Stop the shim that is already serving this port BEFORE starting the task.
# Re-registering over a live shim used to "succeed" while the old instance
# kept the port (2026-09-06, v2 -> v4 prompt cutover): on Windows a second
# http.server listener BINDS beside the first (allow_reuse_address is
# SO_REUSEADDR, which shares a port in LISTEN), so the new interpreter never
# hit an error to log, and its startup lines were then overwritten by the old
# shim's next log write (two appending opens keep independent file
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
Write-Host "Registered + started '$taskName' ($Model, port $Port, pid $($listener.OwningProcess), python $PythonExe, log $LogFile)."
Write-Host "To change the model, prompt file, port or CLI later: edit the PSEUDOLIFE_CLAUDE_SHIM_* lines in ops\.env, then run: python ops\shim_autostart.py restart claude."
# >>> shim cutover >>>
& $PythonExe (Join-Path $repo "ops/shim_cutover.py") --repo $repo --port $Port
if ($LASTEXITCODE -ne 0) { throw "could not report daemon cutover settings" }
# <<< shim cutover <<<
