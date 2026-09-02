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
#   ops\install-shim-autostart.ps1              # default port 8082, v2 prompt, opus
#   ops\install-shim-autostart.ps1 -Model claude-sonnet-5   # pick the served model
#
# The shim wraps the Max-plan `claude` CLI as an OpenAI-compatible endpoint on
# 127.0.0.1 for the daemon's dream pass (primary extractor; the in-stack E4B
# container is the fallback — see docs/superpowers/specs/
# 2026-07-11-sonnet-sidecar-cutover-design.md). Requires a logged-in CLI.
# -Model default is claude-opus-5 per the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json: cortex 0.885 vs 0.821, 5/0).
param(
    [string]$PythonExe = "",
    [int]$Port = 8082,
    [string]$Model = "claude-opus-5",
    [string]$PromptFile = "evals\prompts\sonnet_extractor_v2.md",
    [string]$LogFile = "$env:USERPROFILE\.pseudolife-mcp\claude-shim.log"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if (-not $PythonExe) {
    $venv = Join-Path $repo ".venv\Scripts\python.exe"
    $PythonExe = (Test-Path $venv) ? $venv : (Get-Command python).Source
}
$promptPath = Join-Path $repo $PromptFile
if (-not (Test-Path $promptPath)) { throw "prompt file not found: $promptPath" }
New-Item -ItemType Directory -Force (Split-Path -Parent $LogFile) | Out-Null

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
Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
Write-Host "Registered + started '$taskName' ($Model, port $Port, log $LogFile)."
Write-Host "Cutover env for the daemon (.env or compose override):"
Write-Host "  PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$Port/v1"
Write-Host "  PSEUDOLIFE_DREAM_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"
