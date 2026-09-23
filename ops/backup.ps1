#Requires -Version 7
# pg_dump the Pseudolife-MCP database to data\backups\ with 7-day rotation,
# plus a tar of the daemon state volume (ChromaDB reference documents, cortex
# snapshot, graph snapshots) — the bank alone does not cover document_ingest.
#
#   ops\backup.ps1                 # dump into <repo>\data\backups
#   ops\backup.ps1 -KeepDays 30
#   ops\backup.ps1 -MirrorKeep 2   # cap the mirror at the newest 2 per kind
#   ops\backup.ps1 -AcceptRowDrop  # rotate despite a held row-count gate
#
# Runs pg_dump INSIDE the container (no local postgres client needed).
# Each dump gets a pseudolife_manifest-<stamp>.json of its per-table row
# counts; a copy goes into the daemon, where /health reports it as
# last_backup.
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
        if ($env:PSEUDOLIFE_BACKUP_MIRROR_KEEP) { [int]$env:PSEUDOLIFE_BACKUP_MIRROR_KEEP } else { 0 }),
    # Row-count gate (2026-09-23 review): rotation used to promote any
    # complete dump, however empty, so a logical wipe followed by MirrorKeep
    # backups rotated every good copy off the mirror. When entries, facts or
    # lessons fell by more than this percentage against the last manifest
    # that was not itself held, the new dump is kept and nothing older is
    # deleted, locally or on the mirror. The legitimate drops seen so far
    # are in other tables (dream_run_slots 642 -> 526, 09-20..09-23), so a
    # quarter of a gated table is far outside normal churn.
    [ValidateRange(0, 100)][int]$MaxRowDropPercent = 25,
    # Acknowledge an intended drop: rotate anyway, and make this run the new
    # baseline. Without it the hold is sticky, run after run.
    [switch]$AcceptRowDrop
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $repo "data\backups" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $OutDir "pseudolife_memory-$stamp.sql.gz"

# PostgreSQL writes this as the last line of every plain-format dump.
$dumpMarker = "PostgreSQL database dump complete"

function Read-Dump([string]$Path) {
    # Checked on the HOST artifact, after the copy out of the container, so
    # one test covers three failure modes: the gzip decompresses, pg_dump
    # ran to completion, and `docker cp` did not truncate it. The stream
    # cannot be seeked, so it is read through once, line by line, in
    # constant memory. The same pass counts each table's COPY rows for the
    # manifest: plain-format COPY data holds one row per line (embedded
    # newlines are escaped), ending at a line that is exactly "\.".
    # The marker only counts outside COPY data, where a stored memory that
    # happens to quote it cannot pass for the end of the dump. pg_dump
    # writes it last, so a dump cut off inside COPY data never reaches it.
    $tables = [ordered]@{}
    $complete = $false
    try {
        $fs = [System.IO.File]::OpenRead($Path)
        try {
            $gz = [System.IO.Compression.GZipStream]::new(
                $fs, [System.IO.Compression.CompressionMode]::Decompress)
            try {
                $reader = [System.IO.StreamReader]::new($gz)
                $table = $null
                $rows = 0
                while ($null -ne ($line = $reader.ReadLine())) {
                    if ($null -ne $table) {
                        if ($line -ceq '\.') { $tables[$table] = $rows; $table = $null }
                        else { $rows++ }
                    } elseif ($line -cmatch '^COPY (\S+) .*FROM stdin;$') {
                        $table = $Matches[1]
                        $rows = 0
                    } elseif ($line -ceq "-- $dumpMarker") {
                        $complete = $true
                    }
                }
            } finally { $gz.Dispose() }
        } finally { $fs.Dispose() }
    } catch {
        # A corrupt gzip (the other shape of a killed dump) is not complete.
        Write-Warning "could not read back the dump artifact: $_"
        $complete = $false
    }
    return @{ Complete = $complete; Tables = $tables }
}

