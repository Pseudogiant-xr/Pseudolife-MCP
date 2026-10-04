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
#       The run there logs in as env= says (PSEUDOLIFE_TEST_PG_USER and
#       PSEUDOLIFE_TEST_PG_PASSWORD, or PSEUDOLIFE_TEST_PG_LOGIN_FILE), else
#       as the test login in that user's ~/.pseudolife-mcp/test-pg.env
#       (`pseudolife-mcp test-login create` against its test server).
#       Nothing from this machine's ops/.env is sent.
#   memory=<systemd MemoryMax for the run>             (default 16G)
#   daemon_url=<board daemon URL as seen from there>   (optional)
# PSEUDOLIFE_SUITE_REMOTE overrides ssh=. Without either, only local runs.
#
# Both machines test the committed HEAD: a dirty checkout is refused, and a
# remote run also needs the commit pushed (the remote clone fetches it from
# origin). The remote side runs that commit's own ops/wsl-suite.sh in its
# copy mode, under a systemd scope with MemoryMax, as the SSH user (who needs
# password-free sudo for systemd-run that may preserve the environment:
# NOPASSWD:SETENV:, or NOPASSWD: ALL). It refuses to start unless env=
# leaves PSEUDOLIFE_TEST_PG_HOST_PORT set there to a test server other than
# port 5433 (a fixed PSEUDOLIFE_TEST_DATABASE_URL alone is not enough: the
# default paths would still reach 5433, which may hold a live bank), and
# unless that machine names its own suite lease (full-suite.lease there,
# full-suite@<host>); ops/wsl-suite.sh checks both under
# PSEUDOLIFE_SUITE_DISPATCHED. Port checks cannot see the live bank's
# container on its Docker-network address (port 5432), so pytest there also
# refuses any server it would use that holds a production bank
# (tests/pg_defaults.py, dispatched_live_bank_refusal). The board
# bearer, when this session
# has one, is sent on SSH's stdin, never on a command line, so the remote
# run mirrors on the board under that machine's lease (full-suite.lease
# there, e.g. full-suite@box).
#
# Output streams here and is kept as a log plus a JSON result (machine,
# commit, exit code, pytest's summary line) under
# ~/.pseudolife-mcp/suite-results. The exit code is pytest's.

$ErrorActionPreference = 'Stop'
# Refusals exit 2 (usage); Write-Error would throw under Stop and exit 1.
function Fail([string]$message) { [Console]::Error.WriteLine("remote-suite: $message"); exit 2 }

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$name = Split-Path $root -Leaf
$home_ = [Environment]::GetFolderPath('UserProfile')

$dirty = git -C $root status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) { Fail "$root is not a git checkout" }
if ($dirty) { Fail "$root has uncommitted changes; commit first" }
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
if ($where -and $where -notin 'local', 'remote') { Fail "PSEUDOLIFE_SUITE_WHERE must be local or remote" }
if ($where -eq 'remote' -and -not $remote) { Fail "no remote configured ($cfgFile ssh=...)" }

$pushed = $false
if ($remote -and $where -ne 'local') {
    git -C $root fetch --quiet origin 2>$null
    $pushed = [bool](git -C $root branch -r --contains $sha 2>$null)
    if (-not $pushed) {
        if ($where -eq 'remote') { Fail "$($sha.Substring(0,8)) is not on origin; push it first" }
        Write-Host "remote-suite: $($sha.Substring(0,8)) is not pushed, so only local WSL is a candidate"
    }
}

# A machine's suite lock: exit 0 free (or never created), 1 held. Anything
# else (ssh 255, no flock, a failed wsl.exe) is a failed probe, never "busy".
$probe = 'f="$HOME/.pseudolife-mcp/locks/full-suite.lock"; [ -e "$f" ] || exit 0; command -v flock >/dev/null || exit 3; flock -n "$f" true'
$distro = @()
if ($env:PSEUDOLIFE_WSL_DISTRO) { $distro = @('-d', $env:PSEUDOLIFE_WSL_DISTRO) }
function Get-State([string]$machine) {
    if ($machine -eq 'local') { & wsl.exe @distro -e sh -c $probe } else { & ssh -o BatchMode=yes -o ConnectTimeout=10 $remote $probe }
    switch ($LASTEXITCODE) { 0 { 'free' } 1 { 'held' } default { "error (exit $LASTEXITCODE)" } }
}

