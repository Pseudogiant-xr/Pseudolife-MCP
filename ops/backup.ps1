# pg_dump the PseudoLife-MCP database to data\backups\ with 7-day rotation.
#
#   ops\backup.ps1                 # dump into <repo>\data\backups
#   ops\backup.ps1 -KeepDays 30
#
# Runs pg_dump INSIDE the container (no local postgres client needed).
param(
    [string]$Container = "pseudolife-pg",
    [string]$Db = "pseudolife_memory",
    [string]$User = "pseudolife",
    [string]$OutDir = "",
    [int]$KeepDays = 7
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $repo "data\backups" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $OutDir "pseudolife_memory-$stamp.sql.gz"

Write-Host "Dumping $Db from container $Container -> $out"
# -Fc would be smaller but plain+gzip is trivially restorable with psql.
docker exec $Container pg_dump -U $User -d $Db |
    & { $input | Set-Content -Encoding Byte -NoNewline -Path ($out -replace '\.gz$','') }
# Compress (PowerShell-native gzip via .NET).
$raw = $out -replace '\.gz$',''
$in = [IO.File]::OpenRead($raw)
$fs = [IO.File]::Create($out)
$gz = New-Object IO.Compression.GzipStream($fs, [IO.Compression.CompressionMode]::Compress)
$in.CopyTo($gz); $gz.Close(); $fs.Close(); $in.Close()
Remove-Item $raw

# Rotation.
Get-ChildItem $OutDir -Filter "pseudolife_memory-*.sql.gz" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -Force

Write-Host "Backup complete. Retained last $KeepDays days in $OutDir"
