#Requires -Version 7
# Register the Codex extractor shim to start at logon (Windows Task
# Scheduler) — the ChatGPT-plan twin of ops\install-shim-autostart.ps1.
# (Install under pwsh 7 — the ternary below needs it; the scheduled task
# itself runs fine under powershell.exe 5.1.)
#
# No elevation: run this from a normal PowerShell 7 window. The task is
# registered for this user only (logon of this user, Interactive,
# -RunLevel Limited), which an unelevated user may register, replace and
# remove; a task registered from an elevated shell is owned by
# Administrators, and this script then changes nothing and prints the one
# elevated command that removes it (full note in install-shim-autostart.ps1).
# Never request UAC elevation from a shell inside Claude Desktop or another
# Store-packaged app: that app's next update then fails to launch until a
# reboot (anthropics/claude-code#61635).
#
#   ops\install-codex-shim-autostart.ps1                      # port 8086, terra
#   ops\install-codex-shim-autostart.ps1 -Model gpt-5.6-sol   # pick the served model
#
# The task runs `ops\shim_autostart.py run codex`, which starts
# evals/codex_shim.py with the model, port, health TTL (--health-ttl), host,
# CLI path, interpreter and log file read from ops/.env
# (PSEUDOLIFE_CODEX_SHIM_*) at every start; the flags here are written into
# that file. To change a value later, rerun this script with the new flags
# (it rewrites that file and restarts the shim it proves is its own), or
# edit the file and run `python ops\shim_autostart.py restart codex`, which
# stops every codex_shim.py on that port this account may stop.
#   OpenAI models: gpt-5.6-terra (default), gpt-5.6-sol, gpt-5.6-luna, gpt-6-sol,
#   gpt-6-luna, gpt-6.1-sol (the list ops\install.ps1 offers; the GPT-6 ids were
#   accepted by the Codex CLI on 2026-09-29, gpt-6.1-sol on 2026-11-08 by
#   Codex CLI 0.165.0; any other id passes to the shim unchanged)
#
# The shim wraps the ChatGPT-plan `codex` CLI as an OpenAI-compatible endpoint
# on 127.0.0.1 for the daemon's dream pass. Requires a signed-in CLI
# (`codex login` once, interactively). Extraction quality is UNMEASURED for
# Codex-served models — run `evals/ladder_sweep.py --rung terra` before
# trusting one with consolidation. No -PromptFile parameter on purpose: the
# v2 extraction-prompt override is Sonnet-tuned, so the codex shim runs the
# production prompt until a ladder run measures a Codex variant.
#
# -HealthTtl default is 1800s (not the shim's own 300s): every /health
# refresh is a real CLI call, which is metered spend on a free ChatGPT tier
# (300s ≈ 288 calls/day; 1800s ≈ 48 — arithmetic, not a measured quota; no
# free-tier limit has been measured, so revisit if one surfaces). A
# stale-ok window only costs one failed primary attempt before the dream
# falls back.
param(
    [string]$PythonExe = "",
    [string]$NativeExe = "",
    [string]$NativeTaskName = "",
    [int]$Port = 8086,
    [string]$Model = "gpt-5.6-terra",
    [int]$HealthTtl = 1800,
    [string]$CodexCli = "",
    [string]$LogFile = "$env:USERPROFILE\.pseudolife-mcp\codex-shim.log",
    # How long to wait for the started shim to bind the port. Like the
    # Claude twin it warms its health cache with one real CLI call before it
    # binds; the same 90 s budget, not measured for the Codex CLI.
    [int]$StartupTimeoutSec = 90
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$LogFile = [IO.Path]::GetFullPath($LogFile, (Get-Location).ProviderPath)

. (Join-Path $PSScriptRoot "scheduled_task_lock.ps1")
# Native registration is shipped in the executable.
if ($NativeExe) {
    $NativeExe = (Get-Command $NativeExe -ErrorAction Stop).Source
    $nativeArgs = @("extractor-shim", "install-autostart", "codex", "--repo", $repo, "--native-exe", $NativeExe,
                    "--model", $Model, "--port", "$Port", "--host", "127.0.0.1", "--log", $LogFile)
    if ($NativeTaskName) { $nativeArgs += @("--name", $NativeTaskName) }
    $nativeArgs += @("--health-ttl", "$HealthTtl")
    if ($CodexCli) { $nativeArgs += @("--cli", $CodexCli) }
    & $NativeExe @nativeArgs
    # 3: a task this shell may not change, refused before any change; the
    # native client printed the one line already.
    if ($LASTEXITCODE -eq 3) { throw (New-TaskLockError "the native client left the Codex shim autostart task unchanged (see the line above)") }
    if ($LASTEXITCODE -ne 0) { throw "native codex autostart registration failed" }
    return
}

$taskName = "Pseudolife Codex Shim"
# >>> task lock check >>>
# Decide before changing anything: before the log directory, the
# interpreter pick (which may build a venv), ops\.env and the task. A task
# this shell cannot replace (Administrators' or another account's) is
# refused with the one command that removes it, and so is one whose owner
# cannot be read. A port held by anything but the shim the runner recorded
# (its PID file, proved by PID, start time and command line) is refused
# too: on Windows a second shim cannot bind beside it, so the task would
# report started while the old one kept serving.
function Get-PortListeners([int]$ShimPort) {
    @(Get-NetTCPConnection -LocalPort $ShimPort -State Listen -ErrorAction SilentlyContinue)
}
function Get-PortListener([int]$ShimPort) { Get-PortListeners $ShimPort | Select-Object -First 1 }
Assert-TaskUnlocked $taskName
$recorded = @(Get-RecordedShim $repo 'codex')
$holder = Get-UnprovenPortHolder $Port $recorded 'codex'
if ($holder) { throw (New-TaskLockError $holder) }
# <<< task lock check <<<


# Fail fast if no codex CLI is findable — but do NOT bake a discovered path
# into the task: the official installer keeps codex.exe in a rotating
# %LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\ dir (off PATH) that changes on
# every auto-update, and the shim re-resolves it fresh at each start. Only
# an explicit -CodexCli is pinned through.
if (-not $CodexCli) {
    $onPath = Get-Command codex -ErrorAction SilentlyContinue
    $glob = Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe" `
        -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $onPath -and -not $glob) {
        throw ("codex CLI not found (PATH or %LOCALAPPDATA%\OpenAI\Codex\bin) — " +
               "install Codex and run `codex login`: https://developers.openai.com/codex/cli/")
    }
}

