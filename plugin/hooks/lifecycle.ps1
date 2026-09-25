#Requires -Version 7
# Native Windows override for Codex. Claude keeps the existing Bash commands.
param([ValidateSet('SessionStart', 'UserPromptSubmit', 'CoordinationStart', 'CoordinationPrompt', 'SessionEnd', 'Stop')][string]$Event)
$ErrorActionPreference = 'Stop'

function Write-Context([string]$text) {
    $hookEvent = switch ($Event) {
        'CoordinationStart' { 'SessionStart' }
        'CoordinationPrompt' { 'UserPromptSubmit' }
        default { $Event }
    }
    @{ hookSpecificOutput = @{ hookEventName = $hookEvent; additionalContext = $text } } |
        # Redirected stdout can inherit an OEM console code page. Escaping
        # Unicode keeps the JSON bytes valid regardless of that encoding.
        ConvertTo-Json -Depth 4 -Compress -EscapeHandling EscapeNonAscii
}

# Per-session coordination digest written by the shim's adapter; the same
# path derivation as user-prompt-submit.sh (SHA-256 of the session id under
# PSEUDOLIFE_DIGEST_DIR, default ~/.pseudolife-mcp/digests).
function Get-DigestDir {
    if ($env:PSEUDOLIFE_DIGEST_DIR) { return $env:PSEUDOLIFE_DIGEST_DIR }
    return Join-Path $HOME '.pseudolife-mcp/digests'
}

# The plugin release this hook runs from (the manifest beside the script),
# sent with SessionStart so the daemon can open the briefing with a notice
# when the two differ. '' without a manifest or for a non-version value.
function Get-PluginVersion {
    $root = if ($env:CLAUDE_PLUGIN_ROOT) { $env:CLAUDE_PLUGIN_ROOT } else { Join-Path $PSScriptRoot '..' }
    try {
        $manifest = Get-Content -LiteralPath (Join-Path $root '.claude-plugin/plugin.json') -Raw | ConvertFrom-Json
        $version = [string]$manifest.version
    } catch { return '' }
    if ($version -match '^[0-9A-Za-z.+-]{1,32}$') { return $version }
    return ''
}

# A digest of the hook scripts beside this one, so the daemon can tell
# a cached plugin at its own version apart from its own hooks (the version
# only moves with a release). Same function as pseudolife_memory.plugin_hooks
# and session-start.sh: SHA-256 over `name NUL bytes NUL`, CRLF read as LF.
function Get-PluginHooksDigest {
    $names = @('lifecycle.ps1', 'session-start.sh', 'user-prompt-submit.sh', 'coordination-start.sh', 'coordination-prompt.sh', 'session-end.sh', 'stop-wake.sh')
    $stream = New-Object IO.MemoryStream
    try {
        foreach ($name in $names) {
            $path = Join-Path $PSScriptRoot $name
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return '' }
            $body = [IO.File]::ReadAllBytes($path)
            $text = [Text.Encoding]::Latin1.GetString($body) -replace "`r`n", "`n"
            $chunk = [Text.Encoding]::UTF8.GetBytes($name) + [byte[]]@(0) + [Text.Encoding]::Latin1.GetBytes($text) + [byte[]]@(0)
            $stream.Write($chunk, 0, $chunk.Length)
        }
        $hash = [Security.Cryptography.SHA256]::Create().ComputeHash($stream.ToArray())
        return ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
    } catch { return '' }
}

function Get-DigestKey([string]$SessionId) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($SessionId)
    $hash = [Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
}

