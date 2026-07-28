#Requires -Version 7
# Compact the Docker Desktop data VHDX offline. MANUAL AND ELEVATED ONLY —
# this stops Docker Desktop and shuts down every WSL distro.
#
#   (from an Administrator prompt)
#   ops\compact-docker-vhdx.ps1
#   ops\compact-docker-vhdx.ps1 -Path D:\docker\docker_data.vhdx
#
# Why this is not automated: pruning the build cache (ops\prune-build-cache.ps1)
# frees space INSIDE the VM, and fstrim returns those blocks to the disk's free
# list, but the .vhdx never shrinks on its own — sparse mode was deliberately
# declined as risky. Only an offline `Optimize-VHD -Mode Full` returns the
# space to the host, and that needs elevation plus full Docker downtime, which
# is an operator's decision rather than a scheduled one.
#
# Measured 2026-07-28: prune + fstrim took internal usage 87GB -> 49.3GB while
# the file stayed at 94.74GB; this step then took the file to 47.31GB.
param(
    [string]$Path = (Join-Path $env:LOCALAPPDATA "Docker\wsl\disk\docker_data.vhdx")
)

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw 'Not elevated. Re-run this script from an Administrator PowerShell prompt.'
}

# Optimize-VHD ships with the Hyper-V PowerShell module, which is not present
# on every Windows install (Docker Desktop's WSL2 backend does not require
# Hyper-V to be enabled). Fail here, before anything is stopped, rather than
# with "the term 'Optimize-VHD' is not recognized" after the operator has
# already killed Docker and shut down WSL.
if (-not (Get-Command Optimize-VHD -ErrorAction SilentlyContinue)) {
    throw 'Optimize-VHD not found. Enable the "Hyper-V Module for Windows ' +
        'PowerShell" (Windows Features > Hyper-V > Hyper-V Management Tools) ' +
        'and re-run.'
}

if (-not (Test-Path $Path)) { throw "VHDX not found: $Path" }

$before = [math]::Round((Get-Item $Path).Length / 1GB, 2)
Write-Host "VHDX before: $before GB" -ForegroundColor Cyan

Write-Host 'Stopping Docker Desktop...' -ForegroundColor Cyan
Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue | Stop-Process -Force
foreach ($p in 'com.docker.backend', 'com.docker.build', 'vpnkit') {
    Get-Process $p -ErrorAction SilentlyContinue | Stop-Process -Force
}
Start-Sleep -Seconds 5

Write-Host 'Shutting down WSL (all distros)...' -ForegroundColor Cyan
wsl --shutdown
Start-Sleep -Seconds 10

# Confirm nothing still holds the file: Optimize-VHD otherwise fails with an
# opaque lock error after the operator has already stopped everything.
try {
    $fs = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
    $fs.Close()
} catch {
    throw "VHDX still locked - a WSL/Docker process is running. Wait a few seconds and retry. ($($_.Exception.Message))"
}

Write-Host 'Compacting (this can take several minutes)...' -ForegroundColor Cyan
Optimize-VHD -Path $Path -Mode Full

$after = [math]::Round((Get-Item $Path).Length / 1GB, 2)
Write-Host ''
Write-Host "VHDX after : $after GB" -ForegroundColor Green
Write-Host "Reclaimed  : $([math]::Round($before - $after, 2)) GB" -ForegroundColor Green
Write-Host ''
Write-Host 'Restart Docker Desktop when ready.' -ForegroundColor Yellow
