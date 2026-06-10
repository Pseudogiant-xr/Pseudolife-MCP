# Register the PseudoLife-MCP daemon to start at logon (Windows Task Scheduler).
#
#   ops\install-autostart.ps1                 # uses the repo .venv or system python
#   ops\install-autostart.ps1 -Token "secret" # also export a LAN token
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

$taskName = "PseudoLife-MCP Daemon"
$envPrefix = "`$env:PSEUDOLIFE_MCP_HOST='$BindHost'; " +
             "`$env:PSEUDOLIFE_MCP_PORT='$Port'; " +
             "`$env:PSEUDOLIFE_MCP_DATABASE_URL='$DatabaseUrl'; " +
             "`$env:PSEUDOLIFE_MCP_DATA_DIR='$DataDir'; "
if ($Token) { $envPrefix += "`$env:PSEUDOLIFE_MCP_TOKEN='$Token'; " }

$inner = "$envPrefix & '$PythonExe' -m pseudolife_memory.cli serve"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Force -Description "PseudoLife-MCP memory daemon" | Out-Null

Write-Host "Registered '$taskName' (logon) -> $PythonExe -m pseudolife_memory.cli serve"
Write-Host "Start now with:  Start-ScheduledTask -TaskName '$taskName'"
