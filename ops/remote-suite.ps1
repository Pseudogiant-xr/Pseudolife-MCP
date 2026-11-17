# Run this checkout's full suite on a second Linux machine over SSH, or in
# local WSL (ops/wsl-suite.ps1) when that is allowed.
#
#   pwsh ops/remote-suite.ps1                 # the full suite, where allowed
#   pwsh ops/remote-suite.ps1 -q -n 2         # still the full suite (display
#                                             # and worker options only)
#   pwsh ops/remote-suite.ps1 tests/x.py -x   # any other pytest arguments
#   $env:PSEUDOLIFE_SUITE_WHERE='remote'      # force a machine: local | remote
#
# Which machine. With a remote machine configured (below), it is the only
# candidate, because a full suite in WSL overloads this PC while its owner
# uses it; this PC is a candidate too only while its owner has said it is
# available: the file ~/.pseudolife-mcp/locks/pc-available
# (ops/suite-availability.ps1 has the format; expired, unreadable or
# malformed reads as not available). With it, the remote still comes first
# and local WSL takes the overflow, or the run when the remote cannot be
# probed. PSEUDOLIFE_SUITE_WHERE=local forces this PC and says it overrides
# that rule; =remote forces the remote. With no remote configured at all,
# local WSL runs as it always has. Nothing guesses availability from
# processes, the GPU, input or the hour. The run's first lines say which
# machine it chose and why. The availability file is read again on every
# probe, so a statement that expires or is removed while the run waits
# takes this PC out. The rule covers every run of this script, the
# full-suite dispatcher; a targeted run on this PC uses ops/wsl-suite.ps1.
#
# Each machine allows one full suite (its own lock). "Free" is that
# machine's suite lock, probed with flock as the suite itself takes it: a
# free lock is tried, a held one waits, and the script waits for whichever
# candidate frees first, saying so once a minute
# (PSEUDOLIFE_SUITE_POLL_SECONDS, default 20, sets how often it probes).
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
# Both machines test the committed HEAD: a dirty checkout is refused. The
# remote clone fetches a commit on origin/master from origin; any other (a
# pull request's head, an integration batch's head before `open`, a local
# branch) travels inside the dispatch as a git bundle of what origin/master
# lacks (base64 on SSH's stdin), which that clone fetches after origin, so
# nothing is pushed to the public repository for a run and nothing rests on
# this checkout's remote-tracking refs, which can be stale or another
# remote's.
# The remote side runs that commit's own ops/wsl-suite.sh in its
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
# commit, pytest arguments, exit code, pytest's summary line) under
# ~/.pseudolife-mcp/suite-results. The exit code is pytest's. Only a result
# with empty `args` counts as full-suite evidence for ops/integrate.py open,
# even though display and worker options alone still run the full suite.
#
# The real native client: PSEUDOLIFE_SUITE_NATIVE_CLIENT=1 makes the run
# build it there (ops/wsl-suite.sh, native_client: `cargo build --locked -p
# pseudolife-stdio`, the dev profile, as the hosted `test` lane does) and run
# the tests that need it. On an integrate/* branch (an integration batch,
# ops/integrate.py build) it is on unless the variable is set, because a
# batch's local gate that used the tests' stand-in passed and then failed 18
# tests in the hosted `test` lane (#1634); `ops/integrate.py open` refuses a
# batch result without it. Elsewhere it is off unless asked. The result
# JSON records what the run said: real_client (true, false, or null when the
# run never said), real_client_commit and real_client_path (home as ~).
# Linux only: WSL or the Linux machine, the hosted `test` lane's conditions,
# never the macOS or Windows lanes; and the run's workers stay sized to its
# machine where that lane runs -n 2 --dist loadfile.

$ErrorActionPreference = 'Stop'
# Refusals exit 2 (usage); Write-Error would throw under Stop and exit 1.
function Fail([string]$message) { [Console]::Error.WriteLine("remote-suite: $message"); exit 2 }
. (Join-Path $PSScriptRoot 'suite-availability.ps1')

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$name = Split-Path $root -Leaf
$home_ = [Environment]::GetFolderPath('UserProfile')

