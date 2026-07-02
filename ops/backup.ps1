# pg_dump the PseudoLife-MCP database to data\backups\ with 7-day rotation.
#
#   ops\backup.ps1                 # dump into <repo>\data\backups
#   ops\backup.ps1 -KeepDays 30
#
# Runs pg_dump INSIDE the container (no local postgres client needed).
param(
    [string]$Container = "pseudolife-mcp-postgres",
    [string]$Db = "pseudolife_memory",
    [string]$User = "pseudolife",
    [string]$OutDir = "",
    [int]$KeepDays = 7,
    # Off-disk mirror (2026-07-02 review P2): backups on the same physical
    # disk as the bank die with it. Point this (or the env var) at a folder
    # on ANOTHER disk / synced share. Mirror failure warns, never throws —
    # the primary backup already succeeded and deploys must not abort
    # because a mirror drive is unplugged.
    [string]$MirrorDir = $env:PSEUDOLIFE_BACKUP_MIRROR
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

# Rotation.
Get-ChildItem $OutDir -Filter "pseudolife_memory-*.sql.gz" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -Force

# Off-disk mirror (opt-in; same retention).
if ($MirrorDir) {
    try {
        New-Item -ItemType Directory -Force -Path $MirrorDir | Out-Null
        Copy-Item $out $MirrorDir -Force
        $m = Join-Path $MirrorDir (Split-Path $out -Leaf)
        if ((Test-Path $m) -and (Get-Item $m).Length -eq (Get-Item $out).Length) {
            Get-ChildItem $MirrorDir -Filter "pseudolife_memory-*.sql.gz" |
                Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
                Remove-Item -Force
            Write-Host "Mirrored to $m"
        } else {
            Write-Warning "mirror copy missing or size mismatch: $m"
        }
    } catch {
        Write-Warning "backup mirror failed (primary backup is safe): $_"
    }
}

Write-Host "Backup complete. Retained last $KeepDays days in $OutDir"
