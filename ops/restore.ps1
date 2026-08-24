#Requires -Version 7
# Restore a Pseudolife-MCP pg_dump backup (the inverse of ops\backup.ps1).
#
#   ops\restore.ps1                          # REHEARSAL (default, safe):
#                                            #   newest backup -> scratch db,
#                                            #   row-count report, drop scratch.
#   ops\restore.ps1 -BackupFile <path>       # rehearse a specific backup
#   ops\restore.ps1 -Apply                   # REAL RESTORE into the live db:
#                                            #   safety-dump current bank first,
#                                            #   stop daemon, drop+recreate db,
#                                            #   restore, start daemon, health.
#   ops\restore.ps1 -Apply -StateArchive <pseudolife_state-*.tgz>
#                                            # ALSO replace the daemon state
#                                            # volume (ingested documents +
#                                            # cortex/graph snapshots) from a
#                                            # backup.ps1 state tar. Opt-in:
#                                            # a DB-only restore must not
#                                            # clobber current state.
#
# The rehearsal NEVER touches the live database — it exists so the restore
# path is a rehearsed procedure, not a hope (2026-07-02 review P2: the only
# restore guidance in the repo was a code comment).
param(
    [string]$BackupFile = "",
    [string]$StateArchive = "",
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

if ($StateArchive) {
    if (-not (Test-Path $StateArchive) -or (Get-Item $StateArchive).Length -eq 0) {
        throw "state archive missing or empty: $StateArchive"
    }
    Write-Host "==> State archive: $StateArchive ($([math]::Round((Get-Item $StateArchive).Length/1KB)) KB)"
} else {
    $newestState = Get-ChildItem (Join-Path $repo "data\backups") -Filter "pseudolife_state-*.tgz" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newestState) {
        Write-Host "==> Note: state archives exist (newest: $($newestState.Name)); pass -StateArchive to restore ingested documents + cortex/graph snapshots too."
    }
}

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
        # Materiality threshold for the partial-loss check below. A dump is
        # always OLDER than the live bank, so restored < live is normal drift
        # — but losing most of a table is not drift. 0.5 is deliberately
        # loose, and the check only applies from 20 live rows up, because a
        # ratio over single-digit counts says nothing: 4 episodes vs 1 is
        # three sessions since the dump, not corruption.
        #
        # It is NOT drift-proof, and deliberately so: rehearsing a
        # deliberately old artifact (-BackupFile, a DR drill against the
        # mirror copy) against a table that has more than DOUBLED since it
        # was taken trips this. The alarm text says so, because a rehearsal
        # that stays quiet about a halved table is the failure this whole
        # check exists to stop; a loud, self-explaining false alarm is the
        # cheaper mistake.
        $partialRatio = 0.5
        $partialMinRows = 20
        $alarms = @()
        foreach ($t in $tables) {
            Write-Host ("{0,-14} {1,10} {2,10}" -f $t, $live[$t], $restored[$t])
            # Alarm only when the LIVE bank has rows the backup lost — an
            # absolute-zero check false-fails on a young bank (no dreams run
            # yet = legitimately 0 facts) and teaches users to distrust a
            # perfectly good backup. Every counted table is checked, not just
            # entries/facts: those two are the first and largest sections of
            # the dump, so a truncated artifact keeps them and loses every
            # lesson, episode, entity, edge and world fact behind them — and
            # that rehearsed "PASSED" until 2026-08-25.
            if ($live[$t] -lt 0 -or $restored[$t] -lt 0) {
                # -1 is Get-Counts' "the query failed" sentinel. Silence here
                # would be the original defect wearing a different hat: a
                # PASSED verdict for a comparison that never ran.
                $alarms += "$t (live=$($live[$t]) restored=$($restored[$t]): the row count failed, so nothing was compared)"
            }
            elseif ($live[$t] -gt 0 -and $restored[$t] -eq 0) {
                $alarms += "$t (live=$($live[$t]) restored=$($restored[$t]): the whole table is gone)"
            }
            elseif ($live[$t] -ge $partialMinRows -and
                    $restored[$t] -lt [math]::Floor($live[$t] * $partialRatio)) {
                $alarms += "$t (live=$($live[$t]) restored=$($restored[$t]): more than half the rows are missing)"
            }
        }
        docker exec $Container psql -q -U $User -d postgres -c "DROP DATABASE $scratch"
        if ($alarms.Count -gt 0) {
            throw ("the restored copy does not match the live bank: " +
                   ($alarms -join "; ") +
                   " - investigate before trusting this backup. (Rehearsing a deliberately OLD artifact? A table that more than doubled since the dump trips the 'more than half' check legitimately.)")
        }

        if ($StateArchive) {
            # Integrity-check the state tar (listing decompresses everything).
            Write-Host "==> Rehearsing state archive (list-only, nothing written)..."
            $stateTmp = "/tmp/pl_state_rehearse.tgz"
            docker cp $StateArchive "${DaemonContainer}:$stateTmp"
            if ($LASTEXITCODE -ne 0) { throw "docker cp into $DaemonContainer failed - is the daemon container present?" }
            $n = docker exec $DaemonContainer sh -c "tar tzf $stateTmp | wc -l"
            $tarOk = $LASTEXITCODE -eq 0
            docker exec $DaemonContainer rm -f $stateTmp
            if (-not $tarOk) { throw "state archive is unreadable - investigate before trusting it" }
            Write-Host "==> State archive OK ($($n.Trim()) entries)."
        }
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
        # Evict leftover sessions first: DROP DATABASE fails outright while
        # ANY client is connected (a psql window left open, a Console tab, a
        # daemon connection that outlived `docker stop`). Both calls used to
        # run with their exit status ignored, so a blocked drop surfaced two
        # steps later as "restore failed mid-way" against the OLD database —
        # alarming, and about the wrong thing. pg_backend_pid() excludes this
        # very session; nothing outside the target database is touched.
        docker exec $Container psql -q -U $User -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$Db' AND pid <> pg_backend_pid()" *> $null
        docker exec $Container psql -q -U $User -d postgres -c "DROP DATABASE IF EXISTS $Db"
        if ($LASTEXITCODE -ne 0) {
            throw "DROP DATABASE $Db failed (something is still connected to it). The live bank is UNTOUCHED; restart the daemon with 'docker start $DaemonContainer' when you are done investigating."
        }
        docker exec $Container psql -q -U $User -d postgres -c "CREATE DATABASE $Db"
        if ($LASTEXITCODE -ne 0) {
            throw "CREATE DATABASE $Db failed after the drop - the bank no longer exists in Postgres. Re-run this restore (the dump is still at $BackupFile) before starting the daemon."
        }
        docker exec $Container sh -c "gunzip -c $tmp | psql -q -v ON_ERROR_STOP=1 -U $User -d $Db > /dev/null"
        if ($LASTEXITCODE -ne 0) { throw "RESTORE FAILED mid-way; daemon left stopped. The pre-restore safety dump is in data\backups." }

        if ($StateArchive) {
            # Replace /data (the state volume) while the daemon is stopped.
            # Runs the daemon's own image (already local, has tar) with
            # --volumes-from, so no volume-name resolution or image pull is
            # needed. The safety backup above already tarred the current state.
            Write-Host "==> Restoring the state volume from $StateArchive..."
            $img = docker inspect -f '{{.Config.Image}}' $DaemonContainer
            if ($LASTEXITCODE -ne 0) { throw "cannot inspect $DaemonContainer; state volume NOT restored, daemon left stopped" }
            $dir = (Get-Item $StateArchive).DirectoryName
            $name = Split-Path $StateArchive -Leaf
            docker run --rm --entrypoint sh --volumes-from $DaemonContainer `
                -v "${dir}:/pl_backup:ro" $img `
                -c "find /data -mindepth 1 -delete && tar xzf /pl_backup/$name -C /data"
            if ($LASTEXITCODE -ne 0) { throw "STATE RESTORE FAILED; daemon left stopped. The pre-restore safety state tar is in data\backups." }
        }

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
