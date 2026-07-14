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

# Backup before writing (once, before any mutations).
if (Test-Path $SettingsPath) {
    $bak = "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $SettingsPath $bak
    Write-Host "Backed up -> $bak"
}

# Idempotency: check briefing hook independently.
$hasBriefing = $false
foreach ($group in @($obj.hooks.SessionStart)) {
    foreach ($h in @($group.hooks)) {
        if ($h.command -like "*pseudolife-mcp briefing*") { $hasBriefing = $true }
    }
}
if (-not $hasBriefing) {
    # Append a NEW SessionStart group (leaves existing groups + hooks intact).
    $briefingGroup = [pscustomobject]@{
        hooks = @([pscustomobject]@{ type = 'command'; command = $Command })
    }
    $obj.hooks.SessionStart = @($obj.hooks.SessionStart) + $briefingGroup
    Write-Host "Installed SessionStart briefing hook -> $SettingsPath"
    Write-Host "  command: $Command"
} else {
    Write-Host "Briefing hook already present in $SettingsPath - skipping."
}

# ---- episode lifecycle hooks (idempotent, alongside the briefing hook) ----
if (-not ($obj.hooks.PSObject.Properties.Name -contains 'SessionEnd')) {
    $obj.hooks | Add-Member -NotePropertyName SessionEnd -NotePropertyValue @()
}

$hasStart = $false
foreach ($group in @($obj.hooks.SessionStart)) {
    foreach ($h in @($group.hooks)) {
        if ($h.command -like "*pseudolife-mcp episode-start*") { $hasStart = $true }
    }
}
if (-not $hasStart) {
    $startGroup = [pscustomobject]@{
        hooks = @([pscustomobject]@{ type = 'command';
            command = 'pseudolife-mcp episode-start' })
    }
    $obj.hooks.SessionStart = @($obj.hooks.SessionStart) + $startGroup
    Write-Host "Installed SessionStart episode-start hook."
} else {
    Write-Host "episode-start hook already present - skipping."
}

$hasEnd = $false
foreach ($group in @($obj.hooks.SessionEnd)) {
    foreach ($h in @($group.hooks)) {
        if ($h.command -like "*pseudolife-mcp episode-end*") { $hasEnd = $true }
    }
}
if (-not $hasEnd) {
    $endGroup = [pscustomobject]@{
        hooks = @([pscustomobject]@{ type = 'command';
            command = 'pseudolife-mcp episode-end' })
    }
    $obj.hooks.SessionEnd = @($obj.hooks.SessionEnd) + $endGroup
    Write-Host "Installed SessionEnd episode-end hook."
} else {
    Write-Host "episode-end hook already present - skipping."
}

$obj | ConvertTo-Json -Depth 30 | Set-Content -Path $SettingsPath -Encoding utf8

# The hooks wire the session lifecycle, but the memory LOOP only fires if a
# standing instruction tells the agent to use the tools (issue #12: an install
# with healthy hooks + daemon still never called memory_* because no CLAUDE.md
# carried the block). Check-and-advise only — never edit CLAUDE.md unasked.
$repo = Split-Path -Parent $PSScriptRoot
$claudeMd = Join-Path (Split-Path -Parent $SettingsPath) "CLAUDE.md"
$hasBlock = (Test-Path $claudeMd) -and
    ((Get-Content $claudeMd -Raw) -match 'pseudolife-memory')
if (-not $hasBlock) {
    Write-Host ""
    Write-Warning "$claudeMd has no PseudoLife memory section - without a standing instruction the memory tools sit unused. Append the bundled block:"
    Write-Host "  Add-Content `"$claudeMd`" (Get-Content `"$repo\examples\CLAUDE.memory.md`" -Raw)"
    Write-Host "(or add it to a per-project CLAUDE.md / AGENTS.md instead)"
}