# Returns the digest text to print this turn ('' when nothing changed) and
# advances the shared .seen marker; appends one ledger line per call.
function Read-TurnDigest([string]$SessionId) {
    $digestDir = Get-DigestDir
    if (-not $SessionId -or -not (Test-Path -LiteralPath $digestDir -PathType Container)) { return '' }
    $key = Get-DigestKey $SessionId
    $file = Join-Path $digestDir "$key.txt"
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return '' }
    $seen = Join-Path $digestDir "$key.seen"
    $lines = [IO.File]::ReadAllText($file, [Text.Encoding]::UTF8) -split "`r?`n"
    [int64]$watermark = 0
    [int64]$last = 0
    $valid = [int64]::TryParse($lines[0].Trim(), [ref]$watermark)
    if (Test-Path -LiteralPath $seen -PathType Leaf) {
        [void][int64]::TryParse([IO.File]::ReadAllText($seen).Trim(), [ref]$last)
    }
    $body = ''
    $printed = 0
    if ($valid -and $watermark -gt $last) {
        $body = (($lines | Select-Object -Skip 1) -join "`n").TrimEnd("`n", "`r")
        if ($body) { $printed = $body.Length + 1 }
        [IO.File]::WriteAllText($seen, "$watermark`n")
    }
    # The marker is already advanced: a ledger problem (the shim appends to
    # the same file and Windows refuses a concurrent open) must not lose
    # the body, so the ledger is best effort.
    try {
        $ledger = Join-Path $digestDir 'ledger.log'
        if ((Test-Path -LiteralPath $ledger -PathType Leaf) -and (Get-Item -LiteralPath $ledger).Length -gt 1048576) {
            $kept = Get-Content -LiteralPath $ledger -Tail 2000
            [IO.File]::WriteAllText($ledger, (($kept -join "`n") + "`n"))
        }
        $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $shown = if ($valid) { $watermark } else { 0 }
        [IO.File]::AppendAllText($ledger, "$stamp`thook`t$($key.Substring(0, 8))`t$shown`t$printed`n")
    } catch {}
    return $body
}

function Get-PseudolifeToken([string]$TokenFile) {
    if (-not $TokenFile) {
        return [string]$env:PSEUDOLIFE_MCP_TOKEN
    }
    $item = Get-Item -LiteralPath $TokenFile -Force
    if ($item.PSIsContainer -or $item.Length -lt 1 -or $item.Length -gt 4096 -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.LinkType) {
        throw 'The configured credential file is not a private regular file.'
    }
    if ($IsWindows) {
        $acl = Get-Acl -LiteralPath $TokenFile
        $owner = ([Security.Principal.NTAccount]$acl.Owner).Translate(
            [Security.Principal.SecurityIdentifier]).Value
        $current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $allowed = @($acl.Access | Where-Object AccessControlType -eq Allow)
        foreach ($rule in $allowed) {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin $owner, 'S-1-3-4') {
                throw 'The configured credential file is not owner-only.'
            }
        }
        if (-not $acl.AreAccessRulesProtected -or $owner -ne $current -or -not $allowed) {
            throw 'The configured credential file is not owner-only.'
        }
    }
    $token = [IO.File]::ReadAllText($TokenFile, [Text.Encoding]::UTF8).TrimEnd(
        [char[]]"`r`n")
    if (-not $token -or $token.Length -gt 4096 -or $token -match '\s') {
        throw 'The configured credential file contains an invalid bearer token.'
    }
    return $token
}

function Get-PseudolifeConnection {
    $codexHome = if ($env:CODEX_HOME) {
        $env:CODEX_HOME
    } else {
        Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
    }
    $path = Join-Path $codexHome 'pseudolife/connection.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.Length -lt 2 -or $item.Length -gt 16384 -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.LinkType) {
        throw 'The managed Codex connection file is unsafe.'
    }
    if ($IsWindows) {
        $acl = Get-Acl -LiteralPath $path
        $owner = ([Security.Principal.NTAccount]$acl.Owner).Translate(
            [Security.Principal.SecurityIdentifier]).Value
        $current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $allowed = @($acl.Access | Where-Object AccessControlType -eq Allow)
        foreach ($rule in $allowed) {
            $sid = $rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin $owner, 'S-1-3-4') {
                throw 'The managed Codex connection file is not owner-only.'
            }
        }
        if (-not $acl.AreAccessRulesProtected -or $owner -ne $current -or -not $allowed) {
            throw 'The managed Codex connection file is not owner-only.'
        }
    }
    $raw = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    $canonical = '\A\{\r?\n  "version": 1,\r?\n  "daemon_url": "[A-Za-z0-9+/=]+",\r?\n  "token_file": "[A-Za-z0-9+/=]*"\r?\n\}\r?\n?\z'
    if ($raw -notmatch $canonical) {
        throw 'The managed Codex connection file is malformed.'
    }
    $connection = $raw | ConvertFrom-Json
    $properties = @($connection.PSObject.Properties.Name | Sort-Object)
    if (($properties -join ',') -ne 'daemon_url,token_file,version' -or
        $connection.version -ne 1 -or
        $connection.daemon_url -isnot [string] -or
        $connection.token_file -isnot [string]) {
        throw 'The managed Codex connection file is malformed.'
    }
    try {
        $strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
        return [pscustomobject]@{
            daemon_url = $strictUtf8.GetString(
                [Convert]::FromBase64String($connection.daemon_url))
            token_file = $strictUtf8.GetString(
                [Convert]::FromBase64String($connection.token_file))
        }
    } catch {
        throw 'The managed Codex connection file is malformed.'
    }
}

