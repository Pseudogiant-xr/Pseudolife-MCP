# Restore a PseudoLife-MCP pg_dump backup (the inverse of ops\backup.ps1).
#
#   ops\restore.ps1                          # REHEARSAL (default, safe):
#                                            #   newest backup -> scratch db,
#                                            #   row-count report, drop scratch.
#   ops\restore.ps1 -BackupFile <path>       # rehearse a specific backup
#   ops\restore.ps1 -Apply                   # REAL RESTORE into the live db:
#                                            #   safety-dump current bank first,
#                                            #   stop daemon, drop+recreate db,
#                                            #   restore, start daemon, health.
#
# The rehearsal NEVER touches the live database — it exists so the restore
# path is a rehearsed procedure, not a hope (2026-07-02 review P2: the only
# restore guidance in the repo was a code comment).
param(
    [string]$BackupFile = "",
    [string]$Container = "pseudolife-mcp-postgres",
    [string]$DaemonContainer = "pseudolife-mcp-daemon",
    [string]$Db = "pseudolife_memory",
    [string]$User = "pseudolife",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

# 1. Resolve + validate the backup artifact.
if (-not $BackupFile) {
    $newest = Get-ChildItem (Join-Path $repo "data\backups") -Filter "pseudolife_memory-*.sql.gz" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { throw "no backups found under data\backups" }
    $BackupFile = $newest.FullName
}
if (-not (Test-Path $BackupFile) -or (Get-Item $BackupFile).Length -eq 0) {
    throw "backup artifact missing or empty: $BackupFile"
}
Write-Host "==> Backup: $BackupFile ($([math]::Round((Get-Item $BackupFile).Length/1KB)) KB)"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tmp = "/tmp/pl_restore-$stamp.sql.gz"
docker cp $BackupFile "${Container}:$tmp"
if ($LASTEXITCODE -ne 0) { throw "docker cp into $Container failed" }

$tables = @("entries", "facts", "world_facts", "lessons", "entities", "edges", "episodes")

function Get-Counts([string]$database) {
    $counts = [ordered]@{}
    foreach ($t in $tables) {
        $c = docker exec $Container psql -tA -U $User -d $database -c "SELECT count(*) FROM $t" 2>$null
        $counts[$t] = if ($LASTEXITCODE -eq 0) { [int]$c } else { -1 }
    }
    return $counts
}

try {
    if (-not $Apply) {
        # ── REHEARSAL: restore into a scratch db, compare, drop ──────────
        $scratch = "pseudolife_restore_rehearsal"
        Write-Host "==> Rehearsal: restoring into scratch db '$scratch' (live bank untouched)"
        docker exec $Container psql -q -U $User -d postgres -c "DROP DATABASE IF EXISTS $scratch"
        docker exec $Container psql -q -U $User -d postgres -c "CREATE DATABASE $scratch"
        docker exec $Container sh -c "gunzip -c $tmp | psql -q -v ON_ERROR_STOP=1 -U $User -d $scratch > /dev/null"
        if ($LASTEXITCODE -ne 0) { throw "restore into scratch db FAILED - the backup may be unusable" }

        $live = Get-Counts $Db
        $restored = Get-Counts $scratch
        Write-Host ("{0,-14} {1,10} {2,10}" -f "table", "live", "restored")
        $anyEmpty = $false
        foreach ($t in $tables) {
            Write-Host ("{0,-14} {1,10} {2,10}" -f $t, $live[$t], $restored[$t])
            if ($t -in @("entries", "facts") -and $restored[$t] -le 0) { $anyEmpty = $true }
        }
        docker exec $Container psql -q -U $User -d postgres -c "DROP DATABASE $scratch"
        if ($anyEmpty) { throw "restored bank has empty entries/facts - investigate before trusting this backup" }
        Write-Host "==> Rehearsal PASSED: the backup restores cleanly. (Counts differ from live only by writes since the dump.)"
    }
    else {
        # ── REAL RESTORE ─────────────────────────────────────────────────
        Write-Warning "REAL RESTORE: this REPLACES the live bank '$Db' with $BackupFile"
        Write-Host "==> Safety-dumping the current bank first..."
        & (Join-Path $PSScriptRoot "backup.ps1")

        Write-Host "==> Stopping the daemon..."
        docker stop $DaemonContainer | Out-Null

        Write-Host "==> Dropping + recreating $Db..."
        docker exec $Container psql -q -U $User -d postgres -c "DROP DATABASE IF EXISTS $Db"
        docker exec $Container psql -q -U $User -d postgres -c "CREATE DATABASE $Db"
        docker exec $Container sh -c "gunzip -c $tmp | psql -q -v ON_ERROR_STOP=1 -U $User -d $Db > /dev/null"
        if ($LASTEXITCODE -ne 0) { throw "RESTORE FAILED mid-way; daemon left stopped. The pre-restore safety dump is in data\backups." }

        Write-Host "==> Restarting the daemon..."
        docker start $DaemonContainer | Out-Null
        $h = $null
        for ($i = 0; $i -lt 30; $i++) {
            try {
                $h = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 3
                if ($h.status -eq "ok") { break }
            } catch { Start-Sleep -Milliseconds 1500 }
            $h = $null
        }
        if (-not $h) { throw "daemon did not report healthy after restore - check docker logs $DaemonContainer" }
        Write-Host "==> Restore complete. /health: status=$($h.status) schema=$($h.schema) db=$($h.db)"
    }
}
finally {
    docker exec $Container rm -f $tmp
}
