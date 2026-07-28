#Requires -Version 7
# Retention for the BuildKit build cache: evict entries older than N hours,
# enforce a size ceiling, then return freed blocks to the WSL disk.
#
#   ops\prune-build-cache.ps1                  # age 168h, ceiling 20GB, trim
#   ops\prune-build-cache.ps1 -DryRun          # report only; mutate nothing
#   ops\prune-build-cache.ps1 -MaxAgeHours 24  # tighter age policy
#
# Build cache is pure derived data: over-pruning costs rebuild time and
# nothing else. This script ONLY ever calls `docker builder prune`. It never
# removes images (ops\prune-rollbacks.ps1 owns rollback-tag retention), never
# touches containers, and never runs `docker system prune` or any volume
# command — the bank lives in the external ops_pseudolife_* volumes.
#
# Scale of the problem (2026-07-28): 51.87GB across 169 entries had
# accumulated, every one of them inactive, some 5-6 weeks old. A single
# deploy accounts for 12.45GB across 17 entries.
#
# Why age is the primary filter and size only a backstop: minutes after a
# deploy, `docker builder du` reports the whole fresh 12.45GB as
# "reclaimable", but most of it is cache shared with the live image and
# can't free until that image is gone — `docker system df`'s RECLAIMABLE
# (the honest, right-now number) was 3.314MB of that same cache. A policy
# keyed on `docker builder du`'s reclaimability deletes hot cache and
# cold-starts the next build.
param(
    [ValidateRange(0, 876000)][int]$MaxAgeHours = 168,
    [ValidateRange(0, 100000)][int]$MaxUsedSpaceGB = 20,
    [switch]$DryRun,
    [switch]$NoTrim
)

$ErrorActionPreference = "Stop"

# docker humanizes with SI (1000-based) units and a lowercase k: "8.192kB".
$script:Units = @{ 'B' = 1; 'KB' = 1e3; 'MB' = 1e6; 'GB' = 1e9; 'TB' = 1e12; 'PB' = 1e15 }

function ConvertFrom-DockerSize {
    param([string]$Text)
    if ($Text -notmatch '^\s*([0-9.]+)\s*([kKMGTP]?B)\s*$') {
        throw "unparseable docker size: '$Text'"
    }
    [long]([double]$Matches[1] * $script:Units[$Matches[2].ToUpperInvariant()])
}

function Format-Bytes {
    param([long]$Bytes)
    if ($Bytes -ge 1e9) {
        return [string]::Format([cultureinfo]::InvariantCulture, "{0:N2}GB", ($Bytes / 1e9))
    }
    if ($Bytes -ge 1e6) {
        return [string]::Format([cultureinfo]::InvariantCulture, "{0:N2}MB", ($Bytes / 1e6))
    }
    return "${Bytes}B"
}

function Get-BuildCacheBytes {
    # `--format` with a Go template rather than JSON, so the bash port can
    # share this exact contract without needing jq.
    $rows = @(docker system df --format '{{.Type}}|{{.Size}}')
    if ($LASTEXITCODE -ne 0) { throw "docker system df failed" }
    foreach ($row in $rows) {
        $type, $size = $row -split '\|', 2
        if ($type -eq 'Build Cache') { return (ConvertFrom-DockerSize $size) }
    }
    throw "docker system df reported no Build Cache row"
}

function Get-StaleEstimateBytes {
    # Dry-run only. BuildKit has no --dry-run, so this sums entries whose
    # CreatedAt precedes the cutoff. It is an ESTIMATE: BuildKit's own
    # until= filter considers last-used, and shared entries may not free
    # fully. Reported as such.
    param([int]$AgeHours)
    $cutoff = (Get-Date).ToUniversalTime().AddHours(-$AgeHours)
    $rows = @(docker builder du --format '{{.CreatedAt}}|{{.Size}}')
    if ($LASTEXITCODE -ne 0) { throw "docker builder du failed" }
    $total = 0L
    foreach ($row in $rows) {
        if (-not $row.Trim()) { continue }
        $created, $size = $row -split '\|', 2
        # "2026-07-28 05:31:09.884 +0000 UTC" — strip the trailing zone name,
        # which .NET will not parse alongside the numeric offset.
        $stamp = ($created -replace '\s+UTC\s*$', '').Trim()
        $when = [datetimeoffset]::Parse($stamp, [cultureinfo]::InvariantCulture)
        if ($when.UtcDateTime -lt $cutoff) { $total += (ConvertFrom-DockerSize $size) }
    }
    return $total
}

