#Requires -Version 7
# Native Windows override for Codex. Claude keeps the existing Bash commands.
param([ValidateSet('SessionStart', 'UserPromptSubmit', 'SessionEnd')][string]$Event)
$ErrorActionPreference = 'Stop'

function Write-Context([string]$text) {
    @{ hookSpecificOutput = @{ hookEventName = $Event; additionalContext = $text } } |
        ConvertTo-Json -Depth 4 -Compress
}

if ($Event -eq 'UserPromptSubmit') {
    $disciplineLine = "Memory (PseudoLife) mid-session discipline: before reviewing code, docs, or a PR -> memory_search + memory_lesson_search the target area FIRST, then compare memory against the files and correct drift both ways (fix stale memory via memory_fact_set + memory_outcome; treat memory-vs-file mismatches as review findings). Status or in-progress questions -> memory_search (include sources: status) before or alongside git. Starting work in a new area -> memory_search + memory_lesson_search first. Launching or finishing long-running work -> memory_store a status entry. Outcome landed -> memory_outcome with used_ids."
    Write-Context $disciplineLine
    exit 0
}

$daemonUrl = if ($env:PSEUDOLIFE_MCP_DAEMON_URL) { $env:PSEUDOLIFE_MCP_DAEMON_URL.TrimEnd('/') } else { 'http://127.0.0.1:8765' }
$headers = @{}
if ($env:PSEUDOLIFE_MCP_TOKEN) { $headers.Authorization = "Bearer $env:PSEUDOLIFE_MCP_TOKEN" }
try {
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [string]$payload.session_id
    if ($Event -eq 'SessionStart') {
        $query = if ($sid) { '?session_id=' + [Uri]::EscapeDataString($sid) + '&source=' + [Uri]::EscapeDataString([string]$payload.source) } else { '' }
        $response = Invoke-WebRequest -Uri "$daemonUrl/api/hook/session-start$query" -Headers $headers -TimeoutSec 5
        $text = if ($response.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($response.Content) } else { [string]$response.Content }
        Write-Context $text
    } elseif ($sid) {
        # Codex caps SessionEnd at three seconds. No retry; idle reaping is
        # the backstop if this two-second request misses a busy daemon.
        $body = @{session_id = $sid} | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$daemonUrl/api/hook/session-end" -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 2 | Out-Null
    }
} catch {
    if ($Event -eq 'SessionStart') {
        Write-Context 'Pseudolife-MCP: session briefing unavailable. Try memory_search once before treating memory as offline; check the daemon health and MCP registration if it fails.'
    }
}
exit 0
