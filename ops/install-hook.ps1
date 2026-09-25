#Requires -Version 7
# ^ enforced, not just documented: 5.1's Set-Content writes a BOM into
#   settings.json.
# Idempotently add the Pseudolife-MCP session-start briefing to Claude Code or
# Codex SessionStart hooks, ALONGSIDE (never replacing) existing hooks.
#
#   ops\install-hook.ps1
#   ops\install-hook.ps1 -Client codex
#   ops\install-hook.ps1 -SettingsPath C:\path\to\settings.json
#   ops\install-hook.ps1 -RemoveLegacy [-DryRun] [-SettingsPath ...]
#
# Backs up settings.json first; re-running is a no-op once installed. Adds a new
# SessionStart group so existing hooks (e.g. the static "memory enabled" reminder)
# are left untouched. Requires PowerShell 7+ (UTF-8 no-BOM JSON write).
#
# -RemoveLegacy removes the Claude Code hooks this script and the installers
# wrote, once the pseudolife-memory plugin provides them. It removes only exact
# installer-written commands, only while the plugin is installed and enabled
# for all projects, and backs up settings.json first. -DryRun lists them
# without writing. Exit: 0 found (dry run) or removed, 3 none found, 4 plugin
# not active for all projects, 1 error, 2 usage.
param(
    [ValidateSet("claude", "codex")]
    [string]$Client = "claude",
    [string]$SettingsPath = "",
    [string]$Command = "pseudolife-mcp briefing --hook-json",
    [switch]$RemoveLegacy,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

if (-not $SettingsPath) {
    $SettingsPath = if ($Client -eq "codex") {
        Join-Path $env:USERPROFILE ".codex\hooks.json"
    } else {
        Join-Path $env:USERPROFILE ".claude\settings.json"
    }
}

# Board setup stays separate from the daemon-backed memory briefing; the
# discipline line is the every-turn reminder (see the UserPromptSubmit step).
$coordinationLine = "Pseudolife coordination: at the first task and on resume, use memory_agents(action=list), then memory_agents(action=update, project=<project>, task=<task>, status=<status>) to show scope. Use memory_message(action=receive); read each full message and memory_message(action=ack, message_id=<id>) after reading. On a pending-message hint, receive again. If unavailable, report that and continue independently."
$disciplineLine = "Memory (PseudoLife) mid-session discipline: before reviewing code, docs, or a PR -> memory_search + memory_lesson_search the target area FIRST, then compare memory against the files and correct drift both ways (fix stale memory via memory_fact_set + memory_outcome; treat memory-vs-file mismatches as review findings). Status or in-progress questions -> memory_search (include sources: status) before or alongside git. Starting work in a new area -> memory_search + memory_lesson_search first. Launching or finishing long-running work -> memory_store a status entry. Outcome landed -> memory_outcome with used_ids."

if ($DryRun -and -not $RemoveLegacy) {
    [Console]::Error.WriteLine("-DryRun applies to -RemoveLegacy only.")
    exit 2
}
if ($RemoveLegacy) {
    if ($Client -ne "claude") {
        [Console]::Error.WriteLine("-RemoveLegacy is for Claude Code; ops/setup-codex-hooks.py migrates Codex hooks.")
        exit 2
    }
    # .NET file calls resolve relative paths against the process directory,
    # not the PowerShell location.
    $SettingsPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($SettingsPath)
    $pluginId = "pseudolife-memory@pseudolife-mcp"
    # Exact commands this script and the installers ever wrote, by event. A
    # substring match would also delete a user's own command that merely
    # mentions one (ops/setup-codex-hooks.py applies the same rule). The
    # 2026-08-28..09-05 discipline line ended "-> memory_outcome."; derived,
    # so this file keeps one copy of the line (test_plugin_packaging.py).
    $shipped = [ordered]@{
        SessionStart = @("pseudolife-mcp briefing --hook-json",
            "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json",
            "echo '$coordinationLine'", "pseudolife-mcp episode-start")
        UserPromptSubmit = @("echo '$disciplineLine'",
            "echo '$($disciplineLine -replace ' with used_ids\.$', '.')'")
        SessionEnd = @("pseudolife-mcp episode-end")
    }
    $needles = @("pseudolife-mcp briefing", "mid-session discipline", "Pseudolife coordination:",
        "pseudolife-mcp episode-start", "pseudolife-mcp episode-end")
    function Get-JsonString($node) {
        # A JSON string value, or $null for anything else (ToString alone
        # would also render numbers and objects as text).
        if (($node -is [System.Text.Json.Nodes.JsonValue]) -and $node.ToJsonString().StartsWith('"')) {
            return $node.ToString()
        }
        return $null
    }
    function Get-Shipped($eventName) {
        foreach ($name in $shipped.Keys) { if ($name -ceq $eventName) { return $shipped[$name] } }
        return @()
    }
    function Test-ShippedHook($eventName, $hook) {
        if ($hook -isnot [System.Text.Json.Nodes.JsonObject]) { return $false }
        $known = @(Get-Shipped $eventName)
        $command = Get-JsonString $hook["command"]
        if (((Get-JsonString $hook["type"]) -cne "command") -or -not ($known -ccontains $command)) { return $false }
        return (-not $hook.ContainsKey("commandWindows")) -or
            ($known -ccontains (Get-JsonString $hook["commandWindows"]))
    }
    function Format-Hook($eventName, $command) {
        $command = ($command -split '\s+' | Where-Object { $_ }) -join ' '
        if ($command.Length -gt 100) { $command = $command.Substring(0, 97) + "..." }
        return "  ${eventName}: $command"
    }
    function Test-PluginActive($settings) {
        $enabled = $settings["enabledPlugins"]
        if (($enabled -isnot [System.Text.Json.Nodes.JsonObject]) -or
            ($null -eq $enabled[$pluginId]) -or ($enabled[$pluginId].ToJsonString() -cne "true")) {
            return $false
        }
        $record = Join-Path (Split-Path -Parent ([IO.Path]::GetFullPath($SettingsPath))) "plugins\installed_plugins.json"
        # Nodes are assigned inside each branch, never as a branch's output:
        # the pipeline would unroll a JsonObject into its key/value pairs.
        $plugins = $null
        $records = $null
        try {
            $data = [System.Text.Json.Nodes.JsonNode]::Parse([IO.File]::ReadAllText($record))
            if ($data -is [System.Text.Json.Nodes.JsonObject]) { $plugins = $data["plugins"] }
            if ($plugins -is [System.Text.Json.Nodes.JsonObject]) { $records = $plugins[$pluginId] }
        } catch {
            return $false
        }
        # A list of installs (one record per scope), or one bare record. Never
        # foreach a JsonObject itself: that walks its key/value pairs.
        $list = [System.Collections.Generic.List[object]]::new()
        if ($records -is [System.Text.Json.Nodes.JsonArray]) { foreach ($r in $records) { $list.Add($r) } }
        elseif ($records -is [System.Text.Json.Nodes.JsonObject]) { $list.Add($records) }
        # A project-scoped install runs in that project only.
        foreach ($r in $list) {
            if ($r -isnot [System.Text.Json.Nodes.JsonObject]) { continue }
            if (-not $r.ContainsKey("scope") -or (Get-JsonString $r["scope"]) -ceq "user") { return $true }
        }
        return $false
    }

    if (-not (Test-Path -LiteralPath $SettingsPath)) {
        Write-Output "No settings file at $SettingsPath - nothing to remove."
        exit 3
    }
    try {
        $raw = [IO.File]::ReadAllText($SettingsPath)
        $root = [System.Text.Json.Nodes.JsonNode]::Parse($raw)
    } catch {
        Write-Output "Could not read ${SettingsPath}: $($_.Exception.InnerException.Message ?? $_.Exception.Message)"
        exit 1
    }
    $hooks = $null
    if ($root -is [System.Text.Json.Nodes.JsonObject]) { $hooks = $root["hooks"] }
    $eventNames = @()
    if ($hooks -is [System.Text.Json.Nodes.JsonObject]) { $eventNames = @(foreach ($kv in $hooks) { $kv.Key }) }

    $found = @()
    $lookalikes = @()
    foreach ($eventName in $eventNames) {
        $groups = $hooks[$eventName]
        if ($groups -isnot [System.Text.Json.Nodes.JsonArray]) { continue }
        foreach ($group in $groups) {
            if ($group -isnot [System.Text.Json.Nodes.JsonObject]) { continue }
            $entries = $group["hooks"]
            if ($entries -isnot [System.Text.Json.Nodes.JsonArray]) { continue }
            foreach ($hook in $entries) {
                if ($hook -isnot [System.Text.Json.Nodes.JsonObject]) { continue }
                $command = Get-JsonString $hook["command"]
                if ($null -eq $command) { continue }
                if (Test-ShippedHook $eventName $hook) {
                    $found += Format-Hook $eventName $command
                } elseif (@($needles | Where-Object { $command.Contains($_) }).Count -gt 0) {
                    $lookalikes += Format-Hook $eventName $command
                }
            }
        }
    }

    if ($found.Count -gt 0) {
        Write-Output "Installer-written hooks in $SettingsPath (the pseudolife-memory plugin provides these now):"
        $found | ForEach-Object { Write-Output $_ }
    } else {
        Write-Output "No installer-written Pseudolife hooks in $SettingsPath."
    }
    if ($lookalikes.Count -gt 0) {
        Write-Output "Left alone - not an exact installer-written entry; review by hand:"
        $lookalikes | ForEach-Object { Write-Output $_ }
    }
    if ($found.Count -eq 0) { exit 3 }
    if (-not (Test-PluginActive $root)) {
        Write-Output "Not removed: the pseudolife-memory plugin is not installed and enabled for all projects, so these may be the only Pseudolife hooks that run."
        exit 4
    }
    if ($DryRun) { exit 0 }

    # Emptied groups go; an emptied event stays an empty list, as this
    # script's episode-hook clean-up leaves it.
    foreach ($eventName in $eventNames) {
        $groups = $hooks[$eventName]
        if (((Get-Shipped $eventName).Count -eq 0) -or ($groups -isnot [System.Text.Json.Nodes.JsonArray])) { continue }
        for ($g = $groups.Count - 1; $g -ge 0; $g--) {
            $group = $groups[$g]
            if ($group -isnot [System.Text.Json.Nodes.JsonObject]) { continue }
            $entries = $group["hooks"]
            if ($entries -isnot [System.Text.Json.Nodes.JsonArray]) { continue }
            $before = $entries.Count
            for ($h = $entries.Count - 1; $h -ge 0; $h--) {
                if (Test-ShippedHook $eventName $entries[$h]) { $entries.RemoveAt($h) }
            }
            if (($before -gt 0) -and ($entries.Count -eq 0)) { $groups.RemoveAt($g) }
        }
    }

    $stamp = Get-Date -Format yyyyMMdd-HHmmss
    $backup = "$SettingsPath.bak-$stamp"
    for ($n = 1; Test-Path -LiteralPath $backup; $n++) { $backup = "$SettingsPath.bak-$stamp-$n" }
    Copy-Item -LiteralPath $SettingsPath -Destination $backup
    Write-Output "Backed up -> $backup"
    $options = [System.Text.Json.JsonSerializerOptions]::new()
    $options.WriteIndented = $true
    # Keep the user's text as written: the default encoder escapes quotes,
    # angle brackets and every non-ASCII character.
    $options.Encoder = [System.Text.Encodings.Web.JavaScriptEncoder]::UnsafeRelaxedJsonEscaping
    $text = $root.ToJsonString($options).Replace("`r`n", "`n") + "`n"
    if ($raw.Contains("`r`n")) { $text = $text.Replace("`n", "`r`n") }
    $tmp = "$SettingsPath.tmp-pseudolife"
    [IO.File]::WriteAllText($tmp, $text, [Text.UTF8Encoding]::new($false))
    [IO.File]::Replace($tmp, $SettingsPath, [NullString]::Value)
    Write-Output "Removed $($found.Count) installer-written hook entries from $SettingsPath."
    exit 0
}

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
    if ($Client -eq "codex") {
        $briefingGroup.hooks[0] | Add-Member commandWindows $Command
        $briefingGroup.hooks[0] | Add-Member timeout 15
    }
    $obj.hooks.SessionStart = @($obj.hooks.SessionStart) + $briefingGroup
    Write-Host "Installed SessionStart briefing hook -> $SettingsPath"
    Write-Host "  command: $Command"
} else {
    Write-Host "Briefing hook already present in $SettingsPath - skipping."
}