# >>> shim python >>>
# The task's interpreter must import pseudolife_memory with its
# dependencies (the shim imports the dream system prompt from it): the
# Linux twin registered this unit with a bare python3 that exited 1 in a
# restart loop (Debian 13, 2026-09-29). ops\shim_python.py picks one (the
# checkout's .venv, a venv it made earlier, a PATH python that imports the
# package, else a venv it creates from the checkout; never pipx's
# pseudolife-mcp venv here, which install.ps1 treats as held by a session
# while anything runs from it), verifies it the way the task will use it,
# and says which and why; -PythonExe names one to verify instead. After
# the CLI check: a missing CLI fails faster than a venv build.
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
New-Item -ItemType Directory -Force (Split-Path -Parent $LogFile) | Out-Null

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

# Same detached CreateNoWindow spawner as the Claude shim task — see the
# comment block in install-shim-autostart.ps1 for why (Windows Terminal
# otherwise pins a visible blank tab to the shim for its whole runtime).
# The task's only argument is the runner: the values live in ops/.env. An
# explicit -CodexCli is written there; without one the shim re-resolves
# the rotating %LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\ path at each start.
$configArgs = @("config", "codex", "--model", $Model, "--port", "$Port", "--health-ttl", "$HealthTtl",
                "--python", $PythonExe, "--log", $LogFile)
