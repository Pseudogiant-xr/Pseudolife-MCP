#Requires -Version 7
# Prune stale pre-* rollback tags of the daemon image, keeping the newest N.
#
#   ops\prune-rollbacks.ps1              # keep the 2 newest rollback tags
#   ops\prune-rollbacks.ps1 -Keep 5      # keep more
#
# update.ps1 mints one rollback tag per deploy and, before this script, never
# garbage-collected them (~60 stale tags inside a 177GB docker_data.vhdx by
# 2026-07-14). Only ever removes image TAGS of $Repository whose tag suffix
# follows the pre-* rollback naming; never the deployed tag (doesn't match the
# pattern), never an image a running container uses, never volumes.
param(
    [ValidateRange(0, 10000)][int]$Keep = 2,
    [string]$Repository = "pseudolife-daemon"
)

$ErrorActionPreference = "Stop"

# Image IDs in use by running containers are never removed, even when stale.
# Mid-deploy this also protects the just-minted rollback tag: it shares the
# running daemon's image until the rebuild swaps it.
$containers = @(docker ps -q)
if ($LASTEXITCODE -ne 0) { throw "docker ps failed" }
$inUse = @()
if ($containers.Count -gt 0) {
    $inUse = @(docker inspect --format '{{.Image}}' @containers)
    if ($LASTEXITCODE -ne 0) { throw "docker inspect failed" }
}

# Rollback candidates: repository tags whose suffix follows the pre-* naming
# update.ps1 uses ("<version>-pre-update-<stamp>", or "-Tag pre-x" named ones).
$refs = @(docker image ls $Repository --format '{{.Repository}}:{{.Tag}}')
if ($LASTEXITCODE -ne 0) { throw "docker image ls failed" }
$candidates = @(foreach ($ref in ($refs | Where-Object { $_ -match ':.+-pre-' })) {
    $id, $created = (docker image inspect --format '{{.Id}}|{{.Created}}' $ref) -split '\|', 2
    if ($LASTEXITCODE -ne 0) { throw "docker image inspect $ref failed" }
    [pscustomobject]@{
        Ref     = $ref
        Id      = $id
        Created = [datetimeoffset]::Parse($created, [cultureinfo]::InvariantCulture)
    }
})

# Newest first; a timestamp tie (two tags on one image) breaks toward the
# lexicographically later tag, which for pre-update-<stamp> is the newer one.
$stale = @($candidates | Sort-Object Created, Ref -Descending | Select-Object -Skip $Keep)

$removed = 0
foreach ($img in $stale) {
    if ($inUse -contains $img.Id) {
        Write-Host "==> Rollback retention: keeping $($img.Ref) (image in use by a running container)."
        continue
    }
    docker rmi $img.Ref | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Rollback retention: docker rmi $($img.Ref) failed; leaving it."
        continue
    }
    Write-Host "==> Rollback retention: removed stale tag $($img.Ref)."
    $removed++
}
$kept = [Math]::Min($Keep, $candidates.Count)
Write-Host "==> Rollback retention: kept the newest $kept rollback tag(s), removed $removed stale."