$rawInput = [Console]::In.ReadToEnd()
# Stop carries Claude Code's opt-in wake hook (stop-wake.sh), which Claude
# runs through the bash command. Codex loads the same hooks.json and runs
# this one instead: for Codex it is a silent no-op.
if ($Event -eq 'Stop') { exit 0 }
$sessionId = ''
$startReason = ''
try {
    $parsedInput = $rawInput | ConvertFrom-Json
    $sessionId = [string]$parsedInput.session_id
    $startReason = [string]$parsedInput.source
    if (-not $startReason) { $startReason = [string]$parsedInput.session_start_reason }
} catch {}

if ($Event -eq 'UserPromptSubmit') {
    $disciplineLine = "Memory (PseudoLife) mid-session discipline: before reviewing code, docs, or a PR -> memory_search + memory_lesson_search the target area FIRST, then compare memory against the files and correct drift both ways (fix stale memory via memory_fact_set + memory_outcome; treat memory-vs-file mismatches as review findings). Status or in-progress questions -> memory_search (include sources: status) before or alongside git. Starting work in a new area -> memory_search + memory_lesson_search first. Launching or finishing long-running work -> memory_store a status entry. Outcome landed -> memory_outcome with used_ids."
    Write-Context $disciplineLine
    exit 0
}

if ($Event -eq 'CoordinationPrompt') {
    try {
        $digest = Read-TurnDigest $sessionId
        if ($digest) { Write-Context $digest }
    } catch {}
    exit 0
}

if ($Event -eq 'CoordinationStart') {
    if ($startReason -in 'resume', 'compact', 'clear' -and $sessionId) {
    # A resumed or compacted session lost the digest it saw; clearing the
    # marker makes the next prompt print the current one afresh.
    try {
        $seenPath = Join-Path (Get-DigestDir) ((Get-DigestKey $sessionId) + '.seen')
        if (Test-Path -LiteralPath $seenPath -PathType Leaf) { Remove-Item -LiteralPath $seenPath -Force }
    } catch {}
    }
    # The board check-in comes from the daemon below, and only for a
    # credential that can use the board; a client that set
    # PSEUDOLIFE_AGENT_COORDINATION to anything but a yes asks for nothing.
    $coordinationSetting = ([string]$env:PSEUDOLIFE_AGENT_COORDINATION).Trim().ToLowerInvariant()
    if ($coordinationSetting -and $coordinationSetting -notin '1', 'true', 'yes', 'on') { exit 0 }
}

