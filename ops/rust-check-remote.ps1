# The far half of `python ops/integrate.py build`'s Rust compile check when
# it runs on a dev machine (rust-check.remote; docs/dev/dev-machines.md).
#
# integrate.py sends this script on ssh's stdin (its first line, behind a
# short bootstrap command), followed by a git bundle of the batch
# (`<branch> ^<base>`), base64-encoded, so the batch head reaches the machine
# without being pushed anywhere. Here, under one lock per clone:
#   1. the clone fetches origin's master (and the base if master no longer
#      holds it) and verifies the bundle against it;
#   2. the bundle's branch is fetched into a private ref, which must name
#      exactly the batch head;
#   3. one gate worktree beside the clone (<repo>.rust-check) is forced to
#      that head and cleaned, and `cargo check --workspace --all-targets
#      --locked` runs in its rust/ with one target directory kept for that one
#      copy (<repo>.rust-check-target), since a target shared between copies
#      hands back another copy's units;
#   4. the private ref and the bundle go, and the next run sweeps any that a
#      killed run left.
# Its last line is the verdict integrate.py reads, tagged with the run's
# nonce so no line cargo passes through (a member's proc macro can print)
# can stand in for it: `rust-check <nonce>: cargo exit N` once cargo ran
# (its exit code), else `rust-check <nonce>: not run: <cause>` with exit 3.
# Needs PowerShell 7.2 or later, git and rustup's cargo on PATH. It refuses,
# as not run, while the machine's full-suite lock is held or its drive has
# under 8 GB free, so it neither overlaps a full suite nor reads a full disk
# as a red batch.
param(
    [Parameter(Mandatory)] [string] $Repo,
    [Parameter(Mandatory)] [string] $Base,
    [Parameter(Mandatory)] [string] $Head,
    [Parameter(Mandatory)] [string] $Ref,
    [string] $Nonce = '',
    [int] $LockWaitSeconds = 1200,
    # CLAUDE.md "Disk space": no Rust build below 8 GB free.
    [double] $MinFreeGB = 8
)

$ErrorActionPreference = 'Stop'
# A native command's stderr (git's progress, cargo's whole output) is output
# here, never a terminating error.
$PSNativeCommandUseErrorActionPreference = $false
# Without this a cmdlet's progress record can reach stderr as CLIXML after
# the verdict line, which then is not the last line and reads as "not run":
# 1 run in 20 of the end-to-end test on the Windows dev VM, 16 November 2026.
$ProgressPreference = 'SilentlyContinue'
# git never prompts here: a prompt would hang the run until its timeout.
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'Never'

$tag = if ($Nonce) { "rust-check ${Nonce}:" } else { 'rust-check:' }

function Stop-Check([string] $why) {
    [Console]::Error.WriteLine("$tag not run: $why")
    exit 3
}

function Invoke-Git {
    # git's own words are kept for the cause; its exit code decides. Its
    # stderr, merged here, must not stop the script.
    $ErrorActionPreference = 'Continue'
    # No hook runs: core.hooksPath names a directory that does not exist, so
    # a hook one batch's build script wrote into the clone cannot run in the
    # next batch's checkout.
    $hooks = if ($script:noHooks) { @('-c', "core.hooksPath=$script:noHooks") } else { @() }
    $out = & git @hooks @args 2>&1 | ForEach-Object { "$_" }
    [pscustomobject]@{ Code = $LASTEXITCODE; Text = (($out | Select-Object -Last 3) -join ' ').Trim() }
}

