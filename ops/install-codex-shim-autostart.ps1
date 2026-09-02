#Requires -Version 7
# Register the Codex extractor shim to start at logon (Windows Task
# Scheduler) — the ChatGPT-plan twin of ops\install-shim-autostart.ps1.
# (Install under pwsh 7 — the ternary below needs it; the scheduled task
# itself runs fine under powershell.exe 5.1.)
#
# Needs an ELEVATED PowerShell (Task Scheduler refuses unelevated per-user
# registration on administrator accounts). Open it fresh from the Start
# menu - never request the UAC elevation from a shell inside Claude Desktop
# or another Store-packaged app: that app's next update then fails to
# launch until a reboot (anthropics/claude-code#61635; full note in
# install-shim-autostart.ps1). This very installer, elevated from a Claude
# Desktop session on 2026-08-31, is the most likely trigger of the
# 2026-09-02 reproduction (inferred from timing, not proven).
#
#   ops\install-codex-shim-autostart.ps1                      # port 8086, terra
#   ops\install-codex-shim-autostart.ps1 -Model gpt-5.6-sol   # pick the served model
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
    [int]$Port = 8086,
    [string]$Model = "gpt-5.6-terra",
    [int]$HealthTtl = 1800,
    [string]$CodexCli = "",
    [string]$LogFile = "$env:USERPROFILE\.pseudolife-mcp\codex-shim.log"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if (-not $PythonExe) {
    $venv = Join-Path $repo ".venv\Scripts\python.exe"
    $PythonExe = (Test-Path $venv) ? $venv : (Get-Command python).Source
}
New-Item -ItemType Directory -Force (Split-Path -Parent $LogFile) | Out-Null

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

$taskName = "Pseudolife Codex Shim"
# Same detached CreateNoWindow spawner as the Claude shim task — see the
# comment block in install-shim-autostart.ps1 for why (Windows Terminal
# otherwise pins a visible blank tab to the shim for its whole runtime).
$innerCmd = "`"$PythonExe`" `"$repo\evals\codex_shim.py`" --port $Port " +
            "--model $Model --health-ttl $HealthTtl"
if ($CodexCli) { $innerCmd += " --cli `"$CodexCli`"" }
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
# CIM cmdlet errors here do NOT reliably terminate even under
# $ErrorActionPreference = "Stop" (observed live on the Claude twin: an
# unelevated run printed "Access is denied" and fell through to the success
# message). Force it, and verify the task actually exists before claiming
# success.
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Codex extractor CLI shim (dream pass primary; E4B sidecar is fallback)" `
    -ErrorAction Stop | Out-Null
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    throw ("task '$taskName' was not registered - Task Scheduler needs an ELEVATED " +
           "PowerShell on administrator accounts. Open it fresh from the Start menu; " +
           "never elevate from a shell inside Claude Desktop or another Store-packaged " +
           "app (its next update then fails to launch until a reboot - " +
           "anthropics/claude-code#61635).")
}
Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
Write-Host "Registered + started '$taskName' ($Model, port $Port, health-ttl ${HealthTtl}s, log $LogFile)."
Write-Host "Cutover env for the daemon (.env or compose override):"
Write-Host "  PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$Port/v1"
Write-Host "  PSEUDOLIFE_DREAM_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"