$capBytes = [long]($MaxUsedSpaceGB * 1e9)
$before = Get-BuildCacheBytes
Write-Host ("==> Build-cache retention: {0} in cache (age policy {1}h, ceiling {2})." -f `
    (Format-Bytes $before), $MaxAgeHours, (Format-Bytes $capBytes))

if ($DryRun) {
    $stale = Get-StaleEstimateBytes -AgeHours $MaxAgeHours
    Write-Host "==> DRY RUN: nothing below is executed."
    Write-Host "    age pass    : docker builder prune --force --filter until=${MaxAgeHours}h"
    Write-Host ("                  estimated reclaim {0}." -f (Format-Bytes $stale))
    Write-Host "                  (Estimate: sums CreatedAt; BuildKit's until= uses last-used,"
    Write-Host "                   and shared entries may not free fully.)"
    if (($before - $stale) -gt $capBytes) {
        Write-Host "    ceiling pass: docker builder prune --force --max-used-space $capBytes"
    } else {
        Write-Host "    ceiling pass: skipped (post-age size would be within the ceiling)."
    }
    if (-not $NoTrim) {
        Write-Host "    trim        : wsl -d docker-desktop -e sh -c `"fstrim -v /mnt/docker-desktop-disk`""
    }
    Write-Host "==> DRY RUN complete: nothing was changed."
    return
}

# Age pass — the normal policy, always runs.
docker builder prune --force --filter "until=${MaxAgeHours}h" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "docker builder prune (age pass) failed" }

# Ceiling pass — backstop only. A heavy build week can exceed the cap with
# nothing yet old enough for the age pass to touch.
$afterAge = Get-BuildCacheBytes
if ($afterAge -gt $capBytes) {
    Write-Host ("==> Build-cache retention: {0} still over the {1} ceiling; enforcing." -f `
        (Format-Bytes $afterAge), (Format-Bytes $capBytes))
    docker builder prune --force --max-used-space $capBytes | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker builder prune (ceiling pass) failed" }
}

$after = Get-BuildCacheBytes
Write-Host ("==> Build-cache retention: {0} -> {1} (reclaimed {2})." -f `
    (Format-Bytes $before), (Format-Bytes $after), (Format-Bytes ($before - $after)))

# fstrim returns freed blocks to the vhdx free list. It does NOT shrink the
# host file — only an elevated offline compact does (ops\compact-docker-vhdx.ps1).
# Windows/WSL only in effect, and never fatal. Gated on wsl being present
# rather than on $IsWindows: only Windows hosts have wsl on PATH, the sh
# port gates the same way (command -v wsl), and an OS gate is untestable —
# the harness stubs wsl as a function, which Get-Command finds on any
# platform, but $IsWindows cannot be stubbed (CI drives this script under
# pwsh on Linux).
if ($NoTrim) { return }
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    Write-Host "==> Build-cache retention: no wsl on PATH; skipping fstrim."
    return
}
wsl -d docker-desktop -e sh -c true 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Build-cache retention: no docker-desktop WSL distro; skipping fstrim."
    return
}
# `wsl -d <distro> -e <cmd>` fails to resolve fstrim when passed as the bare
# -e target, even though /sbin (where fstrim lives) is on the child's PATH:
# "execvpe(fstrim) failed: No such file or directory". An absolute path
# (/sbin/fstrim) works, and so does `sh -c`, because the shell performs its
# own PATH lookup rather than relying on wsl's relay to do it; verified live
# to reclaim 207.2MiB. The distro probe above uses the same `sh -c` form for
# consistency, since it is the same bare-`-e` resolution that fails here.
wsl -d docker-desktop -e sh -c "fstrim -v /mnt/docker-desktop-disk"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "fstrim failed (retention otherwise succeeded)."
}
