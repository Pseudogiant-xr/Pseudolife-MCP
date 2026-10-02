# Run this checkout's full suite on the first free machine: local WSL
# (ops/wsl-suite.ps1) or a second Linux machine over SSH.
#
#   pwsh ops/remote-suite.ps1                 # the full suite, wherever is free
#   pwsh ops/remote-suite.ps1 -q -x           # any pytest arguments
#   $env:PSEUDOLIFE_SUITE_WHERE='remote'      # force a machine: local | remote
#
# Each machine allows one full suite (its own lock), so two machines give
# two slots. "Free" is that machine's suite lock, probed with flock as the
# suite itself takes it: a free lock is tried, a held one waits, and the
# script waits for whichever machine frees first, saying so once a minute.
#
# The remote machine is configured in ~/.pseudolife-mcp/locks/full-suite.remote
# (one key=value per line; never in the repository, it names a host):
#   ssh=<ssh destination, e.g. an ~/.ssh/config alias>   (required)
#   repo=<path of a clone there>      (default ~/projects/Pseudolife-MCP)
#   env=<file of KEY=VALUE lines to source there, e.g. the test database URL>
#   memory=<systemd MemoryMax for the run>             (default 16G)
#   daemon_url=<board daemon URL as seen from there>   (optional)
# PSEUDOLIFE_SUITE_REMOTE overrides ssh=. Without either, only local runs.
#
# Both machines test the committed HEAD: a dirty checkout is refused, and a
# remote run also needs the commit pushed (the remote clone fetches it from
# origin). The remote side runs that commit's own ops/wsl-suite.sh in its
# copy mode, under a systemd scope with MemoryMax, as the SSH user (who needs
# password-free sudo for systemd-run). The board bearer, when this session
# has one, is sent on SSH's stdin, never on a command line, so the remote
# run mirrors on the board under that machine's lease (full-suite.lease
# there, e.g. full-suite@box).
#
# Output streams here and is kept as a log plus a JSON result (machine,
# commit, exit code, pytest's summary line) under
# ~/.pseudolife-mcp/suite-results. The exit code is pytest's.

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$name = Split-Path $root -Leaf
$home_ = [Environment]::GetFolderPath('UserProfile')

$dirty = git -C $root status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) { Write-Error "remote-suite: $root is not a git checkout"; exit 2 }
if ($dirty) { Write-Error "remote-suite: $root has uncommitted changes; commit first"; exit 2 }
$sha = (git -C $root rev-parse HEAD).Trim()

$cfg = @{ repo = '~/projects/Pseudolife-MCP'; memory = '16G' }
$cfgFile = Join-Path $home_ '.pseudolife-mcp\locks\full-suite.remote'
if (Test-Path $cfgFile) {
    foreach ($line in Get-Content $cfgFile) {
        if ($line -match '^\s*([a-z_]+)\s*=\s*(.*?)\s*$' -and $line -notmatch '^\s*#') { $cfg[$Matches[1]] = $Matches[2] }
    }
}
if ($env:PSEUDOLIFE_SUITE_REMOTE) { $cfg['ssh'] = $env:PSEUDOLIFE_SUITE_REMOTE }
$remote = $cfg['ssh']

$where = $env:PSEUDOLIFE_SUITE_WHERE
if ($where -and $where -notin 'local', 'remote') { Write-Error "remote-suite: PSEUDOLIFE_SUITE_WHERE must be local or remote"; exit 2 }
if ($where -eq 'remote' -and -not $remote) { Write-Error "remote-suite: no remote configured ($cfgFile ssh=...)"; exit 2 }

$pushed = $false
if ($remote -and $where -ne 'local') {
    git -C $root fetch --quiet origin 2>$null
    $pushed = [bool](git -C $root branch -r --contains $sha 2>$null)
    if (-not $pushed) {
        if ($where -eq 'remote') { Write-Error "remote-suite: $($sha.Substring(0,8)) is not on origin; push it first"; exit 2 }
        Write-Host "remote-suite: $($sha.Substring(0,8)) is not pushed, so only local WSL is a candidate"
    }
}

# A machine's suite lock: exit 0 free (or never created), 1 held.
$probe = 'f="$HOME/.pseudolife-mcp/locks/full-suite.lock"; [ -e "$f" ] || exit 0; flock -n "$f" true'
function Test-LocalFree { & wsl.exe -e sh -c $probe; return $LASTEXITCODE -eq 0 }
function Test-RemoteFree { & ssh -o BatchMode=yes -o ConnectTimeout=10 $remote $probe; return $LASTEXITCODE -eq 0 }

