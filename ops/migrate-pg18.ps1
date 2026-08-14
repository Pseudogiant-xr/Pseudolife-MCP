# Migrate the Docker tier's bank from PostgreSQL 16 to 18 (dump/restore).
#
# A PG major bump cannot reuse the old data volume (on-disk format change),
# so this script performs the full cutover with verification at every step:
#
#   1. preflight  — compose tree says pseudolife-pg:18, live server is 16,
#                   the new volume does not exist yet
#   2. backup     — ops/backup.ps1 (full dump + state archive, as always)
#   3. quiesce    — stop the daemon, take the final cutover dump, record
#                   exact table counts
#   4. swap       — stop PG 16 (its volume is frozen and RETAINED as the
#                   rollback), create the new volume, point ops/.env at it,
#                   build + start pseudolife-pg:18
#   5. restore    — replay the cutover dump under ON_ERROR_STOP
#   6. verify     — table counts must match the quiesced counts EXACTLY;
#                   schema_version and pgvector checked; daemon restarted
#                   and health-polled
#
# Rollback at any point after step 4: stop the stack, restore the previous
# PSEUDOLIFE_BANK_VOLUME in ops/.env, check out the pg16 compose/Dockerfile,
# `docker compose up -d`. The old volume is never modified or deleted.
#
# Usage (from the repo root, after the PG18 change is merged):
#   pwsh ops/migrate-pg18.ps1                # full run
#   pwsh ops/migrate-pg18.ps1 -NewVolume pseudolife-mcp-bank-pg18

