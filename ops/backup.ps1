#Requires -Version 7
# pg_dump the PseudoLife-MCP database to data\backups\ with 7-day rotation,
# plus a tar of the daemon state volume (ChromaDB reference documents, cortex
# snapshot, graph snapshots) — the bank alone does not cover document_ingest.
#
#   ops\backup.ps1                 # dump into <repo>\data\backups
#   ops\backup.ps1 -KeepDays 30
#   ops\backup.ps1 -MirrorKeep 2   # cap the mirror at the newest 2 per kind
#
# Runs pg_dump INSIDE the container (no local postgres client needed).
param(
    [string]$Container = "pseudolife-mcp-postgres",
    [string]$DaemonContainer = "pseudolife-mcp-daemon",
    [string]$Db = "pseudolife_memory",
    [string]$User = "pseudolife",
    [string]$OutDir = "",
    [int]$KeepDays = 7,
    # Off-disk mirror (2026-07-02 review P2): backups on the same physical
    # disk as the bank die with it. Point this (or the env var) at a folder
    # on ANOTHER disk / synced share. Mirror failure warns, never throws —
    # the primary backup already succeeded and deploys must not abort
    # because a mirror drive is unplugged.
    [string]$MirrorDir = $env:PSEUDOLIFE_BACKUP_MIRROR,
    # Keep exactly the newest N files on the MIRROR, by filename stamp — the
    # mirror is typically a cloud-synced folder (Google Drive etc.) where
    # mtimes are untrustworthy and space is metered. 0 = the primary's
    # age-based KeepDays rotation (the pre-knob behavior).
    [ValidateRange(0, 10000)][int]$MirrorKeep = $(
        if ($env:PSEUDOLIFE_BACKUP_MIRROR_KEEP) { [int]$env:PSEUDOLIFE_BACKUP_MIRROR_KEEP } else { 0 })
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $repo "data\backups" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $OutDir "pseudolife_memory-$stamp.sql.gz"

Write-Host "Dumping $Db from container $Container -> $out"
# Dump + gzip INSIDE the container, then copy the artifact out. This avoids
# piping binary through PowerShell entirely — `Set-Content -Encoding Byte`
# was removed in PowerShell 7, and `>` redirection mangles bytes as UTF-16.
# -Fc would be smaller but plain+gzip is trivially restorable with psql.
$tmp = "/tmp/pl_backup-$stamp.sql.gz"
docker exec $Container sh -c "pg_dump -U $User -d $Db | gzip -9 > $tmp"
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed inside container $Container" }
docker cp "${Container}:$tmp" $out
docker exec $Container rm -f $tmp
if (-not (Test-Path $out) -or (Get-Item $out).Length -eq 0) {
    throw "backup artifact missing or empty: $out"
}

# State volume (ChromaDB reference documents + cortex snapshot + graph
# snapshots), tarred from inside the daemon container — the only place those
# live; a pg_dump alone loses every document_ingest on restore. Warn, never
# throw: the DB dump above is the critical artifact and deploys must not
# abort because the daemon happens to be stopped (the skip is loud).
$stateOut = Join-Path $OutDir "pseudolife_state-$stamp.tgz"
try {
    $stateTmp = "/tmp/pl_state-$stamp.tgz"
    docker exec $DaemonContainer sh -c "tar czf $stateTmp -C /data ."
    if ($LASTEXITCODE -ne 0) { throw "tar failed inside container $DaemonContainer" }
    docker cp "${DaemonContainer}:$stateTmp" $stateOut
    docker exec $DaemonContainer rm -f $stateTmp
    if (-not (Test-Path $stateOut) -or (Get-Item $stateOut).Length -eq 0) {
        throw "state artifact missing or empty: $stateOut"
    }
    Write-Host "State volume -> $stateOut"
} catch {
    Write-Warning "STATE VOLUME NOT BACKED UP (ingested documents + cortex/graph snapshots live there; is the daemon running?): $_"
}

# Rotation.
foreach ($pat in "pseudolife_memory-*.sql.gz", "pseudolife_state-*.tgz") {
    Get-ChildItem $OutDir -Filter $pat |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
        Remove-Item -Force
}

# Off-disk mirror (opt-in; KeepDays retention, or newest-N per kind with
# -MirrorKeep). Mirrors both artifacts: the DB dump and the state tar.
if ($MirrorDir) {
    try {
        New-Item -ItemType Directory -Force -Path $MirrorDir | Out-Null
        $artifacts = @($out)
        if (Test-Path $stateOut) { $artifacts += $stateOut }
        foreach ($a in $artifacts) {
            Copy-Item $a $MirrorDir -Force
            $m = Join-Path $MirrorDir (Split-Path $a -Leaf)
            if ((Test-Path $m) -and (Get-Item $m).Length -eq (Get-Item $a).Length) {
                Write-Host "Mirrored to $m"
            } else {
                Write-Warning "mirror copy missing or size mismatch: $m"
            }
        }
        foreach ($pat in "pseudolife_memory-*.sql.gz", "pseudolife_state-*.tgz") {
            if ($MirrorKeep -gt 0) {
                Get-ChildItem $MirrorDir -Filter $pat |
                    Sort-Object Name -Descending | Select-Object -Skip $MirrorKeep |
                    Remove-Item -Force
            } else {
                Get-ChildItem $MirrorDir -Filter $pat |
                    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
                    Remove-Item -Force
            }
        }
    } catch {
        Write-Warning "backup mirror failed (primary backup is safe): $_"
    }
}

Write-Host "Backup complete. Retained last $KeepDays days in $OutDir"
