#Requires -Version 7
# Register the Sonnet extractor shim to start at logon (Windows Task Scheduler).
# (Install under pwsh 7 — the ternary below needs it; the scheduled task itself
# runs fine under powershell.exe 5.1.)
#
#   ops\install-shim-autostart.ps1              # default port 8082, v1 prompt
#   ops\install-shim-autostart.ps1 -Port 8082 -PromptFile evals\prompts\sonnet_extractor_v1.md
#
# The shim wraps the Max-plan `claude` CLI as an OpenAI-compatible endpoint on
# 127.0.0.1 for the daemon's dream pass (primary extractor; the in-stack E4B
# container is the fallback — see docs/superpowers/specs/
# 2026-07-11-sonnet-sidecar-cutover-design.md). Requires a logged-in CLI.
param(
    [string]$PythonExe = "",
    [int]$Port = 8082,
    [string]$PromptFile = "evals\prompts\sonnet_extractor_v1.md",
    [string]$LogFile = "$env:USERPROFILE\.pseudolife-mcp\sonnet-shim.log"
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

$taskName = "PseudoLife Sonnet Shim"
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
$innerCmd = "`"$PythonExe`" `"$repo\evals\sonnet_shim.py`" --port $Port " +
            "--system-prompt-file `"$promptPath`""
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
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Sonnet extractor CLI shim (dream pass primary; E4B sidecar is fallback)" | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Registered + started '$taskName' (port $Port, log $LogFile)."
Write-Host "Cutover env for the daemon (.env or compose override):"
Write-Host "  PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$Port/v1"
Write-Host "  PSEUDOLIFE_DREAM_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1"
Write-Host "  PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor"
Write-Host "  PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto"
