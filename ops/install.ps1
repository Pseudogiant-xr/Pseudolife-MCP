#Requires -Version 7
# ^ Windows PowerShell 5.1 (powershell.exe) writes UTF-8 WITH a BOM, which
#   garbles the first key of ops/.env and can break settings.json parsing —
#   run this under pwsh 7+ (winget install Microsoft.PowerShell).
# One-shot idempotent installer for the Pseudolife-MCP stack (issue #13
# tier 2). Everything downstream of Docker: preflight -> volumes -> extractor
# choice -> compose up -> client hooks -> standing instructions ->
# MCP registration -> health. Re-running is safe; re-running with a different
# -Extractor is the supported way to switch modes.
#
#   ops\install.ps1                                    # interactive
#   ops\install.ps1 -Extractor sidecar -Client codex   # non-interactive
#   ops\install.ps1 -Extractor sonnet-fallback -ClaudeMd append
#   ops\install.ps1 -Extractor sonnet-only -ClaudeMd skip
#
# Extractor modes (spec: docs/superpowers/specs/
# 2026-07-14-installer-extractor-choice-design.md):
#   sonnet-only      Sonnet only — the 9.4 GB sidecar image is never built
#   sonnet-fallback  Claude Sonnet primary via the CLI shim, sidecar fallback
#   sidecar          bundled local CPU extractor only (no Max plan needed)
param(
    [ValidateSet("", "sidecar", "sonnet-fallback", "sonnet-only")]
    [string]$Extractor = "",
    [ValidateSet("claude", "codex", "both")]
    [string]$Client = "claude",
    [ValidateSet("", "append", "skip")]
    [string]$ClaudeMd = "",
    [ValidateSet("", "append", "skip")]
    [string]$Instructions = "",
    [int]$ShimPort = 8082,
    [ValidateSet("shim", "http")]
    [string]$Transport = "shim"
)
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repo "ops\docker-compose.yml"
$envFile = Join-Path $repo "ops\.env"
$overrideFile = Join-Path $repo "ops\docker-compose.override.yml"
$OverrideMarker = "# pseudolife-mcp install: managed override (sonnet-only) — do not edit; installer rewrites/removes this file"
$EnvBegin = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
$EnvEnd = "# <<< pseudolife-mcp install <<<"
$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

# -- 1. preflight --------------------------------------------------------------
Write-Host "==> Preflight..."
& (Join-Path $PSScriptRoot "preflight.ps1") -Client $Client
if ($LASTEXITCODE -ne 0) { throw "Preflight failed - fix the line(s) above and re-run." }

# -- 2. extractor choice (explicit, no default) ---------------------------------
if (-not $Extractor) {
    if (-not $interactive) {
        throw "Non-interactive run: -Extractor sidecar|sonnet-fallback|sonnet-only is required."
    }
    Write-Host ""
    Write-Host "Which dream extractor should consolidate memories?"
    Write-Host "  1) sonnet-only      - lightest: Sonnet only; sidecar never built (~9 GB lighter; needs logged-in Max-plan CLI; dreams pause when the shim is down)"
    Write-Host "  2) sonnet-fallback  - Claude Sonnet primary, sidecar auto-fallback (Max-plan CLI plus the ~9 GB image)"
    Write-Host "  3) sidecar          - bundled local CPU model (no Claude plan needed, works for everyone; ~9 GB image)"
    while (-not $Extractor) {
        switch (Read-Host "Choose 1/2/3") {
            "1" { $Extractor = "sonnet-only" }
            "2" { $Extractor = "sonnet-fallback" }
            "3" { $Extractor = "sidecar" }
            default { Write-Host "  please answer 1, 2 or 3" }
        }
    }
}
Write-Host "==> Extractor mode: $Extractor"