if ($CodexCli) { $configArgs += @("--cli", $CodexCli) }
& $PythonExe (Join-Path $repo "ops\shim_autostart.py") @configArgs 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "could not write the shim settings into ops\.env (see above)" }
$inner = @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = '$($PythonExe -replace "'", "''")'
`$psi.Arguments = '$(("`"$repo\ops\shim_autostart.py`" run codex") -replace "'", "''")'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$psi.WorkingDirectory = '$($repo -replace "'", "''")'
[System.Diagnostics.Process]::Start(`$psi) | Out-Null
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
# This user's logon only, run as this user with a limited token: the shape
# an unelevated user may register.
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# Again just before the unregister: the interpreter pick above can take
# minutes, and a task another account registered meanwhile is not ours.
Assert-TaskUnlocked $taskName
Unregister-ScheduledTask -TaskPath '\' -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
# CIM cmdlet errors here do NOT reliably terminate even under
# $ErrorActionPreference = "Stop" (observed live on the Claude twin: an
# unelevated run printed "Access is denied" and fell through to the success
# message). Force it, and verify the task actually exists before claiming
# success.
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Codex extractor CLI shim (dream pass primary; E4B sidecar is fallback)" `
    -ErrorAction Stop | Out-Null
if (-not (Get-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction SilentlyContinue)) {
    throw ("task '$taskName' was not registered: Task Scheduler refused it (see the message above). " +
           "No elevation is needed for this per-user task; run this script again in a normal " +
           "PowerShell 7 window, and if it still fails, check that the ScheduledTasks module loads " +
           "(Get-Command Register-ScheduledTask).")
}

# Stop the shim already serving this port BEFORE starting the task: on
# Windows the new instance cannot bind beside it, so the old one would keep
# serving the old settings. Only the shim the runner recorded is stopped,
# proved before anything changed (the task lock check block), and only
# after the registration succeeded.
if ($recorded.Count) {
    Write-Host ("Stopping the running codex_shim.py the runner recorded (pid " + (($recorded | ForEach-Object ProcessId) -join ', ') +
                ") so the new task instance can take the port...")
    Stop-RecordedShim $recorded
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-PortListener -ShimPort $Port) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
}
$holder = Get-PortListener -ShimPort $Port
if ($holder) {
    throw ("port $Port is still held by pid $($holder.OwningProcess) after stopping the recorded shim; the task is registered " +
           "but not started. Stop that process, then run: Start-ScheduledTask -TaskPath '\' -TaskName '$taskName'")
}

$logOffset = (Test-Path $LogFile) ? (Get-Item $LogFile).Length : 0
Start-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction Stop
Write-Host "Started '$taskName'; waiting up to ${StartupTimeoutSec}s for the shim to bind 127.0.0.1:$Port (health warm-up is one real CLI call)..."
# Affirmative verification: a listener on the port OWNED BY a codex_shim.py
# process. "The task ran" (LastTaskResult 0) only means the spawner exited.
$listener = $null
$deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
while (-not $listener -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $candidate = Get-PortListener -ShimPort $Port
    if ($candidate) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($candidate.OwningProcess)" -ErrorAction SilentlyContinue
        if ($owner -and $owner.CommandLine -match 'codex_shim\.py') { $listener = $candidate }
    }
}
$newLines = @(Read-LogSince -Path $LogFile -Offset $logOffset)
if (-not $listener) {
    $info = Get-ScheduledTaskInfo -TaskPath '\' -TaskName $taskName -ErrorAction SilentlyContinue
    $tail = if ($newLines.Count -gt 0) { $newLines -join "`n" } else {
        "(none - nothing reached $LogFile; check the CLI login with: codex login status)" }
    throw ("shim did not bind 127.0.0.1:$Port within ${StartupTimeoutSec}s " +
           "(task LastTaskResult $($info.LastTaskResult)). New log lines:`n$tail")
}
foreach ($line in @($newLines | Where-Object { $_ -match 'serving .* on |health warm|Traceback|Error' })) { Write-Host "  log: $line" }
Write-Host "Registered + started '$taskName' ($Model, port $Port, pid $($listener.OwningProcess), health-ttl ${HealthTtl}s, python $PythonExe, log $LogFile)."
Write-Host "To change the model, port, health TTL or CLI later: rerun this script with the new flags, or edit the PSEUDOLIFE_CODEX_SHIM_* lines in ops\.env and run python ops\shim_autostart.py restart codex (which stops every codex_shim.py on that port this account may stop)."
Write-Host "Cutover env for the daemon (.env or compose override):"
Write-Host "  PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$Port/v1"
Write-Host "  PSEUDOLIFE_DREAM_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"