# Keep board setup separate from the daemon-backed memory briefing
# ($coordinationLine is defined at the top, shared with -RemoveLegacy).
$hasCoordination = $false
foreach ($group in @($obj.hooks.SessionStart)) {
    foreach ($h in @($group.hooks)) {
        if ($h.command -like "*Pseudolife coordination:*") { $hasCoordination = $true }
    }
}
if (-not $hasCoordination) {
    $coordinationHook = [pscustomobject]@{ type = 'command'; command = "echo '$coordinationLine'" }
    if ($Client -eq 'codex') {
        $coordinationHook | Add-Member commandWindows "Write-Output '$coordinationLine'"
        $coordinationHook | Add-Member timeout 5
    }
    $obj.hooks.SessionStart = @($obj.hooks.SessionStart) + [pscustomobject]@{ hooks = @($coordinationHook) }
    Write-Host "Installed SessionStart coordination hook -> $SettingsPath"
}

# Every-turn memory-discipline line (UserPromptSubmit), both clients.
# Codex requires review and trust before newly installed hooks run. Static echo
# (no daemon call): the one-shot session-start briefing loses salience over
# a long session; this keeps the loop — including recall-before-review —
# mechanical. Keep the line free of quote characters (it nests in JSON+sh).
# $disciplineLine is defined at the top, shared with -RemoveLegacy.
if ($Client -in "claude", "codex") {
    if (-not ($obj.hooks.PSObject.Properties.Name -contains 'UserPromptSubmit')) {
        $obj.hooks | Add-Member -NotePropertyName UserPromptSubmit -NotePropertyValue @()
    }
    $hasDiscipline = $false
    foreach ($group in @($obj.hooks.UserPromptSubmit)) {
        if ($null -eq $group) { continue }
        foreach ($h in @($group.hooks)) {
            if ($h.command -like "*mid-session discipline*") { $hasDiscipline = $true }
        }
    }
    if (-not $hasDiscipline) {
        $upsGroup = [pscustomobject]@{
            hooks = @([pscustomobject]@{ type = 'command'; command = "echo '$disciplineLine'" })
        }
        if ($Client -eq "codex") {
            $upsGroup.hooks[0] | Add-Member commandWindows "Write-Output '$disciplineLine'"
            $upsGroup.hooks[0] | Add-Member timeout 5
        }
        $obj.hooks.UserPromptSubmit = @($obj.hooks.UserPromptSubmit) + $upsGroup
        Write-Host "Installed UserPromptSubmit discipline hook -> $SettingsPath"
    } else {
        Write-Host "Mid-session discipline hook already present in $SettingsPath - skipping."
    }
}