[CmdletBinding()]
param(
    [string]$NewVolume = "pseudolife-mcp-bank-pg18",
    [string]$PgContainer = "pseudolife-mcp-postgres",
    [string]$DaemonContainer = "pseudolife-mcp-daemon",
    [string]$Db = "pseudolife_memory",
    [string]$User = "pseudolife"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $repo "ops\docker-compose.yml"
$envFile = Join-Path $repo "ops\.env"
$backups = Join-Path $repo "data\backups"

function Fail([string]$msg) { Write-Host "ABORT: $msg" -ForegroundColor Red; exit 1 }

$countsQuery = @"
SELECT 'entries', count(*) FROM entries UNION ALL
SELECT 'facts', count(*) FROM facts UNION ALL
SELECT 'world_facts', count(*) FROM world_facts UNION ALL
SELECT 'lessons', count(*) FROM lessons UNION ALL
SELECT 'entities', count(*) FROM entities UNION ALL
SELECT 'edges', count(*) FROM edges UNION ALL
SELECT 'episodes', count(*) FROM episodes UNION ALL
SELECT 'outcome_signals', count(*) FROM outcome_signals ORDER BY 1
"@

# --- 1. preflight -----------------------------------------------------------
if (-not (Select-String -Path $compose -Pattern 'image:\s*pseudolife-pg:18' -Quiet)) {
    Fail "compose file does not say pseudolife-pg:18 — run this from the merged PG18 tree."
}
$liveMajor = (docker exec $PgContainer psql -U $User -d $Db -t -A -c "SHOW server_version_num") 2>$null
if (-not $liveMajor) { Fail "cannot reach the live Postgres in $PgContainer." }
$liveMajor = [int]($liveMajor.Trim()) / 10000 -as [int]
if ($liveMajor -ge 18) { Fail "live server is already PG $liveMajor — nothing to migrate." }
if (docker volume inspect $NewVolume 2>$null) {
    Fail "volume $NewVolume already exists — inspect it (and remove it yourself if it is a dead earlier attempt); this script never deletes volumes."
}
Write-Host "preflight ok: live PG $liveMajor -> 18, target volume $NewVolume" -ForegroundColor Cyan

# --- 2. full backup ---------------------------------------------------------
& (Join-Path $repo "ops\backup.ps1")
if ($LASTEXITCODE -ne 0) { Fail "ops/backup.ps1 failed — not proceeding without a fresh backup." }

# --- 3. quiesce + cutover dump ---------------------------------------------
Write-Host "stopping daemon (write quiesce)..." -ForegroundColor Cyan
docker stop $DaemonContainer | Out-Null
$before = docker exec $PgContainer psql -U $User -d $Db -t -A -F ':' -c $countsQuery
if ($LASTEXITCODE -ne 0) { docker start $DaemonContainer | Out-Null; Fail "count query failed." }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$cutDump = "pseudolife_memory-pg18cut-$stamp.sql.gz"
docker exec $PgContainer sh -c "pg_dump -U $User -d $Db | gzip -9 > /tmp/$cutDump"
if ($LASTEXITCODE -ne 0) { docker start $DaemonContainer | Out-Null; Fail "cutover pg_dump failed." }
New-Item -ItemType Directory -Force $backups | Out-Null
docker cp "${PgContainer}:/tmp/$cutDump" (Join-Path $backups $cutDump)
Write-Host "cutover dump: $cutDump" -ForegroundColor Cyan

# --- 4. swap ----------------------------------------------------------------
Write-Host "stopping PG 16 (volume frozen as rollback)..." -ForegroundColor Cyan
docker stop $PgContainer | Out-Null
docker rm $PgContainer | Out-Null
docker volume create $NewVolume | Out-Null
# Point ops/.env at the new volume (create/replace the single line).
$lines = @()
if (Test-Path $envFile) {
    $lines = Get-Content $envFile | Where-Object { $_ -notmatch '^\s*PSEUDOLIFE_BANK_VOLUME=' }
}
$lines += "PSEUDOLIFE_BANK_VOLUME=$NewVolume"
Set-Content -Path $envFile -Value $lines
Write-Host "ops/.env: PSEUDOLIFE_BANK_VOLUME=$NewVolume" -ForegroundColor Cyan
docker compose -f $compose build pseudolife-pg
if ($LASTEXITCODE -ne 0) { Fail "pg18 image build failed (old stack is stopped; rollback: restore .env, git checkout pg16 tree, compose up)." }
docker compose -f $compose up -d pseudolife-pg
if ($LASTEXITCODE -ne 0) { Fail "pg18 start failed (rollback: restore .env, git checkout pg16 tree, compose up)." }
$deadline = (Get-Date).AddSeconds(90)
do { Start-Sleep -Seconds 2; docker exec $PgContainer pg_isready -U $User -d $Db 2>$null | Out-Null }
while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
if ($LASTEXITCODE -ne 0) { Fail "pg18 never became ready." }

# --- 5. restore -------------------------------------------------------------
docker cp (Join-Path $backups $cutDump) "${PgContainer}:/tmp/$cutDump"
docker exec $PgContainer sh -c "gunzip -c /tmp/$cutDump | psql -U $User -d $Db -v ON_ERROR_STOP=1 -q"
if ($LASTEXITCODE -ne 0) { Fail "restore FAILED — old volume + backups intact; do not retry blindly, read the psql error above." }

# --- 6. verify --------------------------------------------------------------
$after = docker exec $PgContainer psql -U $User -d $Db -t -A -F ':' -c $countsQuery
if (($before -join "`n") -ne ($after -join "`n")) {
    Write-Host "BEFORE:`n$($before -join "`n")`nAFTER:`n$($after -join "`n")"
    Fail "table counts differ after restore — investigate before starting the daemon."
}
$schema = (docker exec $PgContainer psql -U $User -d $Db -t -A -c "SELECT value FROM meta WHERE key='schema_version'").Trim()
$vec = (docker exec $PgContainer psql -U $User -d $Db -t -A -c "SELECT extversion FROM pg_extension WHERE extname='vector'").Trim()
$ver = (docker exec $PgContainer psql -U $User -d $Db -t -A -c "SHOW server_version").Trim()
Write-Host "restored: PG $ver, pgvector $vec, schema_version $schema, counts exact-match" -ForegroundColor Green

docker compose -f $compose up -d pseudolife-daemon
$deadline = (Get-Date).AddSeconds(120)
$health = $null
do {
    Start-Sleep -Seconds 3
    try { $health = Invoke-RestMethod "http://127.0.0.1:8765/health" -TimeoutSec 3 } catch { $health = $null }
} while ((-not $health -or $health.status -ne "ok") -and (Get-Date) -lt $deadline)
if (-not $health -or $health.status -ne "ok") { Fail "daemon did not report healthy — check its logs; the bank itself verified clean." }
Write-Host ("daemon healthy: schema {0}, storage {1}, db {2}" -f $health.schema, $health.storage, $health.db) -ForegroundColor Green
Write-Host ""
Write-Host "PG 18 cutover complete. Old PG 16 volume retained untouched as rollback." -ForegroundColor Green
Write-Host "Next: exercise a real memory_search through a client, then keep the old volume for at least one comfortable soak period."