$dirty = git -C $root status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0) { Fail "$root is not a git checkout" }
if ($dirty) { Fail "$root has uncommitted changes; commit first" }
$sha = (git -C $root rev-parse HEAD).Trim()

$nativeClient = $env:PSEUDOLIFE_SUITE_NATIVE_CLIENT
$branch = (git -C $root symbolic-ref -q --short HEAD 2>$null)
if (-not $nativeClient -and $branch -like 'integrate/*') {
    $nativeClient = '1'
    Write-Host ("remote-suite: $branch is an integration batch, so the run builds the native client " +
        "and runs the real-client tests as the hosted test lane does " +
        "(PSEUDOLIFE_SUITE_NATIVE_CLIENT=1; set it to 0 to skip them, which ops/integrate.py open refuses)")
}

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

$short = $sha.Substring(0, 8)
$available = $null
if ($remote -and $where -ne 'remote') { $available = Get-SuiteAvailability $home_ }
if ($where -eq 'local' -and $remote -and -not $available.Available) {
    Write-Host ("remote-suite: PSEUDOLIFE_SUITE_WHERE=local overrides the availability rule: running on this PC " +
        "although it is not available for full suites ($($available.Why))")
}

# A commit origin/master does not contain reaches the remote machine as a
# bundle (the header); decided here, made only if the remote is chosen.
$onMaster = $false
if ($remote -and $where -ne 'local') {
    git -C $root fetch --quiet origin master 2>$null
    git -C $root merge-base --is-ancestor $sha 'origin/master' 2>$null
    $onMaster = $LASTEXITCODE -eq 0
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

# The candidates, in order. @(): a switch that yields one array unrolls it,
# and a lone string's [0] is its first character.
$candidates = @(switch ($where) {
    'local' { @('local') }
    'remote' { @('remote') }
    default {
        if (-not $remote) { @('local') }
        elseif ($available.Available) { @('remote', 'local') }
        else { @('remote') }
    }
})
if (-not $where -and $remote -and -not $available.Available) {
    Write-Host "remote-suite: only the remote machine is a candidate: this PC is not available for full suites ($($available.Why))"
}
function Get-Reason([string]$machine, [string]$remoteState) {
    if ($where) { return "forced by PSEUDOLIFE_SUITE_WHERE=$where" }
    if (-not $remote) { return 'no remote machine is configured' }
    if ($machine -eq 'remote') { return 'its suite lock is free' }
    if ($remoteState -like 'error*') {
        return "the remote machine could not be probed ($remoteState) and this PC is available ($($available.Why))"
    }
    "the remote machine is running a full suite and this PC is available ($($available.Why))"
}
$poll = 20
if ($env:PSEUDOLIFE_SUITE_POLL_SECONDS -match '^\d+$') { $poll = [int]$env:PSEUDOLIFE_SUITE_POLL_SECONDS }
$target = $null; $reason = $null; $remoteState = $null; $lastNote = [datetime]::MinValue
while (-not $target) {
    # Read again on every probe: an `until` that passes, or a file removed,
    # while this run waits takes this PC out before it could be chosen.
    if (-not $where -and $remote -and $candidates -contains 'local') {
        $available = Get-SuiteAvailability $home_
        if (-not $available.Available) {
            $candidates = @($candidates | Where-Object { $_ -ne 'local' })
            if (-not $candidates) {
                Fail "this PC is no longer available for full suites ($($available.Why)) and the remote machine could not be probed, so nothing runs"
            }
            Write-Host "remote-suite: this PC is no longer available for full suites ($($available.Why)); only the remote machine is a candidate"
        }
    }
    foreach ($c in @($candidates)) {
        $state = Get-State $c
        if ($c -eq 'remote') { $remoteState = $state }
        if ($state -eq 'free') { $target = $c; $reason = Get-Reason $c $remoteState; break }
        if ($state -ne 'held') {
            if ($candidates.Count -eq 1) {
                if ($c -eq 'remote' -and $remote -and -not $where -and -not $available.Available) {
                    Fail ("cannot probe the remote machine's suite lock ($state), and this PC is not available " +
                        "for full suites ($($available.Why)), so nothing runs; fix the remote, or wait for this PC to be available")
                }
                Fail "cannot probe $c's suite lock: $state"
            }
            Write-Host "remote-suite: cannot probe $c's suite lock ($state); leaving it out"
            $candidates = @($candidates | Where-Object { $_ -ne $c })
        }
    }
    if (-not $target) {
        if (((Get-Date) - $lastNote).TotalSeconds -ge 60) {
            if ($candidates.Count -eq 1 -and $candidates[0] -eq 'remote' -and $remote -and -not $where -and -not $available.Available) {
                Write-Host ("remote-suite: waiting for the remote machine, which is running a full suite; " +
                    "this PC is not available for full suites ($($available.Why))")
            } else {
                Write-Host "remote-suite: waiting; every candidate machine is running a full suite ($($candidates -join ', '))"
            }
            $lastNote = Get-Date
        }
        Start-Sleep -Seconds $poll
    }
}
Write-Host "remote-suite: running on $(if ($target -eq 'remote') { 'the remote machine' } else { 'this PC (local WSL)' }): $reason"
# Local WSL tests this checkout's HEAD, while the result records $sha, the
# commit chosen before any wait (the remote run carries $sha itself as a
# bundle): a checkout that moved or changed while this run waited is refused.
if ($target -eq 'local') {
    $headNow = (git -C $root rev-parse HEAD).Trim()
    $dirtyNow = git -C $root status --porcelain --untracked-files=no
    if ($headNow -ne $sha -or $dirtyNow) {
        Fail ("the checkout moved from $short to $($headNow.Substring(0, 8))" +
            $(if ($dirtyNow) { ' or has uncommitted changes' } else { '' }) +
            " while this run waited, so local WSL would test something other than the commit it records; rerun on the commit you mean")
    }
}

$results = Join-Path $home_ '.pseudolife-mcp\suite-results'
New-Item -ItemType Directory -Force -Path $results | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $results "$stamp-$target-$($sha.Substring(0,8)).log"
$started = Get-Date
Write-Host "remote-suite: $($sha.Substring(0,8)) of $name on $target; log $log"

if ($target -eq 'local') {
    # Restored afterwards, as ops/wsl-suite.ps1 restores what it sets.
    $savedNative = $env:PSEUDOLIFE_SUITE_NATIVE_CLIENT
    $savedChosen = $env:PSEUDOLIFE_SUITE_CHOSEN
    try {
        $env:PSEUDOLIFE_SUITE_NATIVE_CLIENT = $nativeClient
        # This script chose local WSL by the rule above, so ops/wsl-suite.ps1
        # does not refuse it as a direct full run.
        $env:PSEUDOLIFE_SUITE_CHOSEN = 'local'
        & pwsh -NoProfile -File (Join-Path $PSScriptRoot 'wsl-suite.ps1') @args 2>&1 | Tee-Object -FilePath $log
        $code = $LASTEXITCODE
    } finally {
        $env:PSEUDOLIFE_SUITE_NATIVE_CLIENT = $savedNative
        $env:PSEUDOLIFE_SUITE_CHOSEN = $savedChosen
    }
} else {
    function Quote([string]$s) { "'" + $s.Replace("'", "'\''") + "'" }
    # One Add per line: a list cannot nest the way `+=` of an array literal
    # did here (the whole block arrived as one line, 2026-10-02).
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('set -euo pipefail')
    # The controller's preflight also protects a dispatched older head.
    $lines.Add((Get-Content -LiteralPath (Join-Path $PSScriptRoot 'dev-tmp-space.sh') -Raw))
    $lines.Add('dev_tmp_preflight remote-suite || exit 3')
    if ($env:PSEUDOLIFE_MCP_TOKEN) { $lines.Add('export PSEUDOLIFE_MCP_TOKEN=' + (Quote $env:PSEUDOLIFE_MCP_TOKEN)) }
    if ($cfg['daemon_url']) { $lines.Add('export PSEUDOLIFE_MCP_DAEMON_URL=' + (Quote $cfg['daemon_url'])) }
    $lines.Add('repo="' + $cfg['repo'].Replace('~', '$HOME') + '"')
    $lines.Add('sha=' + (Quote $sha))
    $lines.Add('name=' + (Quote $name))
    $lines.Add('memory=' + (Quote $cfg['memory']))
    $lines.Add('envfile="' + $(if ($cfg['env']) { $cfg['env'].Replace('~', '$HOME') } else { '' }) + '"')
    $lines.Add('dispatch="$HOME/.cache/pseudolife-suite/dispatch"')
    $lines.Add('dev_tmp_disk_path remote-suite "$repo" && dev_tmp_disk_path remote-suite "$dispatch" || exit 3')
    $lines.Add('mkdir -p "$dispatch"')
    $lines.Add('bundle=""; script=""; trap ''rm -f -- "$bundle" "$script"'' EXIT')
    if ($onMaster) {
        $lines.Add('git -C "$repo" fetch --quiet origin "$sha"')
    } else {
        # The commit travels in the body: a bundle of what origin/master
        # lacks, decoded there and fetched after origin (which holds its
        # prerequisites) into the ref the copy below is checked out from.
        # The commit chosen at the start, not whatever HEAD is now: the
        # checkout may have moved while the run waited (Codex review). A
        # bundle names refs, so a private one points at it for the moment.
        $controllerDispatch = Join-Path $HOME '.cache/pseudolife-suite/dispatch'
        $null = New-Item -ItemType Directory -Path $controllerDispatch -Force
        $bundleFile = Join-Path $controllerDispatch "pseudolife-suite-$short-$PID.bundle"
        $bundleRef = "refs/pseudolife-suite-bundle/$sha-$PID"
        try {
            $said = git -C $root update-ref $bundleRef $sha 2>&1
            if ($LASTEXITCODE -ne 0) { Fail "could not pin $short for its bundle: $said" }
            $said = git -C $root bundle create $bundleFile $bundleRef '^origin/master' 2>&1
            if ($LASTEXITCODE -ne 0) {
                Fail "could not bundle $short against origin/master for the remote machine (fetch origin, or push it): $said"
            }
            $bytes = [IO.File]::ReadAllBytes($bundleFile)
            $encoded = [Convert]::ToBase64String($bytes, 'InsertLineBreaks')
            Write-Host ("remote-suite: $short is not on origin/master, so it travels as a bundle of what " +
                "origin/master lacks ($([Math]::Ceiling($bytes.Length / 1KB)) KB); nothing is pushed")
        } finally {
            git -C $root update-ref -d $bundleRef *> $null
            Remove-Item -LiteralPath $bundleFile -ErrorAction SilentlyContinue
        }
        $lines.Add('bundle="$(mktemp "$dispatch/bundle-XXXXXXXX")"')
        # Gone on any failure below; the exec at the end runs no EXIT trap,
        # and by then it is removed.
        $lines.Add("base64 -d > `"`$bundle`" <<'PSEUDOLIFE_SUITE_BUNDLE'")
        foreach ($chunk in ($encoded -split "`r?`n")) { $lines.Add($chunk) }
        $lines.Add('PSEUDOLIFE_SUITE_BUNDLE')
        # Origin for the bundle's prerequisites; if it cannot be reached the
        # clone may hold them already, and the bundle fetch says if not.
        $lines.Add('git -C "$repo" fetch --quiet origin || echo "remote-suite: could not fetch origin there; trying the bundle with what the clone holds" >&2')
        $lines.Add('git -C "$repo" fetch --quiet "$bundle" ' + (Quote "+${bundleRef}:refs/heads/suite/$sha"))
        $lines.Add('rm -f -- "$bundle"; bundle=""')
        $lines.Add('test "$(git -C "$repo" rev-parse --verify --quiet "refs/heads/suite/$sha")" = "$sha"')
    }
    # Named by commit, not checkout: every Codex worktree shares one name,
    # and two dispatches of different commits raced on one ref.
    $lines.Add('git -C "$repo" update-ref "refs/heads/suite/$sha" "$sha"')
    $lines.Add('script="$(mktemp "$dispatch/suite-XXXXXXXX.sh")"')
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
    # The pruning and worker settings set here win over env= there.
    foreach ($k in 'PSEUDOLIFE_SUITE_PRUNE', 'PSEUDOLIFE_SUITE_PRUNE_DAYS', 'PSEUDOLIFE_SUITE_KEEP',
             'PSEUDOLIFE_SUITE_WORKERS', 'PSEUDOLIFE_SUITE_PROFILE',
             'PSEUDOLIFE_SUITE_PROFILE_INTERVAL', 'PSEUDOLIFE_SUITE_CARGO_TARGET_DIR') {
        $v = [Environment]::GetEnvironmentVariable($k)
        if ($v) { $lines.Add("export $k=" + (Quote $v)) }
    }
    if ($nativeClient) { $lines.Add('export PSEUDOLIFE_SUITE_NATIVE_CLIENT=' + (Quote $nativeClient)) }
    $lines.Add('export TMPDIR="$HOME/.cache/pseudolife-suite/tmp"')
    $lines.Add('for artifact in "$TMPDIR" "${PSEUDOLIFE_SUITE_VENV:-$HOME/.venvs/pseudolife}" "${PSEUDOLIFE_SUITE_CARGO_TARGET_DIR:-$HOME/.cache/pseudolife-suite/cargo-target}" "${PSEUDOLIFE_SUITE_PROFILE_DIR:-$HOME/.pseudolife-mcp/suite-profiles}"; do dev_tmp_disk_path remote-suite "$artifact" || exit 3; done')
    $lines.Add('mkdir -p "$TMPDIR"')
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
# The profile directory the run named, by name only (it stays under
# ~/.pseudolife-mcp/suite-profiles on the machine that ran it; the full path
# names that machine's account, and results get cited in public PRs).
$profileDir = (Select-String -Path $log -Pattern '^wsl-suite: profiling to (.+)$' -ErrorAction SilentlyContinue |
    Select-Object -Last 1)
$profileDir = if ($profileDir) { ($profileDir.Matches[0].Groups[1].Value.Trim() -split '/')[-1] } else { $null }
# What the run said about the native client (ops/wsl-suite.sh prints the
# path with its home as ~): true only for "yes", false for "no" or a failed
# build, null when it never said (a launcher older than the switch, or a run
# that stopped first). The first such line: the launcher says it before
# pytest starts, and a test's captured output later in the log may quote one.
$realClient = $null; $realCommit = $null; $realPath = $null
$said = (Select-String -Path $log -Pattern '^wsl-suite: real client: (yes|no|build failed)\b(.*)$' -ErrorAction SilentlyContinue |
    Select-Object -First 1)
if ($said) {
    $realClient = $said.Matches[0].Groups[1].Value -eq 'yes'
    $rest = $said.Matches[0].Groups[2].Value
    if ($realClient -and $rest -match 'built from ([0-9a-f]{40})\b') { $realCommit = $Matches[1] }
    # The last ", at": the path ends the line, after any free text.
    if ($realClient -and $rest -match '^.*, at (.+)$') { $realPath = $Matches[1].Trim() }
}
[ordered]@{
    # The machine kind only: the SSH destination names a host and a user,
    # and results get cited in public PRs.
    machine = $(if ($target -eq 'remote') { 'remote' } else { 'local-wsl' })
    commit = $sha; checkout = $name; args = @($args)
    started = $started.ToString('o'); ended = (Get-Date).ToString('o')
    exit_code = $code; summary = $summary; log = $log
    profile = $profileDir
    real_client = $realClient; real_client_commit = $realCommit; real_client_path = $realPath
} | ConvertTo-Json | Set-Content -Path ($log -replace '\.log$', '.json') -Encoding utf8
$clientNote = if ($realClient -and $realCommit) { "real client: yes, built from $($realCommit.Substring(0, 8))" }
    elseif ($realClient) { 'real client: yes, but not built by this run (ops/integrate.py open refuses it)' }
    elseif ($realClient -eq $false) { 'real client: no' } else { 'real client: not reported' }
Write-Host "remote-suite: $target exit $code; $summary; $clientNote"
exit $code