Write-Host "Dumping $Db from container $Container -> $out"
# Dump + compress INSIDE the container, then copy the artifact out. This
# avoids piping binary through PowerShell entirely — `Set-Content -Encoding
# Byte` was removed in PowerShell 7, and `>` redirection mangles bytes as
# UTF-16. -Fc would be smaller but plain+gzip is trivially restorable with
# psql.
#
# pg_dump writes the gzip ITSELF (-Z9, plain format) rather than being piped
# into a separate gzip: the container's POSIX sh has no `pipefail`, so a
# pipeline hands `docker exec` the LAST command's status, and a pg_dump that
# died partway still produced a non-empty, perfectly valid gzip of a
# truncated dump — which passed the only other guard here, a zero-length
# check. One process means the status really is pg_dump's, and (unlike
# dumping to a temp file first) it costs no uncompressed scratch space
# inside the container.
$tmp = "/tmp/pl_backup-$stamp.sql.gz"
docker exec $Container sh -c "pg_dump -U $User -d $Db -Z9 > $tmp"
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed inside container $Container" }
# Land on a .part name and promote only once the artifact verifies: a
# rejected dump must never sit in the backup folder looking like the newest
# good backup (restore.ps1 and the rotation below both glob *.sql.gz).
$part = "$out.part"
docker cp "${Container}:$tmp" $part
docker exec $Container rm -f $tmp
if (-not (Test-Path $part) -or (Get-Item $part).Length -eq 0) {
    throw "backup artifact missing or empty: $part"
}
$dump = Read-Dump $part
if (-not $dump.Complete) {
    # Kept, not deleted: a truncated dump is the evidence for whatever went
    # wrong, and the previous good backups sit untouched beside it.
    throw ("backup is INCOMPLETE - the dump is truncated ('$dumpMarker' " +
           "missing). Nothing was promoted; the rejected artifact is at $part")
}
Move-Item -LiteralPath $part -Destination $out -Force

# Row-count gate: compare against the newest manifest that was not itself
# held, in the out-dir OR the mirror. Skipping held runs keeps the hold on
# until -AcceptRowDrop; otherwise the next backup would compare the wiped
# bank with itself and rotation would resume. Searching the mirror covers a
# deploy from a fresh worktree, whose empty out-dir has no history but
# which prunes the same shared mirror.
$gatedTables = "public.entries", "public.facts", "public.lessons"
$manifestOut = Join-Path $OutDir "pseudolife_manifest-$stamp.json"
$searchDirs = @($OutDir)
if ($MirrorDir -and (Test-Path -LiteralPath $MirrorDir)) { $searchDirs += $MirrorDir }
$candidates = @($searchDirs | ForEach-Object {
        Get-ChildItem -LiteralPath $_ -Filter "pseudolife_manifest-*.json" -File
    } | Sort-Object Name -Descending)
