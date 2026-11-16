#Requires -Version 7
# Register the Pseudolife-MCP daemon to start at logon (Windows Task Scheduler).
#
#   ops\install-autostart.ps1                 # uses the repo .venv or system python
#   ops\install-autostart.ps1 -Token "secret" # also export a LAN token
#
# No elevation: the task is registered for this user's logon only, run as
# this user with a limited token, which an unelevated user may register,
# replace and remove. A task of that name registered from an elevated shell
# is owned by Administrators: this script then changes nothing and prints
# the one elevated command that removes it.
#
# The daemon binds 127.0.0.1:8765 by default. Pass -Host 0.0.0.0 -Token <t>
# to expose it to the LAN (the daemon refuses non-loopback without a token).
param(
    [string]$PythonExe = "",
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765,
    [string]$DatabaseUrl = "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
    [string]$DataDir = "$env:USERPROFILE\.pseudolife-mcp",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

if (-not $PythonExe) {
    $venv = Join-Path $repo ".venv\Scripts\python.exe"
    $PythonExe = (Test-Path $venv) ? $venv : (Get-Command python).Source
}

$taskName = "Pseudolife-MCP Daemon"
. (Join-Path $PSScriptRoot "scheduled_task_lock.ps1")
$lock = Get-TaskLock $taskName
if ($lock) { throw (Format-TaskLock $lock) }
$envPrefix = "`$env:PSEUDOLIFE_MCP_HOST='$BindHost'; " +
             "`$env:PSEUDOLIFE_MCP_PORT='$Port'; " +
             "`$env:PSEUDOLIFE_MCP_DATABASE_URL='$DatabaseUrl'; " +
             "`$env:PSEUDOLIFE_MCP_DATA_DIR='$DataDir'; " +
             "`$env:CUDA_VISIBLE_DEVICES='-1'; "  # CPU-only contract
if ($Token) { $envPrefix += "`$env:PSEUDOLIFE_MCP_TOKEN='$Token'; " }

$inner = "$envPrefix & '$PythonExe' -m pseudolife_memory.cli serve"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force -Description "Pseudolife-MCP memory daemon" | Out-Null

Write-Host "Registered '$taskName' (logon) -> $PythonExe -m pseudolife_memory.cli serve"
Write-Host "Start now with:  Start-ScheduledTask -TaskName '$taskName'"
