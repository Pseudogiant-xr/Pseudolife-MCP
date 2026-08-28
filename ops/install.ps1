#Requires -Version 7
# ^ Windows PowerShell 5.1 (powershell.exe) writes UTF-8 WITH a BOM, which
#   garbles the first key of ops/.env and can break settings.json parsing —
#   run this under pwsh 7+ (winget install Microsoft.PowerShell).
# One-shot idempotent installer for the Pseudolife-MCP stack (issue #13
# tier 2). Everything downstream of Docker: provider selection -> preflight ->
# extractor choice -> compose up -> client hooks -> standing instructions ->
# MCP registration -> health. Re-running is safe; re-running with a different
# -Extractor is the supported way to switch modes.
#
#   ops\install.ps1                                    # interactive
#   ops\install.ps1 -Extractor sidecar -Client codex   # non-interactive
#   ops\install.ps1 -Extractor sonnet-only -Client claude,gemini
#   ops\install.ps1 -Extractor sonnet-fallback -Instructions append
#
# Providers (-Client, comma- or space-separated list):
#   claude    Claude Code    - MCP + SessionStart briefing + per-turn discipline
#   codex     OpenAI Codex   - MCP + SessionStart briefing (opt-in, not Windows)
#   gemini    Gemini CLI     - MCP + standing instructions (no hook system)
#   generic   any MCP agent  - prints paste-ready config + standing block
#   both = claude,codex      all = claude,codex,gemini
#
# Extractor modes (spec: docs/superpowers/specs/
# 2026-07-14-installer-extractor-choice-design.md):
#   sonnet-only      Claude shim only — the ~11.8 GB sidecar image is never built
#   sonnet-fallback  Claude Sonnet primary via the CLI shim, sidecar fallback
#   sidecar          bundled local CPU extractor only (no Max plan needed)
param(
    [ValidateSet("", "sidecar", "sonnet-fallback", "sonnet-only")]
    [string]$Extractor = "",
    [ValidateSet("", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                 "claude-fable-5")]
    [string]$Model = "",
    # Comma/space-separated provider list (claude|codex|gemini|generic, plus
    # the both/all aliases) — validated by Get-ProviderList, not ValidateSet,
    # which cannot express a list.
    [string]$Client = "",
    [ValidateSet("", "append", "skip")]
    [string]$ClaudeMd = "",
    [ValidateSet("", "append", "skip", "auto")]
    [string]$Instructions = "",
    [string]$AgentsFile = "",
    [int]$ShimPort = 8082,
    [ValidateSet("shim", "http")]
    [string]$Transport = "shim",
    [switch]$NoArt
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

# -- presentation helpers -------------------------------------------------------
# Art and color are interactive sugar only: NO_COLOR unset, no -NoArt, and an
# interactive session. Escapes are generated ([char]27), never raw ESC bytes —
# the tracked-tree control-byte guard bans those.
$Esc = [char]27
function Test-ArtOk {
    $interactive -and -not $env:NO_COLOR -and -not $NoArt -and
        ($env:TERM -ne "dumb")
}
function Step($msg) {
    if (Test-ArtOk) { Write-Host "${Esc}[1;36m==>${Esc}[0m $msg" }
    else { Write-Host "==> $msg" }
}

# >>> banner >>>
function Show-Banner {
    if (-not (Test-ArtOk)) { return }
    Write-Host -NoNewline "${Esc}[36m"
    Write-Host @'
        _.-~~-._                            .---------------------.
      .'        '.                          |  .--.  .--.  .--.   |
     /  .--.  .--. \                        | |    ||    ||    |  |
    |  (    )(    ) |     ==============    | |____||____||____|  |
    |   '--'  '--'  |     ==============    |                     |
    |  .--.  .--.   |     ==============    |    P S E U D O      |
    |  (    )(    ) |     ==============    |      L I F E        |
     \  '--'  '--'  /     ==============    |       M C P         |
      '._        _.'                        |_____________________|
         '-.__.-'                             | |  | |  | |  | |
'@
    Write-Host "${Esc}[0m"
}
# <<< banner <<<

# >>> capability-matrix >>>
function Show-Matrix {
    Write-Host @'
  Agent         MCP          Briefing        Per-turn  Standing file
  ------------  -----------  --------------  --------  ---------------------
  Claude Code   shim / HTTP  hook or plugin  yes       ~/.claude/CLAUDE.md
  OpenAI Codex  shim / HTTP  hook (see *)    no        ~/.codex/AGENTS.md
  Gemini CLI    shim / HTTP  none            no        ~/.gemini/GEMINI.md
  Other agent   stdio/HTTP   none            no        AGENTS.md (your path)

  Every agent also gets, with no files touched: the memory tools, and the
  MCP server `instructions` field - the memory loop delivered by the
  protocol itself.

  * Codex hooks are EXPERIMENTAL and off by default: set
        [features]
        codex_hooks = true
    in ~/.codex/config.toml, then review and trust the hook in /hooks.
    Codex hooks are NOT available on Windows - there the standing AGENTS.md
    block is the briefing, which is why appending it is recommended.
'@
}
# <<< capability-matrix <<<

# >>> generic-snippets >>>
function Show-GenericSnippets {
    Write-Host @'
  Add pseudolife-memory to your agent's MCP config. Two ready-to-paste
  shapes (pick ONE):

  stdio shim (recommended - per-session identity; needs
  `pip install pseudolife-mcp` or pipx):
    { "mcpServers": { "pseudolife-memory": {
        "command": "pseudolife-mcp",
        "env": { "PSEUDOLIFE_WRITER_ID": "mcp-client" } } } }

  HTTP (no local install; concurrent sessions share one identity):
    { "mcpServers": { "pseudolife-memory": {
        "type": "http", "url": "http://127.0.0.1:8765/mcp" } } }

  Common config homes: Cursor ~/.cursor/mcp.json - Windsurf
  ~/.codeium/windsurf/mcp_config.json - Zed settings.json
  (context_servers) - Copilot CLI / others: see the tool's MCP docs.
'@
}
# <<< generic-snippets <<<

# Expand aliases, validate, dedupe, and emit the canonical provider order.
function Get-ProviderList([string]$Spec) {
    $expanded = @()
    foreach ($tok in ($Spec -split '[,\s]+' | Where-Object { $_ })) {
        switch ($tok) {
            "both" { $expanded += @("claude", "codex") }
            "all" { $expanded += @("claude", "codex", "gemini") }
            { $_ -in "claude", "codex", "gemini", "generic" } { $expanded += $_ }
            default {
                Write-Host "invalid -Client '$tok' (claude|codex|gemini|generic|both|all)"
                exit 2
            }
        }
    }
    return @(@("claude", "codex", "gemini", "generic") |
        Where-Object { $expanded -contains $_ })
}

Show-Banner

# -- 1. provider selection (before preflight, so it checks what you picked) ------
if (-not $Client) {
    if (-not $interactive) {
        $Client = "claude"
    } else {
        Write-Host ""
        Write-Host "Which coding agents should this install wire up?"
        Write-Host ""
        Write-Host "  1) Claude Code    full parity: MCP + SessionStart briefing + per-turn discipline"
        Write-Host "  2) OpenAI Codex   MCP + SessionStart briefing (opt-in, trust review, not on Windows)"
        Write-Host "  3) Gemini CLI     MCP + standing instructions (Gemini CLI has no hook system)"
        Write-Host "  4) Other MCP agent  Cursor / Windsurf / Zed / Copilot CLI / anything else:"
        Write-Host "                      prints ready-to-paste config, offers the standing block"
        Write-Host ""
        while (-not $Client) {
            $selection = Read-Host 'Select one or more - e.g. "1 2" or "1,3" (Enter = 1)'
            if (-not $selection) { $selection = "1" }
            $picked = @()
            $bad = $false
            foreach ($tok in ($selection -split '[,\s]+' | Where-Object { $_ })) {
                switch ($tok) {
                    "1" { $picked += "claude" }
                    "2" { $picked += "codex" }
                    "3" { $picked += "gemini" }
                    "4" { $picked += "generic" }
                    default { $bad = $true }
                }
            }
            if ($bad -or -not $picked) {
                Write-Host '  please answer with numbers 1-4 (e.g. "1 3")'
            } else {
                $Client = $picked -join ","
            }
        }
    }
}
$clients = @(Get-ProviderList $Client)
$clientList = $clients -join ","
Step "Providers: $($clients -join ' ')"
if ($interactive) {
    Write-Host ""
    Write-Host "What each agent gets:"
    Write-Host ""
    Show-Matrix
    Write-Host ""
}

# -- 2. preflight --------------------------------------------------------------
Step "Preflight..."
# `&` on a .ps1 only refreshes $LASTEXITCODE when the script exits explicitly;
# clear the stale value a prior native command may have left.
$global:LASTEXITCODE = 0
& (Join-Path $PSScriptRoot "preflight.ps1") -Client $clientList
if ($LASTEXITCODE -ne 0) { throw "Preflight failed - fix the line(s) above and re-run." }

# -- 3. extractor choice (explicit, no default) ---------------------------------
if (-not $Extractor) {
    if (-not $interactive) {
        throw "Non-interactive run: -Extractor sidecar|sonnet-fallback|sonnet-only is required."
    }
    Write-Host ""
    Write-Host "Which dream extractor should consolidate memories?"
    Write-Host "  1) sonnet-only      - lightest: Claude shim only; sidecar never built (~11.8 GB lighter; needs logged-in Max-plan CLI; dreams pause when the shim is down)"
    Write-Host "  2) sonnet-fallback  - Claude shim primary, sidecar auto-fallback (Max-plan CLI plus the ~11.8 GB image)"
    Write-Host "  3) sidecar          - bundled local CPU model (no Claude plan needed, works for everyone; ~11.8 GB image)"
    while (-not $Extractor) {
        switch (Read-Host "Choose 1/2/3") {
            "1" { $Extractor = "sonnet-only" }
            "2" { $Extractor = "sonnet-fallback" }
            "3" { $Extractor = "sidecar" }
            default { Write-Host "  please answer 1, 2 or 3" }
        }
    }
}
Step "Extractor mode: $Extractor"

# -- 3b. dreamer model choice (Claude-shim modes only) ---------------------------
# Opus is the recommended default per the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json). The shim honours per-request
# claude-* names, so this is only the launch default — switchable later from
# the Console's Extractor panel without a reinstall.
if ($Extractor -ne "sidecar" -and -not $Model) {
    if ($interactive) {
        Write-Host ""
        Write-Host "Which Claude model should extract memories (the 'dreamer')?"
        Write-Host "  1) claude-opus-5    - recommended: best measured extraction quality"
        Write-Host "  2) claude-sonnet-5  - balanced"
        Write-Host "  3) claude-haiku-4-5 - fastest / lightest on plan usage"
        Write-Host "  4) claude-fable-5   - most capable tier"
        while (-not $Model) {
            switch (Read-Host "Choose 1/2/3/4 (Enter = 1)") {
                { $_ -in "", "1" } { $Model = "claude-opus-5" }
                "2" { $Model = "claude-sonnet-5" }
                "3" { $Model = "claude-haiku-4-5" }
                "4" { $Model = "claude-fable-5" }
                default { Write-Host "  please answer 1, 2, 3 or 4" }
            }
        }
    } else {
        $Model = "claude-opus-5"
    }
    Step "Dreamer model: $Model"
}

# -- 4. volumes (respect names overridden in an existing ops/.env) --------------
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
Step "Volumes ready: $bankVol, $stateVol"

# -- 5. managed env block --------------------------------------------------------
# Daemon-side writer default: a single first-class provider gets its own id;
# any multi-provider or generic install falls back to the neutral id — the
# per-provider ids then ride each MCP registration's env instead (stage 11).
$writerId = if ($clients.Count -eq 1) {
    switch ($clients[0]) {
        "claude" { "claude-code" }
        "codex" { "codex" }
        "gemini" { "gemini" }
        default { "mcp-client" }
    }
} else { "mcp-client" }
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
$block.Add("PSEUDOLIFE_WRITER_ID=$writerId")
$block.Add($EnvEnd)
Set-Content -Path $envFile -Value (@($kept) + @($block)) -Encoding utf8
Step "Wrote managed block in ops/.env"

# -- 6. sidecar enable/disable via the compose override --------------------------
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
        Step "Sidecar disabled via ops/docker-compose.override.yml"
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
        Step "Removed the running extractor container"
    }
} elseif (InstallerOwnsOverride) {
    Remove-Item $overrideFile
    Step "Removed installer-managed override (sidecar re-enabled)"
}