# -- 3. volumes (respect names overridden in an existing ops/.env) --------------
function Get-EnvValue($name) {
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match "^$name=" } | Select-Object -Last 1
        if ($line) { return $line.Substring($name.Length + 1) }
    }
    return $null
}
$bankVol = (Get-EnvValue "PSEUDOLIFE_BANK_VOLUME"); if (-not $bankVol) { $bankVol = "pseudolife-mcp-bank" }
$stateVol = (Get-EnvValue "PSEUDOLIFE_STATE_VOLUME"); if (-not $stateVol) { $stateVol = "pseudolife-mcp-state" }
docker volume create $bankVol | Out-Null
docker volume create $stateVol | Out-Null
Write-Host "==> Volumes ready: $bankVol, $stateVol"

# -- 4. managed env block --------------------------------------------------------
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $repo "ops\.env.example") $envFile }
$lines = @(Get-Content $envFile)
$kept = New-Object System.Collections.Generic.List[string]
$skip = $false
foreach ($l in $lines) {
    if ($l -eq $EnvBegin) { $skip = $true; continue }
    if ($l -eq $EnvEnd) { $skip = $false; continue }
    if (-not $skip) { $kept.Add($l) }
}
$block = New-Object System.Collections.Generic.List[string]
$block.Add($EnvBegin)
switch ($Extractor) {
    "sidecar" { $block.Add("# extractor: sidecar (stock defaults - nothing to set)") }
    "sonnet-fallback" {
        $block.Add("PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$ShimPort/v1")
        $block.Add("PSEUDOLIFE_DREAM_MODEL=extractor")
        $block.Add("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1")
        $block.Add("PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor")
        $block.Add("PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto")
    }
    "sonnet-only" {
        $block.Add("PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$ShimPort/v1")
        $block.Add("PSEUDOLIFE_DREAM_MODEL=extractor")
        # `primary` (not `auto`): states the single-extractor intent and
        # keeps the auto-without-fallback startup warning silent.
        $block.Add("PSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary")
    }
}
$writerId = switch ($Client) {
    "claude" { "claude-code" }
    "codex" { "codex" }
    default { "mcp-client" }
}
$block.Add("PSEUDOLIFE_WRITER_ID=$writerId")
$block.Add($EnvEnd)
Set-Content -Path $envFile -Value (@($kept) + @($block)) -Encoding utf8
Write-Host "==> Wrote managed block in ops/.env"

# -- 5. sidecar enable/disable via the compose override --------------------------
function InstallerOwnsOverride {
    (Test-Path $overrideFile) -and ((Get-Content $overrideFile -TotalCount 1) -eq $OverrideMarker)
}
if ($Extractor -eq "sonnet-only") {
    if (-not (Test-Path $overrideFile) -or (InstallerOwnsOverride)) {
        @(
            $OverrideMarker
            "# A profiled service is skipped by ``up`` entirely: the extractor image is"
            "# never built or pulled. Re-run ops\install.ps1 with a sidecar mode to remove."
            "services:"
            "  pseudolife-extractor:"
            "    profiles: [`"disabled`"]"
        ) | Set-Content -Path $overrideFile -Encoding utf8
        Write-Host "==> Sidecar disabled via ops/docker-compose.override.yml"
    } else {
        Write-Host "NOTE: ops/docker-compose.override.yml exists and is not installer-managed."
        Write-Host "      Add this to it yourself to disable the sidecar:"
        Write-Host "        services:"
        Write-Host "          pseudolife-extractor:"
        Write-Host "            profiles: [`"disabled`"]"
    }
    # Remove a leftover running extractor container (container only - it has
    # no volumes; the image is kept for an easy switch back).
    $names = docker ps -a --format '{{.Names}}'
    if ($names -contains "pseudolife-mcp-extractor") {
        docker rm -f pseudolife-mcp-extractor | Out-Null
        Write-Host "==> Removed the running extractor container"
    }
} elseif (InstallerOwnsOverride) {
    Remove-Item $overrideFile
    Write-Host "==> Removed installer-managed override (sidecar re-enabled)"
}

