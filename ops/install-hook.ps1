# Idempotently add the PseudoLife-MCP session-start briefing to Claude Code's
# SessionStart hooks, ALONGSIDE (never replacing) any existing hooks.
#
#   ops\install-hook.ps1
#   ops\install-hook.ps1 -SettingsPath C:\path\to\settings.json
#
# Backs up settings.json first; re-running is a no-op once installed. Adds a new
# SessionStart group so existing hooks (e.g. the static "memory enabled" reminder)
# are left untouched. Requires PowerShell 7+ (UTF-8 no-BOM JSON write).
param(
    [string]$SettingsPath = (Join-Path $env:USERPROFILE ".claude\settings.json"),
    [string]$Command = "pseudolife-mcp briefing --hook-json"
)
$ErrorActionPreference = "Stop"

# Load existing settings, or start a minimal object.
if (Test-Path $SettingsPath) {
    $obj = Get-Content $SettingsPath -Raw | ConvertFrom-Json
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SettingsPath) | Out-Null
    $obj = [pscustomobject]@{}
}

# Ensure hooks.SessionStart exists.
if (-not ($obj.PSObject.Properties.Name -contains 'hooks')) {
    $obj | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{})
}
if (-not ($obj.hooks.PSObject.Properties.Name -contains 'SessionStart')) {
    $obj.hooks | Add-Member -NotePropertyName SessionStart -NotePropertyValue @()
}

# Idempotency: bail if a briefing hook is already present.
foreach ($group in @($obj.hooks.SessionStart)) {
    foreach ($h in @($group.hooks)) {
        if ($h.command -like "*pseudolife-mcp briefing*") {
            Write-Host "Briefing hook already present in $SettingsPath - nothing to do."
            return
        }
    }
}

# Backup before writing.
if (Test-Path $SettingsPath) {
    $bak = "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $SettingsPath $bak
    Write-Host "Backed up -> $bak"
}

# Append a NEW SessionStart group (leaves existing groups + hooks intact).
$group = [pscustomobject]@{
    hooks = @([pscustomobject]@{ type = 'command'; command = $Command; shell = 'bash' })
}
$obj.hooks.SessionStart = @($obj.hooks.SessionStart) + $group

$obj | ConvertTo-Json -Depth 30 | Set-Content -Path $SettingsPath -Encoding utf8
Write-Host "Installed SessionStart briefing hook -> $SettingsPath"
Write-Host "  command: $Command"
