# The far half of `python ops/integrate.py harness`: a batch's Rust harness
# Python tests (integrate.py RUST_HARNESS) on the dev machine that
# rust-check.remote names (docs/dev/dev-machines.md, "Batch harness and shim tests").
#
# As for ops/rust-check-remote.ps1, the script arrives on ssh's stdin behind
# a short bootstrap, followed by a git bundle of the batch, so nothing is
# pushed. Under the machine suite/database locks and a clone lock:
#   1. the clone fetches origin's master, verifies the bundle, and fetches its
#      branch into a private ref that must name exactly the batch head;
#   2. one harness worktree beside the clone (<repo>.rust-harness) is forced
#      to that head and cleaned;
#   3. a uv environment kept beside the clone (<repo>.rust-harness-venv) gets
#      the head's own dev dependencies;
#   4. the machine's private services hook (~/.pseudolife-mcp/
#      rust-harness-services.ps1) starts its test PostgreSQL and a Linux
#      Docker engine and must prove both answer (Start-HarnessServices throws
#      otherwise), and stops them afterwards (Stop-HarnessServices);
#   5. pytest runs the paths given with --require-databases, so a database
#      case that cannot run fails instead of skipping;
#   6. the private ref and bundle are removed on exit; stale private refs
#      from an interrupted run are cleared before the next fetch.
# Its last line is the verdict integrate.py reads, tagged with the run's
# nonce: `rust-harness <nonce>: pytest exit N`, or `rust-harness <nonce>: not
# run: <cause>` with exit 3. Needs PowerShell 7, git and uv on PATH.
param(
    [Parameter(Mandatory)] [string] $Repo,
    [Parameter(Mandatory)] [string] $Base,
    [Parameter(Mandatory)] [string] $Head,
    [Parameter(Mandatory)] [string] $Ref,
    [Parameter(Mandatory)] [string[]] $PytestArgs,
    [string] $Nonce = '',
    # Tests point this at a stand-in; integrate.py never passes it.
    [string] $ServicesHook = '',
    [ValidateRange(0, 86400)] [int] $LockWaitSeconds = 1200,
    [string] $DatabaseLockPath = '',
    [double] $MinFreeGB = 8
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$ProgressPreference = 'SilentlyContinue'
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'Never'
# LOAD_DATABASE_LOCK
if (-not (Get-Command Enter-DevDatabaseLock -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot 'dev-database-lock.ps1')
}


$tag = if ($Nonce) { "rust-harness ${Nonce}:" } else { 'rust-harness:' }

function Stop-Run([string] $why) {
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

function Get-FreeBytes([string] $path) {
    # The free space of the filesystem holding $path: the mounted volume whose
    # root is the longest prefix of it, since GetPathRoot gives "/" for every
    # Linux path, so a clone on its own mount would be judged by the root's.
    $sep = [IO.Path]::DirectorySeparatorChar
    $cmp = if ($IsWindows) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
    $full = [IO.Path]::GetFullPath($path).TrimEnd([char]92, [char]47) + $sep
    $best = $null
    foreach ($d in [IO.DriveInfo]::GetDrives()) {
        try {
            if (-not $d.IsReady) { continue }
            $root = $d.RootDirectory.FullName.TrimEnd([char]92, [char]47) + $sep
            if ($full.StartsWith($root, $cmp) -and (-not $best -or $root.Length -gt $best.Root.Length)) {
                $best = [pscustomobject]@{ Root = $root; Free = $d.AvailableFreeSpace }
            }
        } catch { }
    }
    if ($best) { return $best.Free }
    return [IO.DriveInfo]::new([IO.Path]::GetPathRoot($full)).AvailableFreeSpace
}

function Assert-HarnessWorktreeHasNoReparsePoint([string] $Path) {
    try { $root = Get-Item -LiteralPath $Path -Force -ErrorAction Stop }
    catch [System.Management.Automation.ItemNotFoundException] { return }
    $pending = [Collections.Generic.Queue[IO.FileSystemInfo]]::new()
    $pending.Enqueue($root)
    while ($pending.Count) {
        $entry = $pending.Dequeue()
        $entry.Refresh()
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "reparse point at '$($entry.FullName)' in the harness worktree; remove the link itself without touching its target, then retry"
        }
        # Never use recursive enumeration: inspect each directory's attributes
        # before descending, including junctions inside an untracked directory.
        if ($entry.Attributes -band [IO.FileAttributes]::Directory) {
            foreach ($child in $entry.EnumerateFileSystemInfos()) { $pending.Enqueue($child) }
        }
    }
}


if ($Head -notmatch '^[0-9a-f]{40}$' -or $Base -notmatch '^[0-9a-f]{40}$') { Stop-Run "the head and base must be full commit ids" }
if ($Ref -notmatch '^refs/heads/integrate/[0-9A-Za-z._-]+$') { Stop-Run "the bundle's ref must be an integrate/* branch, not '$Ref'" }
if ($Nonce -and $Nonce -notmatch '^[0-9a-f]{16,64}$') { Stop-Run "the nonce must be hex" }
if (-not (Test-Path -LiteralPath (Join-Path $Repo '.git'))) { Stop-Run "no git clone at the configured repo= path" }
$Repo = (Resolve-Path -LiteralPath $Repo).ProviderPath.TrimEnd([char]92, [char]47)
$noHooks = "$Repo.rust-harness-no-hooks"
if (Test-Path -LiteralPath $noHooks) { Stop-Run 'the no-hooks path exists; remove it before retrying' }
$hook = if ($ServicesHook) { $ServicesHook } else { Join-Path $HOME '.pseudolife-mcp/rust-harness-services.ps1' }
if (-not (Test-Path -LiteralPath $hook)) {
    Stop-Run "this machine has no services hook (~/.pseudolife-mcp/rust-harness-services.ps1) to start its test PostgreSQL and Linux Docker engine; docs/dev/dev-machines.md says what it must do"
}

$data = [Console]::In.ReadToEnd()
try { $bytes = [Convert]::FromBase64String($data) } catch { Stop-Run "the bundle on stdin is not base64" }
if ($bytes.Length -eq 0) { Stop-Run "no bundle arrived on stdin" }

$gate = "$Repo.rust-harness"
$venv = "$Repo.rust-harness-venv"
$lockPath = "$Repo.rust-harness.lock"
$bundle = "$Repo.rust-harness-$($Head.Substring(0, 12)).bundle"
$private = "refs/rust-harness/$Head"

$lock = $null
$suiteHold = $null
$databaseHold = $null
$deadline = [DateTimeOffset]::UtcNow.AddSeconds($LockWaitSeconds)
function Remaining-Wait { return [math]::Max(0, [int]($deadline - [DateTimeOffset]::UtcNow).TotalSeconds) }
$verdict = $null
$failure = $null
$started = $false
try {
    do {
        # A worker wrapper holds the database turn before its pytest child
        # takes the suite lock. Match that order so neither waits on the other.
        $databaseHold = Enter-DevDatabaseLock -Path $DatabaseLockPath -Holder "batch harness $Head" -WaitSeconds (Remaining-Wait)
        $locks = if ($env:PSEUDOLIFE_SUITE_LOCK_DIR) { $env:PSEUDOLIFE_SUITE_LOCK_DIR } else { Join-Path $HOME '.pseudolife-mcp/locks' }
        $null = [IO.Directory]::CreateDirectory($locks)
        $suitePath = Join-Path $locks 'full-suite.lock'
        $said = $false
        while (-not $suiteHold) {
            try {
                if ($IsWindows) {
                    $suiteHold = [IO.File]::Open($suitePath, 'OpenOrCreate', 'ReadWrite', 'ReadWrite')
                    $suiteHold.Lock(0, 1)
                } else { $suiteHold = [IO.File]::Open($suitePath, 'OpenOrCreate', 'ReadWrite', 'None') }
            } catch [IO.IOException] {
                if ($suiteHold) { $suiteHold.Dispose(); $suiteHold = $null }
                if ((Remaining-Wait) -eq 0) { throw 'full-suite lock wait exhausted; retry after the machine is released' }
                if (-not $said) {
                    $owner = 'another full-suite run'
                    $holderFile = Join-Path $locks 'full-suite.holder.json'
                    if (Test-Path -LiteralPath $holderFile) { $owner = [IO.File]::ReadAllText($holderFile) }
                    [Console]::Error.WriteLine("rust-harness: waiting for full-suite lock: $owner")
                    $said = $true
                }
                Start-Sleep -Milliseconds 200
            }
        }
        # The clone lock uses the same holder metadata and stale-identity rules.
        $lock = Enter-DevDatabaseLock -Path $lockPath -Holder "batch harness clone $Head" -WaitSeconds (Remaining-Wait)
        $free = (Get-FreeBytes (Split-Path -Parent $Repo)) / 1GB
        if ($free -lt $MinFreeGB) { throw "only $([math]::Round($free, 1)) GB free beside the clone; the floor is $MinFreeGB GB" }
        Assert-HarnessWorktreeHasNoReparsePoint $gate
        if ((Test-Path -LiteralPath $gate) -and -not (Test-Path -LiteralPath (Join-Path $gate '.git'))) {
            throw 'the harness worktree has no .git; inspect the interrupted worktree before retrying'
        }
        foreach ($stale in @(& git -C $Repo for-each-ref --format='%(refname)' refs/rust-harness/ 2>$null)) {
            if ($stale) { $null = Invoke-Git -C $Repo update-ref -d $stale }
        }
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
            Assert-HarnessWorktreeHasNoReparsePoint $gate
            $r = Invoke-Git -C $gate checkout --quiet --detach --force $Head
            if ($r.Code -eq 0) {
                Assert-HarnessWorktreeHasNoReparsePoint $gate
                $r = Invoke-Git -C $gate clean -ffdxq
            }
        } else {
            $null = Invoke-Git -C $Repo worktree prune
            $r = Invoke-Git -C $Repo worktree add --quiet --detach --force $gate $Head
        }
        if ($r.Code -ne 0) { $failure = "the harness worktree could not be set to the head (git exit $($r.Code): $($r.Text))"; break }
        $at = (& git -C $gate rev-parse HEAD 2>$null)
        if ("$at".Trim() -ne $Head) { $failure = "the harness worktree is at '$("$at".Trim())', not $Head"; break }

        # Editable installs and pytest execute member code. Withhold the SSH
        # session's credentials, as the compile far half does, and selection
        # controls inherited from an unrelated run. The services hook supplies
        # its disposable test login afterwards; do not scrub that login away.
        $secretName = '(?i)TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|DSN|DATABASE_URL|(^|_)PAT$|(^|_)(API_)?KEY(_ID)?$'
        $secretValue = '://[^/@\s]+:[^/@\s]*@'
        $testControl = '(?i)^(PYTEST_|NEXTEST_|CARGO_NEXTEST_)|^PYTHON(HOME|PATH|STARTUP|INSPECT)$'
        foreach ($v in @(Get-ChildItem env:)) {
            if ($v.Name -match $secretName -or "$($v.Value)" -match $secretValue -or $v.Name -match $testControl) {
                Remove-Item -LiteralPath "env:$($v.Name)" -ErrorAction Stop
            }
        }
        if (-not (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)) { $failure = "uv is not on PATH"; break }
        $py = $null
        foreach ($candidate in (Join-Path $venv 'Scripts/python.exe'), (Join-Path $venv 'bin/python')) {
            if (Test-Path -LiteralPath $candidate) { $py = $candidate; break }
        }
        if (-not $py) {
            & uv venv --quiet --seed --python 3.11 $venv
            if ($LASTEXITCODE -ne 0) { $failure = "uv could not make the environment (exit $LASTEXITCODE)"; break }
            foreach ($candidate in (Join-Path $venv 'Scripts/python.exe'), (Join-Path $venv 'bin/python')) {
                if (Test-Path -LiteralPath $candidate) { $py = $candidate; break }
            }
            if (-not $py) { $failure = "uv made no Python in $([IO.Path]::GetFileName($venv))"; break }
        }
        & uv pip install --quiet --python $py --torch-backend cpu -e "$gate[dev]"
        if ($LASTEXITCODE -ne 0) { $failure = "the head's dev dependencies did not install (uv exit $LASTEXITCODE)"; break }

        . $hook
        try {
            Start-HarnessServices
            $started = $true
        } catch {
            $started = $true    # stop whatever half started
            $failure = "the services hook could not start this machine's services: $($_.Exception.Message)"
            break
        }

        $env:PYTHONPATH = $gate
        $env:HF_HUB_OFFLINE = '1'
        $env:CUDA_VISIBLE_DEVICES = '-1'
        Push-Location -LiteralPath $gate
        try {
            & $py -m pytest -q -p no:cacheprovider --require-databases @PytestArgs
            $verdict = $LASTEXITCODE
        } finally {
            Pop-Location
        }
    } while ($false)
} catch {
    $failure = $_.Exception.Message
} finally {
    if ($started) {
        try { Stop-HarnessServices } catch { $failure = "stopping the services failed: $($_.Exception.Message); verify services before retrying" }
    }
    if ($lock) {
        $null = Invoke-Git -C $Repo update-ref -d $private
        Remove-Item -LiteralPath $bundle -Force -ErrorAction SilentlyContinue
    }
    Exit-DevDatabaseLock $lock
    if ($suiteHold) { $suiteHold.Dispose() }
    Exit-DevDatabaseLock $databaseHold
}
if ($null -ne $failure) { Stop-Run $failure }
[Console]::Error.WriteLine("$tag pytest exit $verdict")
exit $verdict
