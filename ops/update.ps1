#Requires -Version 7
# Safely update ONLY the Pseudolife-MCP daemon to the current checkout code.
#
#   ops\update.ps1                 # backup -> tag rollback -> daemon-only rebuild -> health
#   ops\update.ps1 -Tag pre-x      # name the rollback image tag suffix
#   ops\update.ps1 -NoBackup       # skip the pg_dump (NOT recommended)
#   ops\update.ps1 -KeepRollbacks 5  # rollback tags to retain (default 2)
#   ops\update.ps1 -KeepCacheHours 24 # build cache to retain, hours (default 168)
#   ops\update.ps1 -NoCachePrune     # skip build-cache retention entirely
#   ops\update.ps1 -HealthRetries 30 -HealthDelayMs 1500  # health-wait budget
#   ops\update.ps1 -ForceRollbackTag # tag the rollback even when the version
#                                    # tag is not the running daemon's image
#   ops\update.ps1 -All              # after the daemon: shim, plugin cache,
#                                    # Codex hooks
#   ops\update.ps1 -AllowDirty       # deploy a tree with uncommitted or
#                                    # untracked files (stamped dirty=true),
#                                    # or one git cannot describe (unknown)
#   ops\update.ps1 -NoTestLogin      # do not create the test suite's own
#                                    # Postgres login (made when it is
#                                    # missing and the bundled Postgres
#                                    # runs here: a contributor's host)
#   ops\update.ps1 -ClientsOnly      # not here: python ops/update_clients.py
#
# The deploy itself is pseudolife_memory/update_cli.py — the same code
# `pseudolife-mcp update` runs from an installed package with no checkout.
# This script only maps its flags and runs that code from THIS checkout
# (ops/update.py puts the checkout ahead of any installed package). It
# rebuilds + recreates ONLY the daemon container (`--no-deps`), so Postgres
# and the extractor are never touched; the bank lives in EXTERNAL volumes
# and nothing here ever runs `down -v`. Run after `git pull`; local edits
# must be committed first (or deployed with -AllowDirty).
param(
    [string]$Tag = "",
    [switch]$NoBackup,
    [switch]$ForceRollbackTag,
    [int]$KeepRollbacks = 2,
    [int]$KeepCacheHours = 168,
    [switch]$NoCachePrune,
    [int]$HealthRetries = 30,
    [int]$HealthDelayMs = 1500,
    [switch]$All,
    [switch]$AllowDirty,
    [switch]$NoTestLogin,
    [switch]$ClientsOnly
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

# A release-mode option of `pseudolife-mcp update`; this script deploys the checkout.
if ($ClientsOnly) {
    [Console]::Error.WriteLine("-ClientsOnly is a release-mode option; a checkout deploy builds what the tree holds (for the clients alone: python ops/update_clients.py)")
    exit 2
}

# A python that answers (the Store's `python` stub on PATH exits 9009), 3.10 or newer.
$python = $null
$pyArgs = @()
foreach ($candidate in @("python", "python3", "py")) {
    if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    $probeArgs = if ($candidate -eq "py") { @("-3") } else { @() }
    try {
        & $candidate @probeArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; $pyArgs = $probeArgs; break }
    } catch { continue }
}
if (-not $python) {
    Write-Warning "No Python >= 3.10 on PATH: the deploy is Python (ops/update.py). Install one and re-run."
    exit 1
}

$updateArgs = @("--checkout", $repo, "--keep-rollbacks", "$KeepRollbacks", "--keep-cache-hours", "$KeepCacheHours",
                "--health-retries", "$HealthRetries", "--health-delay-ms", "$HealthDelayMs")
if ($Tag) { $updateArgs += @("--rollback-tag", $Tag) }
if ($NoBackup) { $updateArgs += "--no-backup" }
if ($ForceRollbackTag) { $updateArgs += "--force-rollback-tag" }
if ($NoCachePrune) { $updateArgs += "--no-cache-prune" }
if ($All) { $updateArgs += "--all" }
if ($AllowDirty) { $updateArgs += "--allow-dirty" }
if ($NoTestLogin) { $updateArgs += "--no-test-login" }

& $python @pyArgs (Join-Path $PSScriptRoot "update.py") @updateArgs
exit $LASTEXITCODE
