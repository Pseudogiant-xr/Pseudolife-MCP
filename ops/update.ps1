#Requires -Version 7
# Safely update ONLY the PseudoLife-MCP daemon to the current checkout code.
# (v7 guard: Windows PowerShell 5.1 turns benign native stderr — e.g. docker
# inspecting a not-yet-built image tag — into a terminating NativeCommandError.)
#
#   ops\update.ps1                 # backup -> tag rollback -> daemon-only rebuild -> health
#   ops\update.ps1 -Tag pre-x      # name the rollback image tag suffix
#   ops\update.ps1 -NoBackup       # skip the pg_dump (NOT recommended)
#   ops\update.ps1 -KeepRollbacks 5  # rollback tags to retain (default 2)
#
# Rebuilds + recreates ONLY the daemon container (`--no-deps`), so Postgres and
# the extractor are never touched. The bank lives in EXTERNAL volumes; this never
# runs `down -v`. Run after `git pull` (or local edits) to deploy daemon changes.
param(
    [string]$Tag = "",
    [switch]$NoBackup,
    [int]$KeepRollbacks = 2
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repo "ops\docker-compose.yml"
$envFile = Join-Path $repo "ops\.env"
$overrideFile = Join-Path $repo "ops\docker-compose.override.yml"
$compose = @("-f", $composeFile)
# Scaffold the (gitignored) machine-local env from the example so its knobs
# are discoverable — every line ships commented, so this changes nothing.
$exampleFile = Join-Path $repo "ops\.env.example"
if (-not (Test-Path $envFile) -and (Test-Path $exampleFile)) {
    Copy-Item $exampleFile $envFile
    Write-Host "==> Scaffolded ops/.env from ops/.env.example (all values commented)."
}
# Machine-local overrides (e.g. a fine-tuned GGUF mount) live in the gitignored
# override file; explicit -f disables compose's auto-merge, so add it here.
if (Test-Path $overrideFile) { $compose += @("-f", $overrideFile) }
if (Test-Path $envFile) { $compose = @("--env-file", $envFile) + $compose }

# 1. Backup the bank (pg_dump inside the container) — the always-first rule.
if (-not $NoBackup) {
    Write-Host "==> Backing up the bank (pg_dump)..."
    & (Join-Path $PSScriptRoot "backup.ps1")
} else {
    Write-Warning "Skipping backup (-NoBackup)."
}

# 2. Tag the current daemon image so a bad build can be rolled back. The tag
#    is read from the compose file so this script never drifts from it.
$imageTag = (Select-String -Path $composeFile -Pattern 'image:\s*(pseudolife-daemon:\S+)').Matches[0].Groups[1].Value
if (-not $imageTag) { throw "could not find the pseudolife-daemon image tag in $composeFile" }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $Tag) { $Tag = "pre-update-$stamp" }
$rollback = "$imageTag-$Tag"
docker image inspect $imageTag *> $null
if ($LASTEXITCODE -eq 0) {
    docker tag $imageTag $rollback
    Write-Host "==> Tagged rollback image: $rollback"
} else {
    Write-Warning "No current $imageTag image to tag (first build?)."
}

# 2b. Retention: drop stale pre-* rollback tags beyond the newest N — one is
#     minted per deploy and they otherwise pile up without bound (~60 tags in
#     a 177GB docker_data.vhdx by 2026-07-14). The script never touches the
#     deployed tag or an image a running container uses; a retention hiccup
#     must not abort the deploy.
try {
    & (Join-Path $PSScriptRoot "prune-rollbacks.ps1") -Keep $KeepRollbacks -Repository ($imageTag -split ':')[0]
} catch {
    Write-Warning "Rollback-tag retention failed (deploy continues): $_"
}

# 3. Rebuild + recreate ONLY the daemon. `--no-deps` is what keeps Postgres and
#    the extractor untouched (without it, `up --build <svc>` recreates all three).
Write-Host "==> Rebuilding the daemon only (Postgres + extractor untouched)..."
docker compose @compose up -d --no-deps --build pseudolife-daemon
if ($LASTEXITCODE -ne 0) { throw "daemon rebuild failed" }

# 4. Wait for health.
Write-Host "==> Waiting for the daemon to report healthy..."
$h = $null
for ($i = 0; $i -lt 30; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 3
        if ($h.status -eq "ok") { break }
    } catch { Start-Sleep -Milliseconds 1500 }
    $h = $null
}
if ($h) {
    Write-Host "==> Healthy. schema=$($h.schema) persist_errors=$($h.persist_errors)"
    Write-Host "    Rolled-back deploy if ever needed:"
    Write-Host "      docker tag $rollback $imageTag"
    Write-Host "      docker compose -f `"$composeFile`" up -d --no-deps pseudolife-daemon"
} else {
    Write-Warning "Daemon did not report healthy. Logs: docker logs pseudolife-mcp-daemon"
    Write-Warning "Rollback: docker tag $rollback $imageTag; then re-run the up line above."
    exit 1
}