$headers = @{}
try {
    $connection = Get-PseudolifeConnection
    $managedTokenless = $connection -and -not $connection.token_file
    $explicitUrl = if ($managedTokenless) {
        $null
    } elseif ($env:PSEUDOLIFE_MCP_DAEMON_URL) {
        $env:PSEUDOLIFE_MCP_DAEMON_URL.TrimEnd('/')
    } else { $null }
    $managedUrl = if ($connection) { [string]$connection.daemon_url.TrimEnd('/') } else { $null }
    if ($explicitUrl -and $managedUrl -and $explicitUrl -ne $managedUrl) {
        throw 'The explicit daemon URL conflicts with the managed Codex connection.'
    }
    $explicitTokenFile = if ($managedTokenless) {
        $null
    } elseif ($env:PSEUDOLIFE_MCP_TOKEN_FILE) {
        [string]$env:PSEUDOLIFE_MCP_TOKEN_FILE
    } else { $null }
    if ($connection -and $explicitTokenFile -and
        (-not $explicitUrl -or $explicitUrl -ne $managedUrl)) {
        throw 'An explicit credential file requires the matching managed daemon URL.'
    }
    $daemonUrl = if ($explicitUrl) {
        $explicitUrl
    } elseif ($managedUrl) {
        $managedUrl
    } else {
        'http://127.0.0.1:8765'
    }
    $parsedUrl = $null
    if (-not [Uri]::TryCreate($daemonUrl, [UriKind]::Absolute, [ref]$parsedUrl) -or
        $parsedUrl.Scheme -notin 'http', 'https' -or -not $parsedUrl.Host -or
        $parsedUrl.UserInfo -or $parsedUrl.Query -or $parsedUrl.Fragment -or
        $parsedUrl.AbsolutePath -ne '/') {
        throw 'The configured memory daemon URL is invalid.'
    }
    $tokenFile = if ($explicitTokenFile) {
        $explicitTokenFile
    } elseif ($connection) {
        [string]$connection.token_file
    } else { $null }
    $token = if ($managedTokenless) { '' } else { Get-PseudolifeToken $tokenFile }
    if ($token) { $headers.Authorization = "Bearer $token" }
    if ($Event -eq 'CoordinationStart') {
        # The daemon serves the check-in, or an empty body when this bearer
        # cannot use the board. One request, no retry: at most two seconds
        # inside the five-second hook budget; no answer adds nothing.
        $response = Invoke-WebRequest -Uri "$daemonUrl/api/hook/coordination-start" -Headers $headers -TimeoutSec 2 -MaximumRedirection 0
        $text = if ($response.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($response.Content) } else { [string]$response.Content }
        if ($text.Trim()) { Write-Context $text.TrimEnd() }
        exit 0
    }
    $payload = $rawInput | ConvertFrom-Json
    $sid = [string]$payload.session_id
    if ($Event -eq 'SessionStart') {
        $pairs = @()
        if ($sid) {
            $pairs += 'session_id=' + [Uri]::EscapeDataString($sid)
            $pairs += 'source=' + [Uri]::EscapeDataString([string]$payload.source)
        }
        $pluginVersion = Get-PluginVersion
        if ($pluginVersion) { $pairs += 'plugin_version=' + [Uri]::EscapeDataString($pluginVersion) }
        $hooksDigest = Get-PluginHooksDigest
        if ($hooksDigest -match '^[0-9a-f]{64}$') { $pairs += 'plugin_hooks_digest=' + $hooksDigest }
        $query = if ($pairs.Count) { '?' + ($pairs -join '&') } else { '' }
        # Match session-start.sh's maintenance-stall retry: at most 5+1+5
        # seconds of request/delay budget, within the 15-second hook budget.
        # Registration is idempotent per session_id. Do not retry auth errors.
        for ($attempt = 0; $attempt -lt 2; $attempt++) {
            try {
                $response = Invoke-WebRequest -Uri "$daemonUrl/api/hook/session-start$query" -Headers $headers -TimeoutSec 5 -MaximumRedirection 0
                break
            } catch {
                $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
                if (($attempt -eq 1) -or ($status -ne 0 -and $status -notin 408, 429, 500, 502, 503, 504)) { throw }
                Start-Sleep -Seconds 1
            }
        }
        $text = if ($response.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($response.Content) } else { [string]$response.Content }
        Write-Context $text
    } elseif ($sid) {
        # Codex caps SessionEnd at three seconds. No retry; idle reaping is
        # the backstop if this two-second request misses a busy daemon.
        $body = @{session_id = $sid} | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$daemonUrl/api/hook/session-end" -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 2 -MaximumRedirection 0 | Out-Null
    }
} catch {
    if ($Event -eq 'SessionStart') {
        Write-Context 'Pseudolife-MCP: session briefing unavailable. Try memory_search once before treating memory as offline; check the daemon health and MCP registration if it fails.'
    }
}
exit 0
