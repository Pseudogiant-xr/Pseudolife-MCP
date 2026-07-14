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

# Episode hooks are OBSOLETE since the 2026-06-30 session-scoped episodes
# rework: the daemon lazily opens/closes episodes keyed by mcp-session-id
# (see README "Session lifecycle hooks"). Earlier installer versions added
# them — remove any we find so old installs converge too.
function Remove-HookCommand($groups, $needle) {
    $removed = $false
    $keptGroups = @()
    foreach ($group in @($groups)) {
        if ($null -eq $group) { continue }
        $keptHooks = @(@($group.hooks) | Where-Object { $_.command -notlike "*$needle*" })
        if ($keptHooks.Count -ne @($group.hooks).Count) { $removed = $true }
        if ($keptHooks.Count -gt 0) {
            $group.hooks = $keptHooks
            $keptGroups += $group
        }
    }
    return @{ removed = $removed; groups = $keptGroups }
}

$r = Remove-HookCommand $obj.hooks.SessionStart "pseudolife-mcp episode-start"
$obj.hooks.SessionStart = $r.groups
if ($r.removed) { Write-Host "Removed obsolete episode-start hook (daemon owns episodes now)." }

if ($obj.hooks.PSObject.Properties.Name -contains 'SessionEnd') {
    $r = Remove-HookCommand $obj.hooks.SessionEnd "pseudolife-mcp episode-end"
    $obj.hooks.SessionEnd = $r.groups
    if ($r.removed) { Write-Host "Removed obsolete episode-end hook (daemon owns episodes now)." }
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
