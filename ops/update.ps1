# Safely update ONLY the PseudoLife-MCP daemon to the current checkout code.
#
#   ops\update.ps1                 # backup -> tag rollback -> daemon-only rebuild -> health
#   ops\update.ps1 -Tag pre-x      # name the rollback image tag suffix
#   ops\update.ps1 -NoBackup       # skip the pg_dump (NOT recommended)
#
# Rebuilds + recreates ONLY the daemon container (`--no-deps`), so Postgres and
# the extractor are never touched. The bank lives in EXTERNAL volumes; this never
# runs `down -v`. Run after `git pull` (or local edits) to deploy daemon changes.
param(
    [string]$Tag = "",
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repo "ops\docker-compose.yml"
$envFile = Join-Path $repo "ops\.env"
$compose = @("-f", $composeFile)
if (Test-Path $envFile) { $compose = @("--env-file", $envFile) + $compose }

# 1. Backup the bank (pg_dump inside the container) — the always-first rule.
if (-not $NoBackup) {
    Write-Host "==> Backing up the bank (pg_dump)..."
    & (Join-Path $PSScriptRoot "backup.ps1")
} else {
    Write-Warning "Skipping backup (-NoBackup)."
}

# 2. Tag the current daemon image so a bad build can be rolled back.
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $Tag) { $Tag = "pre-update-$stamp" }
$rollback = "pseudolife-daemon:0.2.0-$Tag"
docker image inspect pseudolife-daemon:0.2.0 *> $null
if ($LASTEXITCODE -eq 0) {
    docker tag pseudolife-daemon:0.2.0 $rollback
    Write-Host "==> Tagged rollback image: $rollback"
} else {
    Write-Warning "No current pseudolife-daemon:0.2.0 image to tag (first build?)."
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
    Write-Host "      docker tag $rollback pseudolife-daemon:0.2.0"
    Write-Host "      docker compose -f `"$composeFile`" up -d --no-deps pseudolife-daemon"
} else {
    Write-Warning "Daemon did not report healthy. Logs: docker logs pseudolife-mcp-daemon"
    Write-Warning "Rollback: docker tag $rollback pseudolife-daemon:0.2.0; then re-run the up line above."
    exit 1
}
