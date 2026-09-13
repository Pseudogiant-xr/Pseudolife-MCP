#Requires -Version 7
# Native Windows override for Codex. Claude keeps the existing Bash commands.
param([ValidateSet('SessionStart', 'UserPromptSubmit', 'SessionEnd')][string]$Event)
$ErrorActionPreference = 'Stop'

function Write-Context([string]$text) {
    @{ hookSpecificOutput = @{ hookEventName = $Event; additionalContext = $text } } |
        # Redirected stdout can inherit an OEM console code page. Escaping
        # Unicode keeps the JSON bytes valid regardless of that encoding.
        ConvertTo-Json -Depth 4 -Compress -EscapeHandling EscapeNonAscii
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

if ($Event -eq 'UserPromptSubmit') {
    $disciplineLine = "Memory (PseudoLife) mid-session discipline: before reviewing code, docs, or a PR -> memory_search + memory_lesson_search the target area FIRST, then compare memory against the files and correct drift both ways (fix stale memory via memory_fact_set + memory_outcome; treat memory-vs-file mismatches as review findings). Status or in-progress questions -> memory_search (include sources: status) before or alongside git. Starting work in a new area -> memory_search + memory_lesson_search first. Launching or finishing long-running work -> memory_store a status entry. Outcome landed -> memory_outcome with used_ids."
    Write-Context $disciplineLine
    exit 0
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
    $payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [string]$payload.session_id
    if ($Event -eq 'SessionStart') {
        $query = if ($sid) { '?session_id=' + [Uri]::EscapeDataString($sid) + '&source=' + [Uri]::EscapeDataString([string]$payload.source) } else { '' }
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