if ($Head -notmatch '^[0-9a-f]{40}$' -or $Base -notmatch '^[0-9a-f]{40}$') { Stop-Check "the head and base must be full commit ids" }
if ($Ref -notmatch '^refs/heads/integrate/[0-9A-Za-z._-]+$') { Stop-Check "the bundle's ref must be an integrate/* branch, not '$Ref'" }
if ($Nonce -and $Nonce -notmatch '^[0-9a-f]{16,64}$') { Stop-Check "the nonce must be hex" }
if (-not (Test-Path -LiteralPath (Join-Path $Repo '.git'))) { Stop-Check "no git clone at the configured repo= path" }
# Beside the clone, whatever spelling repo= used (a trailing separator would
# put the gate inside it).
$Repo = (Resolve-Path -LiteralPath $Repo).ProviderPath.TrimEnd([char]92, [char]47)
$noHooks = "$Repo.rust-check-no-hooks"
if (Test-Path -LiteralPath $noHooks) { Stop-Check "$([IO.Path]::GetFileName($noHooks)) exists beside the clone; it must not (hooks would run from it): remove it" }

$data = [Console]::In.ReadToEnd()
try { $bytes = [Convert]::FromBase64String($data) } catch { Stop-Check "the bundle on stdin is not base64" }
if ($bytes.Length -eq 0) { Stop-Check "no bundle arrived on stdin" }

$gate = "$Repo.rust-check"
$target = "$Repo.rust-check-target"
$lockPath = "$Repo.rust-check.lock"
$bundle = "$Repo.rust-check-$($Head.Substring(0, 12)).bundle"
$private = "refs/rust-check/$Head"

# One build at a time per clone: the gate worktree and its target are shared.
$lock = $null
$deadline = (Get-Date).AddSeconds($LockWaitSeconds)
$said = $false
while (-not $lock) {
    try {
        $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
    } catch [System.IO.IOException] {
        if ((Get-Date) -ge $deadline) {
            Stop-Check "another build held the gate lock ($([IO.Path]::GetFileName($lockPath))) for $LockWaitSeconds s"
        }
        if (-not $said) {
            [Console]::Error.WriteLine("rust-check: waiting for the gate lock another build holds")
            $said = $true
        }
        Start-Sleep -Seconds 5
    }
}

# A run killed with its session (ssh dropped) skipped its cleanup.
foreach ($stale in @(& git -C $Repo for-each-ref --format='%(refname)' refs/rust-check/ 2>$null)) {
    if ($stale) { $null = Invoke-Git -C $Repo update-ref -d $stale }
}
Get-ChildItem -LiteralPath (Split-Path -Parent $Repo) -Filter "$(Split-Path -Leaf $Repo).rust-check-*.bundle" -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