$candidates = switch ($where) {
    'local' { @('local') }
    'remote' { @('remote') }
    default { if ($remote -and $pushed) { @('local', 'remote') } else { @('local') } }
}
$target = $null; $lastNote = [datetime]::MinValue
while (-not $target) {
    foreach ($c in $candidates) {
        $free = if ($c -eq 'local') { Test-LocalFree } else { Test-RemoteFree }
        if ($free) { $target = $c; break }
    }
    if (-not $target) {
        if (((Get-Date) - $lastNote).TotalSeconds -ge 60) {
            Write-Host "remote-suite: waiting; every candidate machine is running a full suite ($($candidates -join ', '))"
            $lastNote = Get-Date
        }
        Start-Sleep -Seconds 20
    }
}

$results = Join-Path $home_ '.pseudolife-mcp\suite-results'
New-Item -ItemType Directory -Force -Path $results | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $results "$stamp-$target-$($sha.Substring(0,8)).log"
$started = Get-Date
Write-Host "remote-suite: $($sha.Substring(0,8)) of $name on $target; log $log"

if ($target -eq 'local') {
    & pwsh -NoProfile -File (Join-Path $PSScriptRoot 'wsl-suite.ps1') @args 2>&1 | Tee-Object -FilePath $log
    $code = $LASTEXITCODE
} else {
    function Quote([string]$s) { "'" + $s.Replace("'", "'\''") + "'" }
    $lines = @('set -euo pipefail')
    if ($env:PSEUDOLIFE_MCP_TOKEN) { $lines += 'export PSEUDOLIFE_MCP_TOKEN=' + (Quote $env:PSEUDOLIFE_MCP_TOKEN) }
    if ($cfg['daemon_url']) { $lines += 'export PSEUDOLIFE_MCP_DAEMON_URL=' + (Quote $cfg['daemon_url']) }
    $lines += @(
        'repo="' + $cfg['repo'].Replace('~', '$HOME') + '"',
        'sha=' + (Quote $sha), 'name=' + (Quote $name), 'memory=' + (Quote $cfg['memory']),
        'envfile="' + $(if ($cfg['env']) { $cfg['env'].Replace('~', '$HOME') } else { '' }) + '"',
        'git -C "$repo" fetch --quiet origin "$sha"',
        'git -C "$repo" update-ref "refs/heads/suite/$name" "$sha"',
        'script="$(mktemp)"; trap ''rm -f -- "$script"'' EXIT',
        'git -C "$repo" show "$sha:ops/wsl-suite.sh" > "$script"',
        'if [ -n "$envfile" ] && [ -f "$envfile" ]; then set -a; . "$envfile"; set +a; fi',
        'export PSEUDOLIFE_SUITE_COMMIT="$sha" PSEUDOLIFE_SUITE_GIT_COMMON="$repo/.git" PSEUDOLIFE_SUITE_NAME="$name"',
        'export PATH="$PATH:$HOME/.local/bin"',
        # exec: PowerShell ends piped stdin with CRLF, and bash must never
        # read on past the run (a stray "\r" line would replace pytest's code).
        'trap - EXIT',
        'exec sudo --preserve-env systemd-run --quiet --scope -p MemoryMax="$memory" --uid="$(id -u)" --gid="$(id -g)" -- env HOME="$HOME" PATH="$PATH" bash -c ''bash "$1" "${@:2}"; code=$?; rm -f -- "$1"; exit $code'' _ "$script" "$@"'
    )
    $body = ($lines -join "`n") + "`n"
    $quotedArgs = ($args | ForEach-Object { Quote ([string]$_) }) -join ' '
    $body | & ssh -o BatchMode=yes $remote "bash -s -- $quotedArgs" 2>&1 | Tee-Object -FilePath $log
    $code = $LASTEXITCODE
}

$summary = (Select-String -Path $log -Pattern '\d+ (passed|failed)|no tests ran|error' -ErrorAction SilentlyContinue |
    Select-Object -Last 1).Line
[ordered]@{
    machine = $target; host = $(if ($target -eq 'remote') { $remote } else { 'local-wsl' })
    commit = $sha; checkout = $name; started = $started.ToString('o'); ended = (Get-Date).ToString('o')
    exit_code = $code; summary = $summary; log = $log
} | ConvertTo-Json | Set-Content -Path ($log -replace '\.log$', '.json') -Encoding utf8
Write-Host "remote-suite: $target exit $code; $summary"
exit $code
