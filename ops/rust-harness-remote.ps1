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
#   3. a fresh run-named uv environment beside the clone gets the head's
#      own dev dependencies and is removed safely after that run;
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
    # Base64 JSON of the controller's batch-head unittest selections.
    [string] $UnitCommands = '',
    # Optional fixture dependencies pinned by the batch-head parity workflow.
    [string] $ExtraRequirements = '',
    [string] $Nonce = '',
    # Tests point this at a stand-in; integrate.py never passes it.
    [string] $ServicesHook = '',
    [ValidateRange(0, 86400)] [int] $LockWaitSeconds = 1200,
    [ValidateRange(0, 86400)] [int] $DatabaseTurnWaitSeconds = 0,
    [string] $DatabaseLockPath = '',
    # Tests use private marker files; the controller never overrides this.
    [string] $TurnMarkerPath = '',
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
if ($Ref -cnotmatch '^refs/heads/(integrate|stacked)/[0-9A-Za-z._-]+$') { Stop-Run "the bundle's ref must be an integrate/* branch or a stacked/* branch, not '$Ref'" }
if ($Nonce -and $Nonce -notmatch '^[0-9a-f]{16,64}$') { Stop-Run "the nonce must be hex" }
$units = @()
try {
    if ($UnitCommands) {
        $unitJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($UnitCommands))
        if (-not $unitJson.TrimStart().StartsWith('[')) { throw 'the unittest selections must be an array' }
        $units = @(ConvertFrom-Json -InputObject $unitJson)
        foreach ($unit in $units) {
            if ($unit.runner -notin @('unittest-discover', 'unittest-file') -or -not $unit.paths.Count) {
                throw 'unknown or empty unittest selection'
            }
            foreach ($path in $unit.paths) {
                if ($path -notmatch '^[A-Za-z0-9_./-]+\.py$' -or '..' -in $path.Split('/')) {
                    throw 'a unittest file is not a literal relative Python path'
                }
            }
            if ($unit.runner -eq 'unittest-discover') {
                if ($unit.start -notmatch '^[A-Za-z0-9_./-]+$' -or '..' -in $unit.start.Split('/') -or
                    $unit.pattern -notmatch '^test_[A-Za-z0-9_.*?-]+\.py$') { throw 'unsupported unittest discovery selection' }
            } elseif ($unit.paths.Count -ne 1) { throw 'a direct unittest command must name one file' }
        }
    }
} catch { Stop-Run "cannot read the unittest selections: $($_.Exception.Message)" }
$requirements = @()
try {
    if ($ExtraRequirements) {
        $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ExtraRequirements))
        if (-not $json.TrimStart().StartsWith('[')) { throw 'fixture requirements must be an array' }
        $requirements = @(ConvertFrom-Json -InputObject $json)
        foreach ($requirement in $requirements) {
            if ($requirement -isnot [string] -or $requirement -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9.+!-]*$') {
                throw 'fixture requirements must be literal package==version pins'
            }
        }
    }
} catch { Stop-Run "cannot read fixture requirements: $($_.Exception.Message)" }
if (-not (Test-Path -LiteralPath (Join-Path $Repo '.git'))) { Stop-Run "no git clone at the configured repo= path" }
$Repo = (Resolve-Path -LiteralPath $Repo).ProviderPath.TrimEnd([char]92, [char]47)
$noHooks = "$Repo.rust-harness-no-hooks"
if (Test-Path -LiteralPath $noHooks) { Stop-Run 'the no-hooks path exists; remove it before retrying' }
$hook = if ($ServicesHook) { $ServicesHook } else { Join-Path $HOME '.pseudolife-mcp/rust-harness-services.ps1' }
if (-not (Test-Path -LiteralPath $hook)) {
    Stop-Run "this machine has no services hook (~/.pseudolife-mcp/rust-harness-services.ps1) to start its test PostgreSQL and Linux Docker engine; docs/dev/dev-machines.md says what it must do"
}
if (-not $TurnMarkerPath) { $TurnMarkerPath = Join-Path $HOME '.pseudolife-mcp/locks/test-postgres.turn-enabled' }
if (-not (Test-Path -LiteralPath $TurnMarkerPath -PathType Leaf)) {
    Stop-Run 'database turn marker is missing; configure this service-managing machine with: New-Item -ItemType File -Force -Path "$HOME/.pseudolife-mcp/locks/test-postgres.turn-enabled"'
}