# -- 7. bring the stack up --------------------------------------------------------
$compose = @("--env-file", $envFile, "-f", $composeFile)
if (Test-Path $overrideFile) { $compose += @("-f", $overrideFile) }
Step "docker compose up -d --build (first build downloads images - grab a coffee)..."
docker compose @compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "compose up failed" }

# -- 8. Claude shim autostart (Sonnet modes) --------------------------------------
if ($Extractor -ne "sidecar") {
    Step "Registering the Claude shim autostart (Task Scheduler; needs an ELEVATED pwsh)..."
    try {
        & (Join-Path $PSScriptRoot "install-shim-autostart.ps1") -Port $ShimPort -Model $Model
    } catch {
        Write-Warning "Shim autostart registration failed (usually elevation): $_"
        Write-Host "  Re-run later from an admin pwsh: ops\install-shim-autostart.ps1 -Port $ShimPort -Model $Model"
        Write-Host "  Or start it manually: python evals\claude_shim.py --port $ShimPort --model $Model --system-prompt-file evals\prompts\sonnet_extractor_v2.md"
    }
}

# -- 9. session lifecycle hooks (hook-capable providers only) ---------------------
# claude: unless the plugin already owns the hooks. codex: hooks exist but are
# experimental, opt-in, and NOT available on Windows — there the standing
# AGENTS.md block is the briefing (stage 10 offers it). gemini/generic: no
# hook system.
$installedPlugins = Join-Path $env:USERPROFILE ".claude\plugins\installed_plugins.json"
$claudePluginInstalled = (Test-Path $installedPlugins) -and
    ((Get-Content $installedPlugins -Raw) -match 'pseudolife-memory@pseudolife-mcp')