$candidates = switch ($where) {
    'local' { @('local') }
    'remote' { @('remote') }
    default { if ($remote -and $pushed) { @('local', 'remote') } else { @('local') } }
}
$target = $null; $lastNote = [datetime]::MinValue
while (-not $target) {
    foreach ($c in @($candidates)) {
        $state = Get-State $c
        if ($state -eq 'free') { $target = $c; break }
        if ($state -ne 'held') {
            if ($candidates.Count -eq 1) { Fail "cannot probe $c's suite lock: $state" }
            Write-Host "remote-suite: cannot probe $c's suite lock ($state); leaving it out"
            $candidates = @($candidates | Where-Object { $_ -ne $c })
        }
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
    # One Add per line: a list cannot nest the way `+=` of an array literal
    # did here (the whole block arrived as one line, 2026-10-02).
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('set -euo pipefail')
    if ($env:PSEUDOLIFE_MCP_TOKEN) { $lines.Add('export PSEUDOLIFE_MCP_TOKEN=' + (Quote $env:PSEUDOLIFE_MCP_TOKEN)) }
    if ($cfg['daemon_url']) { $lines.Add('export PSEUDOLIFE_MCP_DAEMON_URL=' + (Quote $cfg['daemon_url'])) }
    $lines.Add('repo="' + $cfg['repo'].Replace('~', '$HOME') + '"')
    $lines.Add('sha=' + (Quote $sha))
    $lines.Add('name=' + (Quote $name))
    $lines.Add('memory=' + (Quote $cfg['memory']))
    $lines.Add('envfile="' + $(if ($cfg['env']) { $cfg['env'].Replace('~', '$HOME') } else { '' }) + '"')
    $lines.Add('git -C "$repo" fetch --quiet origin "$sha"')
    # Named by commit, not checkout: every Codex worktree shares one name,
    # and two dispatches of different commits raced on one ref.
    $lines.Add('git -C "$repo" update-ref "refs/heads/suite/$sha" "$sha"')
    $lines.Add('script="$(mktemp)"')
    $lines.Add('git -C "$repo" show "$sha:ops/wsl-suite.sh" > "$script"')
    $lines.Add('if [ -n "$envfile" ] && [ -f "$envfile" ]; then set -a; . "$envfile"; set +a; fi')
    # The same rule ops/wsl-suite.sh enforces under PSEUDOLIFE_SUITE_DISPATCHED
    # (tests/test_wsl_suite_launcher.py), repeated here for a dispatched
    # commit older than that guard: a fixed test URL alone leaves default
    # paths on 5433, the live bank's server on the box.
    # Stripped and compared as a number ('05433', '5433 ' reach 5433 too).
    $lines.Add('server="$(printf %s "${PSEUDOLIFE_TEST_PG_HOST_PORT:-}" | tr -d "[:space:]")"; port="${server##*:}"')
    $lines.Add('if [[ "$server" != *:* || ! "$port" =~ ^[0-9]+$ ]] || (( 10#$port == 5433 )); then echo "remote-suite: PSEUDOLIFE_TEST_PG_HOST_PORT is not set to a test server other than port 5433 on this machine after env=; refusing" >&2; exit 2; fi')
    $lines.Add('export PSEUDOLIFE_SUITE_DISPATCHED=1')
    $lines.Add('export PSEUDOLIFE_SUITE_COMMIT="$sha" PSEUDOLIFE_SUITE_GIT_COMMON="$repo/.git" PSEUDOLIFE_SUITE_NAME="$name"')
    $lines.Add('export PATH="$PATH:$HOME/.local/bin" TERM=dumb')
    # exec: PowerShell ends piped stdin with CRLF, and bash must never read
    # on past the run (a stray "\r" line would replace pytest's exit code).
    # The inner shell deletes the script copy and keeps pytest's code.
    $lines.Add('exec sudo --preserve-env systemd-run --quiet --scope -p MemoryMax="$memory" --uid="$(id -u)" --gid="$(id -g)" -- env HOME="$HOME" PATH="$PATH" bash -c ''bash "$1" "${@:2}"; code=$?; rm -f -- "$1"; exit $code'' _ "$script" "$@"')
    $body = ($lines -join "`n") + "`n"
    $quotedArgs = ($args | ForEach-Object { Quote ([string]$_) }) -join ' '
    # Keepalives: a silently dropped link ends the run's ssh in ~2 min
    # instead of leaving the dispatcher waiting forever.
    $body | & ssh -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=4 $remote "bash -s -- $quotedArgs" 2>&1 | Tee-Object -FilePath $log
    $code = $LASTEXITCODE
}

$summary = (Select-String -Path $log -Pattern '\d+ (passed|failed)|no tests ran|error' -ErrorAction SilentlyContinue |
    Select-Object -Last 1).Line
[ordered]@{
    # The machine kind only: the SSH destination names a host and a user,
    # and results get cited in public PRs.
    machine = $(if ($target -eq 'remote') { 'remote' } else { 'local-wsl' })
    commit = $sha; checkout = $name; started = $started.ToString('o'); ended = (Get-Date).ToString('o')
    exit_code = $code; summary = $summary; log = $log
} | ConvertTo-Json | Set-Content -Path ($log -replace '\.log$', '.json') -Encoding utf8
Write-Host "remote-suite: $target exit $code; $summary"
exit $code
