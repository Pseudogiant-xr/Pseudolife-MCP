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
# The lock a WSL run takes lives in the WSL home and cannot see a Windows
# run's lock, which is why a host that adopts this refuses native Windows
# full runs (PSEUDOLIFE_SUITE_WINDOWS / full-suite.windows). Every argument
# goes to pytest unchanged; PSEUDOLIFE_WSL_DISTRO picks the distribution
# (default: WSL's default one). The exit code is pytest's.

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$distro = @()
if ($env:PSEUDOLIFE_WSL_DISTRO) { $distro = @('-d', $env:PSEUDOLIFE_WSL_DISTRO) }

$linuxRoot = (& wsl.exe @distro -e wslpath -a ($root -replace '\\', '/'))
if ($LASTEXITCODE -ne 0 -or -not $linuxRoot) {
    Write-Error "wsl-suite: could not map $root into WSL (is a distribution installed?)"
    exit 2
}

# What the run reads from the environment: the board mirror's bearer and
# daemon URL (so the run shows on the board as the full-suite lease), and the
# suite's own settings. /p translates a Windows path into a WSL one.
$forward = @(
    'PSEUDOLIFE_MCP_TOKEN', 'PSEUDOLIFE_MCP_DAEMON_URL', 'PSEUDOLIFE_AGENT_PROJECT',
    'PSEUDOLIFE_TEST_PG_PASSWORD', 'PSEUDOLIFE_TEST_DATABASE_URL',
    'PSEUDOLIFE_REQUIRE_TEST_POSTGRES', 'PSEUDOLIFE_TEST_EMBEDDER',
    'PSEUDOLIFE_SUITE_LOCK', 'PSEUDOLIFE_SUITE_SLOTS', 'PSEUDOLIFE_TEST_CUDA',
    'PSEUDOLIFE_SUITE_VENV', 'PSEUDOLIFE_SUITE_PYTHON', 'HF_HUB_OFFLINE'
) | Where-Object { Test-Path "env:$_" }
$paths = @('PSEUDOLIFE_MCP_TOKEN_FILE') | Where-Object { Test-Path "env:$_" } |
    ForEach-Object { "$_/p" }
$entries = @($forward) + @($paths)
if ($env:WSLENV) { $entries = @($env:WSLENV) + $entries }
$env:WSLENV = $entries -join ':'

& wsl.exe @distro --cd $linuxRoot -e bash ops/wsl-suite.sh @args
exit $LASTEXITCODE