if ($claudePluginInstalled -and ($clients -contains "claude")) {
    Step "pseudolife-memory Claude Code plugin detected - skipping Claude"
    Write-Host "    hook and CLAUDE.md block (the plugin provides both). The plugin no"
    Write-Host "    longer bundles an MCP server, so the transport is still wired below."
}

$hookState = @{}
$briefingCommand = "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
foreach ($selectedClient in $clients) {
    if ($selectedClient -notin "claude", "codex") { continue }
    if (($selectedClient -eq "claude") -and $claudePluginInstalled) {
        $hookState["claude"] = "plugin"
        continue
    }
    if (($selectedClient -eq "codex") -and $IsWindows) {
        Step "Skipping the Codex session hook: Codex hooks are not available on"
        Write-Host "    Windows - the standing AGENTS.md block is the briefing there"
        Write-Host "    (stage 10 offers to append it)."
        $hookState["codex"] = "windows"
        continue
    }
    Step "Installing $selectedClient session hook..."
    & (Join-Path $PSScriptRoot "install-hook.ps1") -Client $selectedClient -Command $briefingCommand
    $hookState[$selectedClient] = "hook"
}

# -- 10. standing memory instructions (consent; never edited without it) ----------
# Default is `auto`: skip wherever a session-start briefing already delivers
# the block (claude hook/plugin, codex hook), and offer an interactive append
# where none exists (gemini, generic, codex on Windows). `auto` never writes
# a standing file in a non-interactive run; -Instructions append behaves
# exactly as before. -ClaudeMd remains a compatibility alias.
$instructionChoice = if ($Instructions) { $Instructions } elseif ($ClaudeMd) { $ClaudeMd } else { "auto" }
$instrState = @{}
function Add-MemoryBlock($path, $provider) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    Add-Content -Path $path -Value (Get-Content (Join-Path $repo "examples\CLAUDE.memory.md") -Raw)
    Step "Appended memory block to $path"
    $instrState[$provider] = "appended:$path"
}
foreach ($selectedClient in $clients) {
    if (($selectedClient -eq "claude") -and $claudePluginInstalled) {
        $instrState["claude"] = "covered-by-plugin"
        continue
    }
    $instructionPath = switch ($selectedClient) {
        "codex" { Join-Path $env:USERPROFILE ".codex\AGENTS.md" }
        "gemini" { Join-Path $env:USERPROFILE ".gemini\GEMINI.md" }
        "generic" { $AgentsFile }
        default { Join-Path $env:USERPROFILE ".claude\CLAUDE.md" }
    }
    $hasBlock = $instructionPath -and (Test-Path $instructionPath) -and
        ((Get-Content $instructionPath -Raw) -match 'pseudolife-memory')
    if ($hasBlock) {
        Step "Memory block already present in $instructionPath - skipping."
        $instrState[$selectedClient] = "present:$instructionPath"
        continue
    }
    $choice = $instructionChoice
    if ($choice -eq "auto") {
        switch ($selectedClient) {
            "claude" {
                # The SessionStart hook already delivers the block — a
                # standing-file copy would double-inject.
                $choice = "skip"
            }
            "codex" {
                if ($IsWindows -and $interactive) {
                    $yn = Read-Host "Codex hooks are unavailable on Windows - append the standing memory block to $instructionPath? [Y/n]"
                    $choice = if ($yn -in "n", "N", "no", "NO") { "skip" } else { "append" }
                } else {
                    $choice = "skip"
                }
            }
            "gemini" {
                if ($interactive) {
                    $yn = Read-Host "Gemini CLI has no hook system - append the standing memory block to $instructionPath? [Y/n]"
                    $choice = if ($yn -in "n", "N", "no", "NO") { "skip" } else { "append" }
                } else {
                    $choice = "skip"
                }
            }
            "generic" {
                if ($AgentsFile) {
                    $choice = "append"
                } elseif ($interactive) {
                    $default = Join-Path $env:USERPROFILE "AGENTS.md"
                    $answer = Read-Host "Append the standing memory block to which file? (Enter = $default, `"-`" to skip)"
                    if ($answer -eq "-") {
                        $choice = "skip"
                    } else {
                        $instructionPath = if ($answer) { $answer } else { $default }
                        $choice = "append"
                    }
                } else {
                    $choice = "skip"
                }
            }
        }
    }
    if (($choice -eq "append") -and ($selectedClient -eq "generic") -and -not $instructionPath) {
        Write-Host "NOTE: generic append needs a target - pass -AgentsFile <path>."
        $choice = "skip"
    }
    if ($choice -eq "append") {
        Add-MemoryBlock $instructionPath $selectedClient
    } else {
        $hintPath = if ($instructionPath) { $instructionPath } else { "<your AGENTS.md>" }
        Step "Standing memory block not written for $selectedClient. To add it:"
        Write-Host "  Add-Content `"$hintPath`" (Get-Content `"$repo\examples\CLAUDE.memory.md`" -Raw)"
        $instrState[$selectedClient] = "skipped:$hintPath"
    }
}

# -- 11. wire into selected MCP clients ----------------------------------------------
# Runs even with the plugin installed: the plugin is the hooks/commands layer
# only, so the MCP transport (shim by default) always comes from here.
# The shim install itself is client-agnostic; memoize one attempt so
# multi-provider runs don't run pipx/pip twice.
$script:shimInstallResult = $null
function Install-ShimOnce {
    if ($null -ne $script:shimInstallResult) { return $script:shimInstallResult }
    # NOTE: every native command in here pipes to Out-Host — a PS function
    # returns ALL uncaptured output, so a bare `pipx install` would pollute
    # the boolean return and make failures read as success at the call site
    # (the 2026-07-19 Invoke-WithRetry lesson; $LASTEXITCODE survives the pipe).
    $shimInstalled = $false
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        $pipxList = pipx list 2>$null
        if ($pipxList -match "package pseudolife-mcp ") {
            pipx upgrade pseudolife-mcp 2>&1 | Out-Host
        } else {
            pipx install pseudolife-mcp 2>&1 | Out-Host
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
            & $exe @exeArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { continue }
            & $exe @exeArgs -m pip install --user pseudolife-mcp 2>&1 | Out-Host
            if ($LASTEXITCODE -eq 0) {
                $shimInstalled = $true
                break
            } else {
                Write-Warning "$($candidate.Label) -m pip install --user pseudolife-mcp failed (exit $LASTEXITCODE)."
            }
        }
    }
    $script:shimInstallResult = $shimInstalled
    return $shimInstalled
}

# Per-provider writer ids ride each registration's env (the shim forwards
# PSEUDOLIFE_WRITER_ID as the X-PL-Writer header). CLI env-flag support is
# probed, never assumed: a missing flag degrades to the flagless form plus a
# printed manual config edit — never to a failed install. HTTP transport
# cannot carry env, so there the daemon default (ops/.env) applies.
function Get-EnvFlag($cli) {
    $help = & $cli mcp add --help 2>$null
    if ("$help" -match '--env') { return "--env" }
    return $null
}

$mcpState = @{}
foreach ($selectedClient in $clients) {
    if ($selectedClient -eq "generic") {
        Write-Host ""
        Step "Other MCP-capable agents - paste-ready config:"
        Write-Host ""
        Show-GenericSnippets
        Write-Host ""
        continue
    }
    if ($selectedClient -eq "codex") {
        codex mcp get pseudolife-memory *> $null
        if ($LASTEXITCODE -eq 0) {
            Step "MCP server already wired into Codex - skipping."
            $mcpState["codex"] = "present"
        } elseif (($Transport -eq "shim") -and (Install-ShimOnce)) {
            $envFlag = Get-EnvFlag "codex"
            if ($envFlag) {
                codex mcp add $envFlag PSEUDOLIFE_WRITER_ID=codex pseudolife-memory -- pseudolife-mcp
                $mcpState["codex"] = "shim-env"
            } else {
                codex mcp add pseudolife-memory -- pseudolife-mcp
                Write-Host "  (this codex CLI takes no env flag - for per-provider write attribution"
                Write-Host "   add to the server's entry in ~/.codex/config.toml:"
                Write-Host "     env = { PSEUDOLIFE_WRITER_ID = `"codex`" })"
                $mcpState["codex"] = "shim"
            }
            Step "Wired into Codex via the pseudolife-mcp shim - per-session identity (a Codex session no longer inherits a concurrent Claude session's episode)."
        } else {
            if ($Transport -eq "shim") {
                Write-Warning "Shim unavailable for Codex (see warnings above) - falling back to HTTP."
                Write-Host "  Without the shim, a Codex session running beside a Claude Code session shares its episode identity."
            }
            codex mcp add pseudolife-memory --url http://127.0.0.1:8765/mcp
            Step "Wired into Codex (codex mcp add, HTTP)."
            $mcpState["codex"] = "http"
        }
    } elseif ($selectedClient -eq "gemini") {
        $geminiList = gemini mcp list 2>$null
        if ("$geminiList" -match "pseudolife-memory") {
            Step "MCP server already wired into Gemini CLI - skipping."
            $mcpState["gemini"] = "present"
        } elseif (($Transport -eq "shim") -and (Install-ShimOnce)) {
            $envFlag = Get-EnvFlag "gemini"
            if ($envFlag) {
                gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini pseudolife-memory pseudolife-mcp
                $mcpState["gemini"] = "shim-env"
            } else {
                gemini mcp add -s user pseudolife-memory pseudolife-mcp
                Write-Host "  (this gemini CLI takes no env flag - for per-provider write attribution"
                Write-Host "   add `"env`": {`"PSEUDOLIFE_WRITER_ID`": `"gemini`"} to the server's"
                Write-Host "   entry in ~/.gemini/settings.json)"
                $mcpState["gemini"] = "shim"
            }
            Step "Wired into Gemini CLI via the pseudolife-mcp shim - per-session identity."
        } else {
            if ($Transport -eq "shim") {
                Write-Warning "Shim unavailable for Gemini CLI (see warnings above) - falling back to HTTP."
            }
            gemini mcp add -s user -t http pseudolife-memory http://127.0.0.1:8765/mcp
            Step "Wired into Gemini CLI (gemini mcp add, HTTP)."
            $mcpState["gemini"] = "http"
        }
    } else {
        claude mcp get pseudolife-memory *> $null
        if ($LASTEXITCODE -eq 0) {
            Step "MCP server already wired into Claude Code - skipping."
            $mcpState["claude"] = "present"
        } elseif ($Transport -eq "shim") {
            if (Install-ShimOnce) {
                claude mcp remove pseudolife-memory *> $null
                $envFlag = Get-EnvFlag "claude"
                if ($envFlag) {
                    claude mcp add --scope user $envFlag PSEUDOLIFE_WRITER_ID=claude-code pseudolife-memory -- pseudolife-mcp
                    $mcpState["claude"] = "shim-env"
                } else {
                    claude mcp add --scope user pseudolife-memory -- pseudolife-mcp
                    $mcpState["claude"] = "shim"
                }
                Step "Wired into Claude Code via the pseudolife-mcp shim - per-session identity (required for correct episodes with concurrent sessions)."
            } else {
                Write-Warning "Could not install the pseudolife-mcp shim - no working pipx or Python (>=3.10, py -3 or python) was found, or the shim install itself failed (see warnings above)."
                Write-Host "  Without the shim, concurrent Claude Code sessions share one episode identity."
                Write-Host "  Install pipx or Python >=3.10 and re-run, or pass -Transport http to silence this."
                claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
                Step "Wired into Claude Code via HTTP (fallback - shim tooling not found or shim install failed)."
                $mcpState["claude"] = "http"
            }
        } else {
            claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
            Step "Wired into Claude Code via HTTP (-Transport http)."
            $mcpState["claude"] = "http"
        }
    }
}

# -- 12. health -----------------------------------------------------------------------
Step "Waiting for the daemon to report healthy..."
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
Step "Healthy: http://127.0.0.1:8765/health (Console: http://127.0.0.1:8765/ui/)"

# -- 13. per-provider wiring ladder + per-mode verify hints ---------------------------
# [x] wired · [-] deliberately skipped · [!] unavailable, with remediation.
function Describe-Mcp($state) {
    switch ($state) {
        "shim-env" { "stdio shim (per-provider writer id set)" }
        "shim" { "stdio shim (writer id: daemon default in ops/.env)" }
        "http" { "HTTP (writer id: daemon default in ops/.env)" }
        "present" { "already wired (unchanged)" }
        default { "not wired" }
    }
}
function Describe-Instr($state) {
    if (-not $state) { return "[-] Standing file        skipped" }
    $tail = ($state -split ":", 2)[-1]
    switch -Wildcard ($state) {
        "appended:*" { "[x] Standing file        $tail" }
        "present:*" { "[x] Standing file        $tail" }
        "covered-by-plugin" { "[-] Standing file        plugin briefing covers it" }
        "skipped:*" { "[-] Standing file        skipped - append later to $tail" }
        default { "[-] Standing file        skipped" }
    }
}
Write-Host ""
Step "What got wired, per agent:"
foreach ($selectedClient in $clients) {
    Write-Host ""
    switch ($selectedClient) {
        "claude" {
            Write-Host "  Claude Code"
            Write-Host "    [x] MCP transport        $(Describe-Mcp $mcpState['claude'])"
            Write-Host "    [x] Server instructions  automatic (MCP instructions field)"
            if ($hookState["claude"] -eq "plugin") {
                Write-Host "    [x] Session briefing     Claude Code plugin"
                Write-Host "    [x] Per-turn discipline  Claude Code plugin"
            } else {
                Write-Host "    [x] Session briefing     SessionStart hook -> ~/.claude/settings.json"
                Write-Host "    [x] Per-turn discipline  UserPromptSubmit hook"
            }
            Write-Host "    $(Describe-Instr $instrState['claude'])"
        }
        "codex" {
            Write-Host "  OpenAI Codex"
            Write-Host "    [x] MCP transport        $(Describe-Mcp $mcpState['codex'])"
            Write-Host "    [x] Server instructions  automatic (MCP instructions field)"
            if ($hookState["codex"] -eq "hook") {
                Write-Host "    [x] Session briefing     hook written - enable codex_hooks = true, then trust it in /hooks"
            } elseif ($hookState["codex"] -eq "windows") {
                Write-Host "    [!] Session briefing     unavailable on Windows - the standing AGENTS.md block is the briefing"
            } else {
                Write-Host "    [!] Session briefing     not installed - re-run: ops\install-hook.ps1 -Client codex"
            }
            Write-Host "    [!] Per-turn discipline  unavailable - Codex has no per-prompt hook"
            Write-Host "    $(Describe-Instr $instrState['codex'])"
        }
        "gemini" {
            Write-Host "  Gemini CLI"
            Write-Host "    [x] MCP transport        $(Describe-Mcp $mcpState['gemini'])"
            Write-Host "    [x] Server instructions  automatic (MCP instructions field)"
            Write-Host "    [!] Session briefing     unavailable - Gemini CLI has no hook system"
            Write-Host "    [!] Per-turn discipline  unavailable"
            Write-Host "    $(Describe-Instr $instrState['gemini'])"
        }
        "generic" {
            Write-Host "  Other MCP agent"
            Write-Host "    [-] MCP transport        paste the printed config into your agent"
            Write-Host "    [x] Server instructions  automatic once connected (MCP instructions field)"
            Write-Host "    [!] Session briefing     unavailable - no hook system to wire"
            Write-Host "    [!] Per-turn discipline  unavailable"
            Write-Host "    $(Describe-Instr $instrState['generic'])"
        }
    }
}
Write-Host ""
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