$baseline = $null
foreach ($m in $candidates) {
    try { $parsed = Get-Content -LiteralPath $m.FullName -Raw | ConvertFrom-Json }
    catch { Write-Warning "skipping unreadable manifest $($m.FullName): $_"; continue }
    if ($parsed.rotation -ne "held") { $baseline = @{ Name = $m.Name; Data = $parsed }; break }
}
$drops = @()
if ($baseline -and $baseline.Data.tables) {
    foreach ($t in $gatedTables) {
        $prop = $baseline.Data.tables.PSObject.Properties[$t]
        if (-not $prop) { continue }
        $before = [long]$prop.Value
        # A gated table missing from the dump is a drop to zero.
        $after = if ($dump.Tables.Contains($t)) { [long]$dump.Tables[$t] } else { 0 }
        if ($before -gt 0 -and ($before - $after) * 100 -gt $before * $MaxRowDropPercent) {
            $pct = [math]::Floor(($before - $after) * 100 / $before)
            $drops += "$t $before -> $after (-$pct%)"
        }
    }
}
$rotation = if (-not $drops) { "ok" } elseif ($AcceptRowDrop) { "accepted" } else { "held" }
$note = ""
if ($drops) {
    $note = "fell by more than $MaxRowDropPercent% since $($baseline.Name): " + ($drops -join "; ")
}
[ordered]@{
    dump       = Split-Path $out -Leaf
    created_at = (Get-Date).ToUniversalTime().ToString(
        "yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture)
    rotation   = $rotation
    baseline   = if ($baseline) { $baseline.Name } else { $null }
    note       = $note
    tables     = $dump.Tables
} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $manifestOut -Encoding utf8NoBOM
if ($rotation -eq "held") {
    Write-Warning ("ROW-COUNT GATE: $note. Rotation and mirror pruning are HELD: " +
        "nothing older is deleted and the new dump is kept. If the drop is " +
        "intended, re-run with -AcceptRowDrop; until then every backup holds.")
} elseif ($rotation -eq "accepted") {
    Write-Warning ("ROW-COUNT GATE: $note. Accepted (-AcceptRowDrop): rotating, " +
        "and this backup is the new baseline.")
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

# Rotation. Rejected .part artifacts age out on the same window: they are
# kept as evidence (see the completeness check above), but a dump that keeps
# failing must not pile up full-size files forever on a machine that is
# already having a bad day. Listed explicitly because the Windows filter
# `*.sql.gz` does NOT match `*.sql.gz.part` — which is also why a rejected
# artifact cannot masquerade as a backup. A held row-count gate skips it all.
if ($rotation -ne "held") {
    foreach ($pat in "pseudolife_memory-*.sql.gz", "pseudolife_memory-*.sql.gz.part",
                     "pseudolife_state-*.tgz", "pseudolife_manifest-*.json") {
        Get-ChildItem $OutDir -Filter $pat |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
            Remove-Item -Force
    }
}

# Off-disk mirror (opt-in; KeepDays retention, or newest-N per kind with
# -MirrorKeep). Mirrors every artifact: the DB dump, the state tar, and the
# manifest (last, so a manifest never lands without its dump). A held gate
# still copies — it only skips the pruning.
if ($MirrorDir) {
    try {
        New-Item -ItemType Directory -Force -Path $MirrorDir | Out-Null
        $artifacts = @($out)
        if (Test-Path $stateOut) { $artifacts += $stateOut }
        $artifacts += $manifestOut
        foreach ($a in $artifacts) {
            Copy-Item $a $MirrorDir -Force
            $m = Join-Path $MirrorDir (Split-Path $a -Leaf)
            if ((Test-Path $m) -and (Get-Item $m).Length -eq (Get-Item $a).Length) {
                Write-Host "Mirrored to $m"
            } else {
                Write-Warning "mirror copy missing or size mismatch: $m"
            }
        }
        if ($rotation -ne "held") {
            foreach ($pat in "pseudolife_memory-*.sql.gz", "pseudolife_state-*.tgz",
                             "pseudolife_manifest-*.json") {
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
        }
    } catch {
        Write-Warning "backup mirror failed (primary backup is safe): $_"
    }
}

# Record this backup inside the daemon, where /health reports it as
# last_backup: the host's backup folder is invisible to the container, and
# an age nobody can see is how 2026-09-14..20 went six days without a dump
# unnoticed. Warn, never throw — same reasoning as the state tar.
try {
    docker cp $manifestOut "${DaemonContainer}:/data/last-backup.json"
    if ($LASTEXITCODE -ne 0) { throw "docker cp exited $LASTEXITCODE" }
    Write-Host "Recorded in the daemon (/health last_backup)"
} catch {
    Write-Warning ("could not record the backup in the daemon's /data/last-backup.json " +
        "(/health last_backup will read older than it is): $_")
}

if ($rotation -eq "held") {
    Write-Warning "Backup complete, but rotation is HELD by the row-count gate (see above). Nothing was deleted."
} else {
    Write-Host "Backup complete. Retained last $KeepDays days in $OutDir"
}