# -- 6. bring the stack up --------------------------------------------------------
$compose = @("--env-file", $envFile, "-f", $composeFile)
if (Test-Path $overrideFile) { $compose += @("-f", $overrideFile) }
Write-Host "==> docker compose up -d --build (first build downloads images - grab a coffee)..."
docker compose @compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "compose up failed" }

# -- 7. Sonnet shim autostart (Sonnet modes) --------------------------------------
if ($Extractor -ne "sidecar") {
    Write-Host "==> Registering the Sonnet shim autostart (Task Scheduler; needs an ELEVATED pwsh)..."
    try {
        & (Join-Path $PSScriptRoot "install-shim-autostart.ps1") -Port $ShimPort
    } catch {
        Write-Warning "Shim autostart registration failed (usually elevation): $_"
        Write-Host "  Re-run later from an admin pwsh: ops\install-shim-autostart.ps1 -Port $ShimPort"
        Write-Host "  Or start it manually: python evals\sonnet_shim.py --port $ShimPort --system-prompt-file evals\prompts\sonnet_extractor_v1.md"
    }
}

# -- 8. session lifecycle hooks -----------------------------------------------------
$clients = if ($Client -eq "both") { @("claude", "codex") } else { @($Client) }
$installedPlugins = Join-Path $env:USERPROFILE ".claude\plugins\installed_plugins.json"
$claudePluginInstalled = (Test-Path $installedPlugins) -and
    ((Get-Content $installedPlugins -Raw) -match 'pseudolife-memory@pseudolife-mcp')
if ($claudePluginInstalled -and ($clients -contains "claude")) {
    Write-Host "==> pseudolife-memory Claude Code plugin detected - skipping Claude"
    Write-Host "    hook, CLAUDE.md block, and mcp add (the plugin provides all three)."
}

$briefingCommand = "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
foreach ($selectedClient in $clients) {
    if (($selectedClient -eq "claude") -and $claudePluginInstalled) { continue }
    Write-Host "==> Installing $selectedClient session hook..."
    & (Join-Path $PSScriptRoot "install-hook.ps1") -Client $selectedClient -Command $briefingCommand
}

# -- 9. standing memory instructions (consent; never edited without it) -------------
# -ClaudeMd remains a compatibility alias for existing automation.
$instructionChoice = if ($Instructions) { $Instructions } else { $ClaudeMd }
foreach ($selectedClient in $clients) {
    if (($selectedClient -eq "claude") -and $claudePluginInstalled) { continue }
    $instructionPath = if ($selectedClient -eq "codex") {
        Join-Path $env:USERPROFILE ".codex\AGENTS.md"
    } else {
        Join-Path $env:USERPROFILE ".claude\CLAUDE.md"
    }
    $hasBlock = (Test-Path $instructionPath) -and
        ((Get-Content $instructionPath -Raw) -match 'pseudolife-memory')
    if ($hasBlock) {
        Write-Host "==> Memory block already present in $instructionPath - skipping."
        continue
    }
    $choice = $instructionChoice
    if (-not $choice) {
        if ($interactive) {
            $yn = Read-Host "Append the memory-loop block to $instructionPath? [Y/n]"
            $choice = if ($yn -match '^[Nn]') { "skip" } else { "append" }
        } else {
            $choice = "skip"
        }
    }
    if ($choice -eq "append") {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $instructionPath) | Out-Null
        Add-Content -Path $instructionPath -Value (Get-Content (Join-Path $repo "examples\CLAUDE.memory.md") -Raw)
        Write-Host "==> Appended memory block to $instructionPath"
    } else {
        Write-Host "SKIPPED: MCP server instructions still provide the core memory loop. Optional stronger guidance:"
        Write-Host "  Add-Content `"$instructionPath`" (Get-Content `"$repo\examples\CLAUDE.memory.md`" -Raw)"
    }
}