# Episode hooks are OBSOLETE since the 2026-06-30 session-scoped episodes
# rework: the daemon lazily opens/closes episodes keyed by mcp-session-id
# (see docs/guide/episodes.md). Earlier installer versions added
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

if ($Client -eq "codex") {
    Write-Host ""
    Write-Warning "Codex will skip this new or changed hook until you review and trust its exact definition."
    Write-Host "  Start Codex, open /hooks, review the definition from $SettingsPath, and approve it."
    Write-Host "Current Codex runtimes enable hooks by default, including Windows."
    Write-Host "  Check /hooks for trust or managed-policy blocks; older runtimes may need updating."
    Write-Host "  If [features] hooks = false is intentional, use the standing AGENTS.md block."
}

# The hooks wire the session lifecycle, but the memory LOOP only fires if a
# standing instruction tells the agent to use the tools (issue #12: an install
# with healthy hooks + daemon still never called memory_* because no standing
# instructions carried the block). Check-and-advise only — never edit it here.
$repo = Split-Path -Parent $PSScriptRoot
$instructionFile = if ($Client -eq "codex") { "AGENTS.md" } else { "CLAUDE.md" }
$instructionPath = Join-Path (Split-Path -Parent $SettingsPath) $instructionFile
$hasBlock = (Test-Path $instructionPath) -and
    ((Get-Content $instructionPath -Raw) -match 'pseudolife-memory')
if (-not $hasBlock) {
    Write-Host ""
    Write-Warning "$instructionPath has no Pseudolife memory section. Append the bundled block for stronger recall/capture guidance:"
    Write-Host "  Add-Content `"$instructionPath`" (Get-Content `"$repo\examples\CLAUDE.memory.md`" -Raw)"
    Write-Host "(or add it to a per-project CLAUDE.md / AGENTS.md instead)"
}
