# Run this checkout's test suite inside WSL instead of on native Windows.
#
#   pwsh ops/wsl-suite.ps1                      # the full suite
#   pwsh ops/wsl-suite.ps1 tests/test_bm25.py   # anything else pytest takes
#
# A full suite on native Windows starts Git Bash processes in bursts that
# stalled mouse input on the maintainer's host (tests/suite_lock.py carries
# the 2026-10-02 measurement). Inside WSL the same run never touches the
# Windows console subsystem. ops/wsl-suite.sh does the work on the Linux
# side: a per-checkout uv environment, then pytest; this wrapper converts
# the checkout path and forwards the settings the run needs.
#
# The run tests the checkout's committed HEAD from a copy on the Linux
# filesystem (see ops/wsl-suite.sh for why), so a checkout with uncommitted
# changes to tracked files is refused: commit first, which is also what a
# PR's "tested head" needs. PSEUDOLIFE_WSL_SUITE_SOURCE=worktree tests the
# working tree over /mnt/c instead (git-dependent tests then fail or skip).
#
# The lock a WSL run takes lives in the WSL home and cannot see a Windows
# run's lock, which is why a host that adopts this refuses native Windows
# full runs (PSEUDOLIFE_SUITE_WINDOWS / full-suite.windows). Every argument
# goes to pytest unchanged; PSEUDOLIFE_WSL_DISTRO picks the distribution
# (default: WSL's default one). The exit code is pytest's.

$ErrorActionPreference = 'Stop'
# Refusals exit 2 (usage); Write-Error would throw under Stop and exit 1.
function Fail([string]$message) { [Console]::Error.WriteLine("wsl-suite: $message"); exit 2 }

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$distro = @()
if ($env:PSEUDOLIFE_WSL_DISTRO) { $distro = @('-d', $env:PSEUDOLIFE_WSL_DISTRO) }

$linuxRoot = (& wsl.exe @distro -e wslpath -a ($root -replace '\\', '/'))
if ($LASTEXITCODE -ne 0 -or -not $linuxRoot) {
    Fail "could not map $root into WSL (is a distribution installed?)"
}

# What the run reads from the environment: the board mirror's bearer and
# daemon URL (so the run shows on the board as the full-suite lease), and the
# suite's own settings. /p translates a Windows path into a WSL one.
$forward = @(@(
    'PSEUDOLIFE_MCP_TOKEN', 'PSEUDOLIFE_MCP_DAEMON_URL', 'PSEUDOLIFE_AGENT_PROJECT',
    'PSEUDOLIFE_TEST_PG_PASSWORD', 'PSEUDOLIFE_TEST_PG_HOST_PORT', 'PSEUDOLIFE_TEST_DATABASE_URL',
    'PSEUDOLIFE_REQUIRE_TEST_POSTGRES', 'PSEUDOLIFE_TEST_EMBEDDER',
    'PSEUDOLIFE_SUITE_LOCK', 'PSEUDOLIFE_SUITE_SLOTS', 'PSEUDOLIFE_TEST_CUDA',
    'PSEUDOLIFE_SUITE_VENV', 'PSEUDOLIFE_SUITE_PYTHON', 'HF_HUB_OFFLINE'
) | Where-Object { Test-Path "env:$_" })
# The token file only when there is no token value: under /mnt/c it shows
# loose permissions, and the board client refuses a credential file that is
# not owner-only (seen 2026-10-02), so the run would leave the board.
$paths = @()
if (-not $env:PSEUDOLIFE_MCP_TOKEN -and $env:PSEUDOLIFE_MCP_TOKEN_FILE) {
    $paths = @('PSEUDOLIFE_MCP_TOKEN_FILE/p')
}
$copy = @{}
if ($env:PSEUDOLIFE_WSL_SUITE_SOURCE -ne 'worktree') {
    $dirty = git -C $root status --porcelain --untracked-files=no
    if ($LASTEXITCODE -ne 0) { Fail "$root is not a git checkout" }
    if ($dirty) {
        Fail ("$root has uncommitted changes; the run tests the committed HEAD, " +
            "so commit first (or set PSEUDOLIFE_WSL_SUITE_SOURCE=worktree)")
    }
    $copy = @{
        PSEUDOLIFE_SUITE_COMMIT     = (git -C $root rev-parse HEAD)
        PSEUDOLIFE_SUITE_GIT_COMMON = (Resolve-Path (git -C $root rev-parse --path-format=absolute --git-common-dir)).Path
        PSEUDOLIFE_SUITE_ENV_FILE   = (Join-Path $root 'ops\.env')
        PSEUDOLIFE_SUITE_NAME       = (Split-Path $root -Leaf)
    }
    $paths += @('PSEUDOLIFE_SUITE_GIT_COMMON/p', 'PSEUDOLIFE_SUITE_ENV_FILE/p')
    $forward += @('PSEUDOLIFE_SUITE_COMMIT', 'PSEUDOLIFE_SUITE_NAME')
}
$entries = @($forward) + @($paths)
$saved = $env:WSLENV
$savedCopy = @{}
foreach ($k in $copy.Keys) { $savedCopy[$k] = [Environment]::GetEnvironmentVariable($k) }
if ($saved) { $entries = @($saved) + $entries }
try {
    # Restored afterwards: run with `&` or dot-sourced, this script shares the
    # caller's environment, and a later wsl call there must not inherit it.
    $env:WSLENV = $entries -join ':'
    foreach ($k in $copy.Keys) { [Environment]::SetEnvironmentVariable($k, $copy[$k]) }
    & wsl.exe @distro --cd $linuxRoot -e bash ops/wsl-suite.sh @args
    $code = $LASTEXITCODE
} finally {
    $env:WSLENV = $saved
    foreach ($k in $savedCopy.Keys) { [Environment]::SetEnvironmentVariable($k, $savedCopy[$k]) }
}
exit $code