$data = [Console]::In.ReadToEnd()
try { $bytes = [Convert]::FromBase64String($data) } catch { Stop-Run "the bundle on stdin is not base64" }
if ($bytes.Length -eq 0) { Stop-Run "no bundle arrived on stdin" }

$gate = "$Repo.rust-harness"
$environmentId = [guid]::NewGuid().ToString('N')
$venv = "$Repo.rust-harness-env-$environmentId"
$expectedVenv = [IO.Path]::GetFullPath($venv)
$environmentCreated = $false
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

function Get-HarnessLongPath([string] $Path) {
    if (-not ('HarnessPathSpelling' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class HarnessPathSpelling {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern uint GetLongPathName(string path, StringBuilder output, uint length);
    public static string Expand(string path) {
        var output = new StringBuilder(32768);
        uint length = GetLongPathName(path, output, (uint)output.Capacity);
        if (length == 0) throw new Win32Exception(Marshal.GetLastWin32Error());
        if (length >= output.Capacity) throw new InvalidOperationException("path is too long");
        return output.ToString();
    }
}
'@
    }
    [HarnessPathSpelling]::Expand($Path)
}

function Read-HarnessProcCommandLine([string] $Path) {
    [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($Path))
}

# The PID of the first other process whose argv names the path, or nothing.
function Find-HarnessProcPathUser {
    param([string] $EnvironmentPath, [string] $ProcRoot = '/proc')
    foreach ($processDir in [IO.Directory]::EnumerateDirectories($ProcRoot)) {
        $processNumber = [IO.Path]::GetFileName($processDir)
        if ($processNumber -notmatch '^\d+$' -or [int]$processNumber -eq $PID) { continue }
        try { $command = Read-HarnessProcCommandLine "$processDir/cmdline" }
        catch [IO.FileNotFoundException] { continue }
        catch [IO.DirectoryNotFoundException] { continue }
        catch [UnauthorizedAccessException] { continue }
        catch [IO.IOException] {
            # A process reaped after the listing fails its read (ESRCH); one
            # whose directory is still listed is not known to be gone.
            if ([IO.Directory]::Exists($processDir)) { throw }
            continue
        }
        if ($command.Contains($EnvironmentPath)) { return [int]$processNumber }
    }
}

function Remove-OwnedHarnessEnvironment {
    param([string] $EnvironmentPath, [string] $ExpectedPath, [bool] $Created,
          [string] $RunId, [string] $Repository)
    if (-not $Created) { return }
    if ([IO.Path]::GetFullPath($EnvironmentPath) -cne $ExpectedPath -or
        [IO.Path]::GetFileName($EnvironmentPath) -cne "$(Split-Path -Leaf $Repository).rust-harness-env-$RunId") {
        throw 'the harness environment no longer names the path this run created; inspect it before retrying'
    }
    Assert-HarnessWorktreeHasNoReparsePoint $EnvironmentPath
    if ($IsWindows) {
        # A child can use the environment through cwd or its own environment
        # variables without naming it in argv. The owned job covers those
        # children even after an intermediate launcher has exited.
        if ('DevDatabaseProcessTree' -as [type]) {
            $remaining = @([DevDatabaseProcessTree]::RemainingChildren())
            if ($remaining.Count) {
                throw "an owned run process remains (PID $($remaining -join ', ')); inspect it before removing the environment"
            }
        }
        $longEnvironment = Get-HarnessLongPath $EnvironmentPath
        $users = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.ProcessId -ne $PID -and (
                ($_.CommandLine -and $_.CommandLine.IndexOf($EnvironmentPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
                ($_.CommandLine -and $_.CommandLine.IndexOf($longEnvironment, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
                ($_.ExecutablePath -and ($_.ExecutablePath.StartsWith($EnvironmentPath + '\', [StringComparison]::OrdinalIgnoreCase) -or
                                        $_.ExecutablePath.StartsWith($longEnvironment + '\', [StringComparison]::OrdinalIgnoreCase))))
        })
        if ($users.Count) { throw 'a process still uses this run''s harness environment; inspect the owned process before retrying' }
    } else {
        if ($null -ne (Find-HarnessProcPathUser -EnvironmentPath $EnvironmentPath)) {
            throw 'a process still uses this run''s harness environment; inspect it before retrying'
        }
    }
    # This exact fresh path was created under the clone lock. No link is
    # followed and no active process may reference it when it is removed.
    Remove-Item -LiteralPath $EnvironmentPath -Recurse -Force -ErrorAction Stop
}

try {
    do {
        Initialize-DevDatabaseProcessTree
        # A worker wrapper holds the database turn before its pytest child
        # takes the suite lock. Match that order so neither waits on the other.
        $databaseHold = Enter-DevDatabaseLock -Path $DatabaseLockPath -Holder "batch harness $Head" -WaitSeconds $DatabaseTurnWaitSeconds
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
        $environmentClock = [Diagnostics.Stopwatch]::StartNew()
        if (Test-Path -LiteralPath $venv) { $failure = 'the new run environment path already exists; inspect it before retrying'; break }
        $null = New-Item -ItemType Directory -Path $venv -ErrorAction Stop
        $environmentCreated = $true
        & uv venv --quiet --seed --python 3.11 $venv
        if ($LASTEXITCODE -ne 0) { $failure = "uv could not make the environment (exit $LASTEXITCODE)"; break }
        $py = $null
        foreach ($candidate in (Join-Path $venv 'Scripts/python.exe'), (Join-Path $venv 'bin/python')) {
            if (Test-Path -LiteralPath $candidate) { $py = $candidate; break }
        }
        if (-not $py) { $failure = "uv made no Python in $([IO.Path]::GetFileName($venv))"; break }
        & uv pip install --quiet --python $py --torch-backend cpu -e "$gate[dev]" @requirements
        if ($LASTEXITCODE -ne 0) { $failure = "the head's dev dependencies did not install (uv exit $LASTEXITCODE)"; break }
        $environmentClock.Stop()
        [Console]::Out.WriteLine("rust-harness: fresh environment creation and install: $([math]::Round($environmentClock.Elapsed.TotalSeconds, 3)) s")

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
            if ($verdict -eq 0) {
                foreach ($unit in $units) {
                    $unitPaths = @($unit.paths)
                    if ($unit.runner -eq 'unittest-discover') {
                        $unitArgs = @('-m', 'unittest', 'discover', '-s', $unit.start, '-p', $unit.pattern)
                    } else { $unitArgs = @($unitPaths[0]) }
                    $unitClock = [Diagnostics.Stopwatch]::StartNew()
                    $unitOutput = [Collections.Generic.List[string]]::new()
                    & $py @unitArgs 2>&1 | ForEach-Object {
                        $unitOutput.Add("$_")
                        [Console]::Out.WriteLine("$_")
                    }
                    $verdict = $LASTEXITCODE
                    $unitClock.Stop()
                    if ($verdict -ne 0) { break }
                    $text = $unitOutput -join "`n"
                    $ran = [regex]::Match($text, '(?m)^Ran ([1-9][0-9]*) tests? in ')
                    if (-not $ran.Success) { throw 'the unittest runner reported no executed cases; inspect its selection before retrying' }
                    $skip = [regex]::Match($text, '(?m)^OK \(skipped=([0-9]+)\)')
                    $skipped = if ($skip.Success) { [int]$skip.Groups[1].Value } else { 0 }
                    $passed = [int]$ran.Groups[1].Value - $skipped
                    if ($passed -lt 1) { throw 'the unittest selection skipped every case; fix its prerequisites before retrying' }
                    [Console]::Out.WriteLine("$passed passed, $skipped skipped in $([math]::Round($unitClock.Elapsed.TotalSeconds, 3))s (unittest)")
                }
            }
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
    try {
        Remove-OwnedHarnessEnvironment -EnvironmentPath $venv -ExpectedPath $expectedVenv -Created $environmentCreated -RunId $environmentId -Repository $Repo
        $environmentCreated = $false
    } catch { $failure = "the owned environment was not removed safely: $($_.Exception.Message)" }
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