$verdict = $null
$failure = $null
$suiteHold = $null
# `break` leaves the do-block with a cause, so the cleanup below always runs
# and the verdict is written after it.
try {
    do {
        # The machine's full-suite lock, found as tests/suite_lock.py finds it,
        # held until the cleanup below, so a suite that starts meanwhile
        # queues behind the check instead of overlapping it. It is taken
        # the way the suite takes it: a lock on byte 0 on Windows
        # (msvcrt.locking there; the file stays openable, as the suite opens
        # it before locking), flock elsewhere (.NET's FileShare.None).
        $locks = if ($env:PSEUDOLIFE_SUITE_LOCK_DIR) { $env:PSEUDOLIFE_SUITE_LOCK_DIR } else { Join-Path $HOME '.pseudolife-mcp/locks' }
        $null = New-Item -ItemType Directory -Force -Path $locks
        $suitePath = Join-Path $locks 'full-suite.lock'
        try {
            if ($IsWindows) {
                $suiteHold = [IO.File]::Open($suitePath, 'OpenOrCreate', 'ReadWrite', 'ReadWrite')
                $suiteHold.Lock(0, 1)
            } else {
                $suiteHold = [IO.File]::Open($suitePath, 'OpenOrCreate', 'ReadWrite', 'None')
            }
        } catch [System.IO.IOException] {
            if ($suiteHold) { $suiteHold.Dispose(); $suiteHold = $null }
            $failure = "a full suite holds this machine (its full-suite.lock); a compile check does not overlap one, so build again once it ends"
            break
        }
        $free = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($Repo)).AvailableFreeSpace / 1GB
        if ($free -lt $MinFreeGB) { $failure = "only $([math]::Round($free, 1)) GB free on the clone's drive; the floor is $MinFreeGB GB (CLAUDE.md, Disk space)"; break }
        $r = Invoke-Git -C $Repo fetch --quiet origin master
        if ($r.Code -ne 0) { $failure = "the clone cannot fetch origin (git exit $($r.Code): $($r.Text))"; break }
        $r = Invoke-Git -C $Repo cat-file -e "$Base^{commit}"
        if ($r.Code -ne 0) {
            $r = Invoke-Git -C $Repo fetch --quiet origin $Base
            if ($r.Code -ne 0) { $failure = "the batch's base $($Base.Substring(0, 12)) is not on origin (git exit $($r.Code): $($r.Text))"; break }
        }
        [IO.File]::WriteAllBytes($bundle, $bytes)
        $r = Invoke-Git -C $Repo bundle verify --quiet $bundle
        if ($r.Code -ne 0) { $failure = "the bundle does not apply to the clone (git exit $($r.Code): $($r.Text))"; break }
        $r = Invoke-Git -C $Repo fetch --quiet $bundle "+${Ref}:$private"
        if ($r.Code -ne 0) { $failure = "the bundle's $Ref did not fetch (git exit $($r.Code): $($r.Text))"; break }
        $got = (& git -C $Repo rev-parse --verify --quiet "$private^{commit}" 2>$null)
        if ("$got".Trim() -ne $Head) { $failure = "the bundle's head is '$("$got".Trim())', not the batch head $Head"; break }

        if (Test-Path -LiteralPath (Join-Path $gate '.git')) {
            $r = Invoke-Git -C $gate checkout --quiet --detach --force $Head
            if ($r.Code -eq 0) { $r = Invoke-Git -C $gate clean -ffdxq }
        } else {
            $null = Invoke-Git -C $Repo worktree prune
            $r = Invoke-Git -C $Repo worktree add --quiet --detach --force $gate $Head
        }
        if ($r.Code -ne 0) { $failure = "the gate worktree could not be set to the head (git exit $($r.Code): $($r.Text))"; break }
        $at = (& git -C $gate rev-parse HEAD 2>$null)
        if ("$at".Trim() -ne $Head) { $failure = "the gate worktree is at '$("$at".Trim())', not $Head"; break }

        # Member build scripts and proc macros run under cargo, so this
        # session's credential-shaped variables are withheld, as integrate.py
        # withholds them locally (SECRET_ENV_RE, URL_CREDENTIALS_RE; a test
        # pins the two patterns equal). The toolchain's variables stay.
        $secretName = '(?i)TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|DSN|DATABASE_URL|(^|_)PAT$|(^|_)(API_)?KEY(_ID)?$'
        $secretValue = '://[^/@\s]+:[^/@\s]*@'
        foreach ($v in @(Get-ChildItem env:)) {
            if ($v.Name -match $secretName -or "$($v.Value)" -match $secretValue) {
                Remove-Item -LiteralPath "env:$($v.Name)" -ErrorAction SilentlyContinue
            }
        }
        $env:CARGO_TARGET_DIR = $target
        $env:CARGO_TERM_COLOR = 'never'
        if (-not (Get-Command cargo -CommandType Application -ErrorAction SilentlyContinue)) { $failure = "cargo is not on PATH"; break }
        Push-Location -LiteralPath (Join-Path $gate 'rust')
        try {
            & cargo check --workspace --all-targets --locked
            $verdict = $LASTEXITCODE
        } finally {
            Pop-Location
        }
    } while ($false)
} finally {
    $null = Invoke-Git -C $Repo update-ref -d $private
    Remove-Item -LiteralPath $bundle -Force -ErrorAction SilentlyContinue
    if ($suiteHold) { $suiteHold.Dispose() }
    $lock.Dispose()
}
if ($null -ne $failure) { Stop-Check $failure }
[Console]::Error.WriteLine("$tag cargo exit $verdict")
exit $verdict