# -- 10. wire into selected MCP clients ----------------------------------------------
foreach ($selectedClient in $clients) {
    if (($selectedClient -eq "claude") -and $claudePluginInstalled) { continue }
    if ($selectedClient -eq "codex") {
        codex mcp get pseudolife-memory *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "==> MCP server already wired into Codex - skipping."
        } else {
            codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
            Write-Host "==> Wired into Codex (codex mcp add)."
        }
    } else {
        claude mcp get pseudolife-memory *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "==> MCP server already wired into Claude Code - skipping."
        } elseif ($Transport -eq "shim") {
            $shimInstalled = $false
            if (Get-Command pipx -ErrorAction SilentlyContinue) {
                $pipxList = pipx list 2>$null
                if ($pipxList -match "package pseudolife-mcp ") {
                    pipx upgrade pseudolife-mcp
                } else {
                    pipx install pseudolife-mcp
                }
                if ($LASTEXITCODE -eq 0) {
                    $shimInstalled = $true
                } else {
                    Write-Warning "pipx install/upgrade pseudolife-mcp failed (exit $LASTEXITCODE)."
                }
            } else {
                # Probe every candidate interpreter independently - a stale/broken
                # `py` launcher must not block falling through to a viable `python`.
                $interpreterCandidates = @(
                    @{ Label = "py -3"; Cmd = "py"; Args = @("-3") },
                    @{ Label = "python"; Cmd = "python"; Args = @() }
                ) | Where-Object { Get-Command $_.Cmd -ErrorAction SilentlyContinue }
                foreach ($candidate in $interpreterCandidates) {
                    $exe = $candidate.Cmd
                    $exeArgs = $candidate.Args
                    & $exe @exeArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
                    if ($LASTEXITCODE -ne 0) { continue }
                    & $exe @exeArgs -m pip install --user pseudolife-mcp
                    if ($LASTEXITCODE -eq 0) {
                        $shimInstalled = $true
                        break
                    } else {
                        Write-Warning "$($candidate.Label) -m pip install --user pseudolife-mcp failed (exit $LASTEXITCODE)."
                    }
                }
            }
            if ($shimInstalled) {
                claude mcp remove pseudolife-memory *> $null
                claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
                Write-Host "==> Wired into Claude Code via the pseudolife-mcp shim - per-session identity (required for correct episodes with concurrent sessions)."
            } else {
                Write-Warning "Could not install the pseudolife-mcp shim - no working pipx or Python (>=3.10, py -3 or python) was found, or the shim install itself failed (see warnings above)."
                Write-Host "  Without the shim, concurrent Claude Code sessions share one episode identity."
                Write-Host "  Install pipx or Python >=3.10 and re-run, or pass -Transport http to silence this."
                claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
                Write-Host "==> Wired into Claude Code via HTTP (fallback - shim tooling not found or shim install failed)."
            }
        } else {
            claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
            Write-Host "==> Wired into Claude Code via HTTP (-Transport http)."
        }
    }
}
# -- 11. health -----------------------------------------------------------------------
Write-Host "==> Waiting for the daemon to report healthy..."
$h = $null
for ($i = 0; $i -lt 40; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 3
        if ($h.status -eq "ok") { break }
    } catch { Start-Sleep -Milliseconds 1500 }
    $h = $null
}
if (-not $h) {
    Write-Warning "Daemon not healthy yet. Logs: docker logs pseudolife-mcp-daemon"
    exit 1
}
Write-Host "==> Healthy: http://127.0.0.1:8765/health (Console: http://127.0.0.1:8765/ui/)"

# -- 12. per-mode verify hints -----------------------------------------------------------
switch ($Extractor) {
    "sidecar" {
        Write-Host "Verify: memory_dream(action=""status"") - primary_url should point at pseudolife-extractor:8081."
    }
    "sonnet-fallback" {
        Write-Host "Verify: memory_dream(action=""status"") - fallback_url set and primary_healthy: true (shim up)."
    }
    "sonnet-only" {
        Write-Host "Verify: memory_dream(action=""status"") - primary_url on :$ShimPort, extractor_mode: primary."
        Write-Host "Note: dreams pause (and retry next sweep) whenever the shim is down or the CLI is logged out."
    }
}
Write-Host "Done. First session: tell your coding agent to remember something."
