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
#   ops\install.ps1 -Extractor codex-fallback -Client codex
#
# Providers (-Client, comma- or space-separated list):
#   claude    Claude Code    - MCP + SessionStart briefing + per-turn discipline
#   claude-desktop  Claude Desktop - MCP via claude_desktop_config.json (no hooks)
#   codex     OpenAI Codex   - MCP + hooks selected with -CodexHooks
#   gemini    Gemini CLI     - MCP + standing instructions (no hook system)
#   generic   any MCP agent  - prints paste-ready config + standing block
#   both = claude,codex      all = claude,codex,gemini
#
# -ClaudePlugin auto|skip: install the Claude Code plugin (hooks + commands)
#   when claude is a client (default: auto; an installed plugin is left alone).
# -ClaudeLegacyHooks ask|remove|keep: with the plugin installed, remove the
#   hooks an earlier install wrote to ~/.claude/settings.json (default: ask -
#   prompts; unattended runs keep them).
#
# Extractor modes (spec: docs/superpowers/specs/
# 2026-07-14-installer-extractor-choice-design.md):
#   sonnet-only      Claude shim only — the ~11.8 GB sidecar image is never built
#   sonnet-fallback  Claude Sonnet primary via the CLI shim, sidecar fallback
#   codex-only       Codex (ChatGPT-plan) shim only — sidecar never built
#   codex-fallback   Codex shim primary, sidecar fallback
#   sidecar          bundled local CPU extractor only (no Max plan needed)
param(
    [ValidateSet("", "sidecar", "sonnet-fallback", "sonnet-only",
                 "codex-fallback", "codex-only")]
    [string]$Extractor = "",
    [ValidateSet("", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5",
                 "claude-fable-5", "gpt-5.6-sol", "gpt-5.6-terra",
                 "gpt-5.6-luna")]
    [string]$Model = "",
    # Comma/space-separated provider list (claude|claude-desktop|codex|gemini|
    # generic, plus the both/all aliases) — validated by Get-ProviderList, not
    # ValidateSet, which cannot express a list.
    [string]$Client = "",
    # Explicit ownership avoids duplicating hooks from an installed Codex plugin.
    [ValidateSet("auto", "manual", "plugin", "skip")]
    [string]$CodexHooks = "auto",
    # yes is explicit consent to PseudoLife hook execution and an auto fallback.
    [ValidateSet("ask", "yes", "no")]
    [string]$CodexHookTrust = "ask",
    [ValidateSet("", "append", "skip")]
    [string]$ClaudeMd = "",
    [ValidateSet("", "append", "skip", "auto")]
    [string]$Instructions = "",
    [string]$AgentsFile = "",
    # 0 = auto: 8082 for the Claude shim modes, 8086 for the Codex ones.
    [int]$ShimPort = 0,
    [ValidateSet("shim", "http")]
    [string]$Transport = "shim",
    # Install the Claude Code plugin (hooks + commands) when claude is a client.
    [ValidateSet("auto", "skip")]
    [string]$ClaudePlugin = "auto",
    # With the plugin installed, remove the hooks an earlier install wrote to
    # ~/.claude/settings.json; ask prompts, and unattended runs keep them.
    [ValidateSet("ask", "remove", "keep")]
    [string]$ClaudeLegacyHooks = "ask",
    [switch]$NoArt
)
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repo "ops\docker-compose.yml"
$envFile = Join-Path $repo "ops\.env"
$overrideFile = Join-Path $repo "ops\docker-compose.override.yml"
$OverrideMarker = "# pseudolife-mcp install: managed override (shim-only extractor) — do not edit; installer rewrites/removes this file"
# Pre-codex installs wrote the mode-specific text; keep recognizing it so a
# mode switch still removes/rewrites their override file.
$LegacyOverrideMarker = "# pseudolife-mcp install: managed override (sonnet-only) — do not edit; installer rewrites/removes this file"
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
                        :=-:--:=====             -=:
                   ==: #%##@%-*@@@*#%:                :==
                :*@*:=-#%+**%+*:+#+*%+                   @*:
             :+#*@%#%%=*+++%#%%#+--*%*.                     #+:
            +@%%+#%**#%%#*#@@+***%*+%*                       %@+
         -*@#%*%%@*#*=%@+=**@@*+#@@:%%=                []       @*-
        =%*+#*%#*+@@=+@*:-*@%*+**@%#**@                 |        *%=
        @%-*%##*%:%%--#@#@#%#-*=*%*#%%*          []     |        -%@
      :*#@*#%%@@%=+%*@##%*#===***-+#@*   .--o     |     |    []    #*:
     :%+*****%++*%-#%%%##%#=+=+%*#==@%:           |     |     |     +%:
    =#*%%***+***+*%%%++=-#%@==--@%:-#%@           |     |     |    []*#=
   =%@+%*#%%#@%*#@@%*%%: +%%:-*%%%=+#%@   o--.    |     |     |     | @%=
  .@%###=+#%%@@.+*+*@@%: *@%#+= :+%#%@*           |     |     |     |  %@.
  ##%=%@%%%==%*%+=::=*@#%+=-+#%@@+:+%%%        .---------------.       %##
 .%%*###%@@*==+***%%*..*@@=--.*#%++:#*@ o------|  .---------.  |-----o  %%.
 **%#=:+#@@##*#@*:-#@= *=% :#*.%##:-%@%        |  | # # # # |  |        %**
 %+@%-#%%===**#%*--%@+:*%- .@@=.%#==%%= o------|  | # # # # |  |-----o  @+%
 @%%%#*#%%*#::=#%#*+#%**+=*%@%*@%#-:%@@        |  '---------'  |        %%@
 *%%%*+=:#@%#--%@@%##******=::*@@+:=%@+ o------|  o   o   o  o |-----o  %%*
:@@##==**@@*%*%%#.=%*%%%%*##*######%*@.        '---------------'         @@:
%#%+##%%%%=%=*#%%..:*@%@%%*+*%%%%#%%*%            |     |     |     |    %#%
-%###%***+%#=:#%@#*+*#***%###%*+-:=+%%            |     |     |     |    #%-
 %%%*%=**:*%@@#*=********=**-=%*%+:.%%-           |     |     |     |   %%%
 =%@#%#**#*#@@%#*%%#**###%@%=.=@@%=:%%%   o--[]--.|     |     |    []   @%=
  +%###%#*@%%*+#*#*++*#**#%@##*%%###%@%           |     |     |        #%+
    *%*#*#:#**#****+*@%%%@+:*@%***=*#%.          [] .---|     |      *%*
    =%*=+%%##=**#%+==***@%#-:%%%+=:*%#                  |    []      *%=
     %#***:-**+#%%*%%@%:==-=#@@%#++=@@+                []           *#%
     :*%+*##=@@###+++#%*+#+%@**=%#@-*%@                             %*:
       :#%%%#**#-*#%*+*%**=%%***%#*.+@@                           %#:
         :=###*==+#*#%+**%#*%+=+*+::%@%                         #=:
            +@%==***==****=:+****=:*@*:                      %@+
             .=*%@%*+-==*+*++===++=*%=                      *=.
                   =*@@%%%@@@#+#**%@*                 @*=
                       =====+***+==.              ===

                         P S E U D O L I F E   M C P
'@
    Write-Host "${Esc}[0m"
}
# <<< banner <<<

# >>> capability-matrix >>>
function Show-Matrix {
    Write-Host @'
  Agent           MCP          Briefing        Per-turn  Standing file
  --------------  -----------  --------------  --------  ---------------------
  Claude Code     shim / HTTP  hook or plugin  yes       ~/.claude/CLAUDE.md
  Claude Desktop  shim         none            no        none
  OpenAI Codex    shim / HTTP  hook (see *)    yes       ~/.codex/AGENTS.md
  Gemini CLI      shim / HTTP  none            no        ~/.gemini/GEMINI.md
  Other agent     stdio/HTTP   none            no        AGENTS.md (your path)

  Every agent also gets, with no files touched: the memory tools, and the
  MCP server `instructions` field - the memory loop delivered by the
  protocol itself.

  * Current Codex runtimes enable hooks by default, including Windows.
    Review and trust new or changed hooks in /hooks before they run.
    Support depends on the runtime and policy, not the selected model.
    Use a standing AGENTS.md block if hooks are disabled or unavailable.
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
        "env": { "PSEUDOLIFE_WRITER_ID": "mcp-client",
                 "PSEUDOLIFE_MCP_NO_SPAWN": "1" } } } }

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
            { $_ -in "claude", "claude-desktop", "codex", "gemini", "generic" } { $expanded += $_ }
            default {
                Write-Host "invalid -Client '$tok' (claude|claude-desktop|codex|gemini|generic|both|all)"
                exit 2
            }
        }
    }
    return @(@("claude", "claude-desktop", "codex", "gemini", "generic") |
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
        Write-Host "  2) OpenAI Codex   MCP + session and per-turn hooks (trust review)"
        Write-Host "  3) Gemini CLI     MCP + standing instructions (Gemini CLI has no hook system)"
        Write-Host "  4) Other MCP agent  Cursor / Windsurf / Zed / Copilot CLI / anything else:"
        Write-Host "                      prints ready-to-paste config, offers the standing block"
        Write-Host "  5) Claude Desktop   MCP entry written to claude_desktop_config.json (no hook system)"
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
                    "5" { $picked += "claude-desktop" }
                    default { $bad = $true }
                }
            }
            if ($bad -or -not $picked) {
                Write-Host '  please answer with numbers 1-5 (e.g. "1 3")'
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
        throw "Non-interactive run: -Extractor sidecar|sonnet-fallback|sonnet-only|codex-fallback|codex-only is required."
    }
    Write-Host ""
    Write-Host "Which dream extractor should consolidate memories?"
    Write-Host "  1) sonnet-only      - lightest: Claude shim only; sidecar never built (~11.8 GB lighter; needs logged-in Max-plan CLI; dreams pause when the shim is down)"
    Write-Host "  2) sonnet-fallback  - Claude shim primary, sidecar auto-fallback (Max-plan CLI plus the ~11.8 GB image)"
    Write-Host "  3) sidecar          - bundled local CPU model (no Claude plan needed, works for everyone; ~11.8 GB image)"
    Write-Host "  4) codex-fallback   - Codex (ChatGPT-plan) shim primary, sidecar auto-fallback (ladder-measured at parity with the Claude ceiling - see docs/guide/dreaming.md)"
    Write-Host "  5) codex-only       - Codex shim only; sidecar never built (ladder-measured; dreams pause when the shim is down)"
    while (-not $Extractor) {
        switch (Read-Host "Choose 1/2/3/4/5") {
            "1" { $Extractor = "sonnet-only" }
            "2" { $Extractor = "sonnet-fallback" }
            "3" { $Extractor = "sidecar" }
            "4" { $Extractor = "codex-fallback" }
            "5" { $Extractor = "codex-only" }
            default { Write-Host "  please answer 1-5" }
        }
    }
}
Step "Extractor mode: $Extractor"
$claudeShimMode = $Extractor -in "sonnet-only", "sonnet-fallback"
$codexShimMode = $Extractor -in "codex-only", "codex-fallback"
if ($ShimPort -eq 0) { $ShimPort = $codexShimMode ? 8086 : 8082 }
# A model from the wrong family would silently serve the shim's launch
# default (the per-request override only honours its own prefixes).
if (($claudeShimMode -and $Model -and -not $Model.StartsWith("claude-")) -or
    ($codexShimMode -and $Model -and -not $Model.StartsWith("gpt-"))) {
    throw "-Model $Model does not match extractor mode $Extractor"
}
# Fail fast on a missing shim CLI: preflight only knows -Client, so e.g.
# -Extractor codex-fallback -Client claude would otherwise sail through and
# die at the autostart stage with the stack already up.
if ($claudeShimMode -and -not (Get-Command claude -ErrorAction SilentlyContinue)) {
    throw ("claude CLI not found (needed by extractor mode $Extractor) - " +
           "npm install -g @anthropic-ai/claude-code, then log in")
}
if ($codexShimMode -and
    -not (Get-Command codex -ErrorAction SilentlyContinue) -and
    -not (Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe" -ErrorAction SilentlyContinue)) {
    throw ("codex CLI not found (needed by extractor mode $Extractor) - " +
           "install Codex and run ``codex login``: https://developers.openai.com/codex/cli/")
}

# -- 3b. dreamer model choice (Claude-shim modes only) ---------------------------
# Opus is the recommended default per the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json). The shim honours per-request
# claude-* names, so this is only the launch default — switchable later from
# the Console's Extractor panel without a reinstall.
if ($claudeShimMode -and -not $Model) {
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
# GPT-5.6 menu: no 'recommended' — extraction quality is unmeasured for all
# three (the ladder's terra rung exists to measure it); Terra is only the
# shim's balanced default.
if ($codexShimMode -and -not $Model) {
    if ($interactive) {
        Write-Host ""
        Write-Host "Which GPT-5.6 model should extract memories (the 'dreamer')?"
        Write-Host "  1) gpt-5.6-terra - balanced default (extraction quality unmeasured)"
        Write-Host "  2) gpt-5.6-sol   - flagship (unmeasured)"
        Write-Host "  3) gpt-5.6-luna  - fastest / lightest on plan usage (unmeasured)"
        while (-not $Model) {
            switch (Read-Host "Choose 1/2/3 (Enter = 1)") {
                { $_ -in "", "1" } { $Model = "gpt-5.6-terra" }
                "2" { $Model = "gpt-5.6-sol" }
                "3" { $Model = "gpt-5.6-luna" }
                default { Write-Host "  please answer 1, 2 or 3" }
            }
        }
    } else {
        $Model = "gpt-5.6-terra"
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
        "claude-desktop" { "claude-desktop" }
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
    { $_ -in "sonnet-fallback", "codex-fallback" } {
        $block.Add("PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$ShimPort/v1")
        $block.Add("PSEUDOLIFE_DREAM_MODEL=extractor")
        $block.Add("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1")
        $block.Add("PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor")
        $block.Add("PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto")
    }
    { $_ -in "sonnet-only", "codex-only" } {
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
    (Test-Path $overrideFile) -and
        ((Get-Content $overrideFile -TotalCount 1) -in $OverrideMarker, $LegacyOverrideMarker)
}
if ($Extractor -in "sonnet-only", "codex-only") {
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

# -- 8. CLI shim autostart (Claude / Codex modes) ---------------------------------
# A mode switch must tear down the OTHER family's autostart: an abandoned
# shim task keeps making real CLI calls at every /health refresh, forever,
# on a plan whose owner believes it is turned off. Best-effort like the
# registration below (unelevated removal fails; warn with the manual step).
function Remove-ShimTask($name) {
    if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) { return }
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Write-Warning "could not remove autostart task '$name' (needs an ELEVATED pwsh opened from the Start menu, not from inside Claude Desktop) - its shim keeps starting at logon until you remove it"
    } else {
        Step "Removed autostart task '$name' (a running shim process, if any, persists until logoff)"
    }
}
if (-not $codexShimMode) { Remove-ShimTask "Pseudolife Codex Shim" }
if (-not $claudeShimMode) {
    Remove-ShimTask "Pseudolife Claude Shim"
    Remove-ShimTask "Pseudolife Sonnet Shim"   # pre-rename installs
}
if ($claudeShimMode) {
    Step "Registering the Claude shim autostart (Task Scheduler; needs an ELEVATED pwsh opened from the Start menu - not from a shell inside Claude Desktop)..."
    try {
        & (Join-Path $PSScriptRoot "install-shim-autostart.ps1") -Port $ShimPort -Model $Model
    } catch {
        Write-Warning "Shim autostart registration or start failed (registration: usually elevation): $_"
        Write-Host "  Re-run later from an admin pwsh opened fresh from the Start menu (never from a shell inside Claude Desktop - see the note in ops\install-shim-autostart.ps1):"
        Write-Host "    ops\install-shim-autostart.ps1 -Port $ShimPort -Model $Model"
        Write-Host "  Or start it manually: python evals\claude_shim.py --port $ShimPort --model $Model --system-prompt-file evals\prompts\sonnet_extractor_v5.md"
    }
} elseif ($codexShimMode) {
    Step "Registering the Codex shim autostart (Task Scheduler; needs an ELEVATED pwsh opened from the Start menu - not from a shell inside Claude Desktop)..."
    try {
        & (Join-Path $PSScriptRoot "install-codex-shim-autostart.ps1") -Port $ShimPort -Model $Model
    } catch {
        Write-Warning "Shim autostart registration failed (usually elevation): $_"
        Write-Host "  Re-run later from an admin pwsh opened fresh from the Start menu (never from a shell inside Claude Desktop - see the note in ops\install-codex-shim-autostart.ps1):"
        Write-Host "    ops\install-codex-shim-autostart.ps1 -Port $ShimPort -Model $Model"
        Write-Host "  Or start it manually: python evals\codex_shim.py --port $ShimPort --model $Model"
    }
}

# -- 8b. Claude Code plugin (hooks + commands layer) ------------------------------
# >>> claude plugin >>>
# The plugin is the third install beside the daemon and the shim, and the
# only one that needed two commands typed inside Claude Code. Installing it
# here gives a fresh machine the session briefing and per-turn hooks from
# the plugin (section 9 then skips the settings.json hooks). Idempotent: an
# installed plugin is left alone - its cache moves through /plugin update,
# and the daemon's session briefing says when it is behind.
$claudePluginId = "pseudolife-memory@pseudolife-mcp"
$claudePluginMarketplace = "pseudolife-mcp"
$claudePluginMarketplaceSource = "Pseudogiant-xr/Pseudolife-MCP"
$script:pluginClaude = ""
$script:pluginClaudeRecovery = ""
function Get-ClaudePluginManual {
    "inside Claude Code run /plugin marketplace add $claudePluginMarketplaceSource then /plugin install $claudePluginId"
}
function Test-ClaudePluginRecorded {
    $file = Join-Path $env:USERPROFILE ".claude\plugins\installed_plugins.json"
    return (Test-Path $file) -and ((Get-Content $file -Raw) -match [regex]::Escape("`"$claudePluginId`""))
}
function Get-ClaudePluginInstalledVersion {
    $file = Join-Path $env:USERPROFILE ".claude\plugins\installed_plugins.json"
    try {
        $records = @((Get-Content $file -Raw | ConvertFrom-Json).plugins.$claudePluginId)
        if ($records.Count -gt 0) { return [string]$records[0].version }
    } catch {}
    return ""
}
function Install-ClaudePlugin {
    if ($clients -notcontains "claude") { return }
    # An installed plugin is reported as such even under skip: the ladder
    # line must agree with the hook-ownership lines section 9 derives from
    # the same record.
    if (Test-ClaudePluginRecorded) {
        $script:pluginClaude = "present:$(Get-ClaudePluginInstalledVersion)"
        return
    }
    if ($ClaudePlugin -eq "skip") { $script:pluginClaude = "skipped"; return }
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
        $script:pluginClaude = "no-cli"
        $script:pluginClaudeRecovery = "the claude CLI is not on PATH; $(Get-ClaudePluginManual)"
        return
    }
    $marketplaces = Join-Path $env:USERPROFILE ".claude\plugins\known_marketplaces.json"
    $marketplaceKnown = (Test-Path $marketplaces) -and
        ((Get-Content $marketplaces -Raw) -match [regex]::Escape("`"$claudePluginMarketplace`""))
    if (-not $marketplaceKnown) {
        Step "Adding the $claudePluginMarketplace plugin marketplace to Claude Code..."
        claude plugin marketplace add $claudePluginMarketplaceSource
        if ($LASTEXITCODE -ne 0) {
            $script:pluginClaude = "failed"
            $script:pluginClaudeRecovery = "'claude plugin marketplace add $claudePluginMarketplaceSource' failed (GitHub reachable?); retry it, or $(Get-ClaudePluginManual)"
            Write-Warning $script:pluginClaudeRecovery
            return
        }
    }
    Step "Installing the $claudePluginId plugin into Claude Code..."
    # --yes skips a confirmation newer CLIs require without a TTY; older
    # ones lack the flag, so probe rather than assume.
    $yesFlag = @()
    # Some CLIs print help on stderr; read both streams.
    $installHelp = [string](claude plugin install --help 2>&1)
    if ($installHelp -match '--yes') { $yesFlag = @('--yes') }
    claude plugin install @yesFlag $claudePluginId
    if ($LASTEXITCODE -ne 0) {
        $script:pluginClaude = "failed"
        $script:pluginClaudeRecovery = "'claude plugin install $claudePluginId' failed (see above); retry it, or $(Get-ClaudePluginManual)"
        Write-Warning $script:pluginClaudeRecovery
        return
    }
    if (Test-ClaudePluginRecorded) {
        $script:pluginClaude = "installed:$(Get-ClaudePluginInstalledVersion)"
    } else {
        $script:pluginClaude = "failed"
        $script:pluginClaudeRecovery = "'claude plugin install' returned success but ~/.claude/plugins/installed_plugins.json does not list $claudePluginId; check 'claude plugin list', or $(Get-ClaudePluginManual)"
        Write-Warning $script:pluginClaudeRecovery
    }
}
function Describe-Plugin($state) {
    $tail = ($state -split ":", 2)[-1]
    switch -Wildcard ($state) {
        "installed:*" { "[x] Plugin               installed (v$tail) - restart Claude Code or /reload-plugins to load it" }
        "present:*" { "[x] Plugin               already installed (v$tail)" }
        "skipped" { "[-] Plugin               skipped (-ClaudePlugin skip) - hooks come from settings.json instead" }
        "no-cli" { "[!] Plugin               not installed - $($script:pluginClaudeRecovery)" }
        "failed" { "[!] Plugin               not installed - $($script:pluginClaudeRecovery)" }
        default { "[-] Plugin               not installed" }
    }
}
# <<< claude plugin <<<
Install-ClaudePlugin

# >>> claude legacy hooks >>>
# Installs from before the plugin wrote the briefing, coordination and
# discipline hooks (and, before 2026-07-14, episode hooks) into
# ~/.claude/settings.json. The plugin provides them now, so an upgraded user
# runs each one twice. install-hook removes only the exact entries the
# installers wrote, only while the plugin runs for every project, after a
# backup. Section 9 calls this for a plugin-owned Claude; it asks first
# unless -ClaudeLegacyHooks says otherwise.
$script:legacyClaude = ""
$script:legacyClaudeBackup = ""
function Read-ClaudeLegacyAnswer {
    # The reply, or $null when there is no terminal to ask.
    if (-not $interactive) { return $null }
    return (Read-Host "Remove them from ~/.claude/settings.json (a timestamped backup is taken first)? [y/N]")
}
function Invoke-LegacyHookScript([bool]$dryRun) {
    $hookArgs = @{ Client = "claude"; RemoveLegacy = $true; DryRun = $dryRun }
    $global:LASTEXITCODE = 0
    try {
        $lines = & (Join-Path $repo "ops\install-hook.ps1") @hookArgs *>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } catch {
        $lines = @("$_")
        $code = 1
    }
    return [pscustomobject]@{ Code = $code; Report = (@($lines) -join "`n") }
}
function Invoke-ClaudeLegacyHookCleanup {
    $scan = Invoke-LegacyHookScript $true
    switch ($scan.Code) {
        0 { }
        3 {
            # Nothing to remove; a lookalike the user should review still shows.
            if ($scan.Report -match 'by hand') { Write-Host $scan.Report }
            return
        }
        4 { $script:legacyClaude = "inactive"; Write-Host $scan.Report; return }
        default { $script:legacyClaude = "error"; Write-Warning $scan.Report; return }
    }
    Step "Found hooks an earlier install wrote to ~/.claude/settings.json; the plugin provides them now:"
    Write-Host $scan.Report
    $choice = $ClaudeLegacyHooks
    if ($choice -eq "ask") {
        $reply = Read-ClaudeLegacyAnswer
        if ($null -eq $reply) {
            $choice = "keep"
            Write-Host "    No terminal to ask: left them in place. Rerun with -ClaudeLegacyHooks remove to remove them."
        } elseif ($reply -in "y", "yes") {
            $choice = "remove"
        } else {
            $choice = "keep"
        }
    }
    if ($choice -ne "remove") { $script:legacyClaude = "kept"; return }
    $result = Invoke-LegacyHookScript $false
    Write-Host $result.Report
    if ($result.Code -eq 0) {
        $script:legacyClaude = "removed"
        $backupLine = @($result.Report -split "`n" | Where-Object { $_ -like "Backed up -> *" })[0]
        $script:legacyClaudeBackup = "$backupLine" -replace '^Backed up -> ', ''
    } else {
        $script:legacyClaude = "error"
    }
}
function Describe-LegacyHooks($state) {
    switch ($state) {
        "removed" { "[x] Old hooks            removed from ~/.claude/settings.json (backup: $($script:legacyClaudeBackup))" }
        "kept" { "[!] Old hooks            still in ~/.claude/settings.json, so they run twice - rerun with -ClaudeLegacyHooks remove" }
        "inactive" { "[-] Old hooks            kept in ~/.claude/settings.json - the plugin is not enabled for all projects" }
        "error" { "[!] Old hooks            not removed (see the error above) - remove them by hand (plugin/README.md, Migrating from installer hook wiring)" }
    }
}
# <<< claude legacy hooks <<<

# -- 9. session lifecycle hooks (hook-capable providers only) ---------------------
# Claude skips hooks owned by its plugin. Codex resolves ownership, consent,
# exact hook trust, runtime verification, and instruction fallback together.
# Gemini/generic have no hook system.
$installedPlugins = Join-Path $env:USERPROFILE ".claude\plugins\installed_plugins.json"
$claudePluginInstalled = (Test-Path $installedPlugins) -and
    ((Get-Content $installedPlugins -Raw) -match 'pseudolife-memory@pseudolife-mcp')
$instructionChoice = if ($Instructions) { $Instructions } elseif ($ClaudeMd) { $ClaudeMd } else { "auto" }
if ($claudePluginInstalled -and ($clients -contains "claude")) {
    Step "pseudolife-memory Claude Code plugin detected - skipping Claude"
    if ($instructionChoice -eq "append") {
        Write-Host "    hook (the plugin provides it, serving a compact memory core); the full"
        Write-Host "    CLAUDE.md block is still appended, as requested. The plugin no longer"
        Write-Host "    bundles an MCP server, so the transport is still wired below."
    } else {
        Write-Host "    hook and CLAUDE.md block (the plugin provides the hook, which serves a"
        Write-Host "    compact memory core; the full block stays optional). The plugin no"
        Write-Host "    longer bundles an MCP server, so the transport is still wired below."
    }
}

$hookState = @{}
$codexSetup = $null
$codexSetupValid = $false
$codexCredentialFile = $null
$codexCredentialUrl = $null
$codexConnectionConfigured = $false
$codexCredentialBootstrapFailed = $false
$codexRuntimeDefaults = $null
$codexRuntimeRecovery = $null
$briefingCommand = "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
foreach ($selectedClient in $clients) {
    if ($selectedClient -notin "claude", "codex") { continue }
    if (($selectedClient -eq "claude") -and $claudePluginInstalled) {
        $hookState["claude"] = "plugin"
        Invoke-ClaudeLegacyHookCleanup
        continue
    }
    if ($selectedClient -eq "codex") {
        $codexSetup = [pscustomobject]@{
            source = "skip"; status = "unavailable"; instructions = "skipped"
            recovery = "Install Python 3.10 or newer, then rerun this installer to finish Codex memory setup."
        }
        # Probe real interpreters; Windows Store aliases and old Python versions
        # must not prevent trying the next candidate. Do not alter the user's PATH.
        $codexPython = $null
        foreach ($candidate in @("python", "python3", "py")) {
            if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
            $probeArgs = if ($candidate -eq "py") { @("-3") } else { @() }
            try {
                $probe = & $candidate @probeArgs -c 'import sys; sys.exit(1) if sys.version_info < (3, 10) else print(sys.executable)' 2>$null
                if (($LASTEXITCODE -eq 0) -and $probe) { $codexPython = "$probe".Trim(); break }
            } catch { continue }
        }
        if ($codexPython) {
            $savedCredentialToken = [Environment]::GetEnvironmentVariable(
                "PSEUDOLIFE_MCP_TOKEN", "Process")
            $savedCredentialFile = [Environment]::GetEnvironmentVariable(
                "PSEUDOLIFE_MCP_TOKEN_FILE", "Process")
            $savedCredentialUrl = [Environment]::GetEnvironmentVariable(
                "PSEUDOLIFE_MCP_DAEMON_URL", "Process")
            $installerToken = Get-EnvValue "PSEUDOLIFE_MCP_TOKEN"
            $installerUrl = Get-EnvValue "PSEUDOLIFE_MCP_DAEMON_URL"
            if (-not $installerUrl) { $installerUrl = "http://127.0.0.1:8765" }
            try {
                $credentialArgs = @((Join-Path $repo "ops/setup-codex-coordination.py"),
                    "--credentials")
                if ($installerToken) {
                    $credentialArgs += "--installer-token-stdin"
                }
                $credentialArgs += @("--installer-daemon-url", $installerUrl)
                $credentialOutput = if ($installerToken) {
                    $installerToken | & $codexPython @credentialArgs
                } else {
                    & $codexPython @credentialArgs
                }
                $credentialExit = $LASTEXITCODE
                $credentialResult = ($credentialOutput -join "`n") | ConvertFrom-Json
                if (($credentialExit -eq 0) -and
                    ($credentialResult.status -in "ready", "tokenless") -and
                    ($credentialResult.credential_file_configured -in $true, $false) -and
                    ($credentialResult.connection_configured -in $true, $false) -and
                    (-not $credentialResult.connection_configured -or
                     $credentialResult.daemon_url -is [string]) -and
                    (-not $credentialResult.credential_file_configured -or
                     $credentialResult.credential_file_path -is [string])) {
                    $codexConnectionConfigured = [bool]$credentialResult.connection_configured
                    if ($codexConnectionConfigured) {
                        $codexCredentialUrl = [string]$credentialResult.daemon_url
                        $env:PSEUDOLIFE_MCP_DAEMON_URL = $codexCredentialUrl
                        $env:PSEUDOLIFE_MCP_TOKEN = $null
                        $env:PSEUDOLIFE_MCP_TOKEN_FILE = $null
                    }
                    if ($credentialResult.credential_file_configured) {
                        $codexCredentialFile = [string]$credentialResult.credential_file_path
                        $env:PSEUDOLIFE_MCP_TOKEN_FILE = $codexCredentialFile
                        Step "Codex credential file ready; future rotations are picked up by new requests."
                    }
                } else {
                    throw "Invalid credential setup result"
                }
            } catch {
                $codexCredentialBootstrapFailed = $true
                $codexSetup.recovery = "Codex credential setup failed. Repair the configured token file or Codex configuration, then rerun the installer."
            }
            if (-not $codexCredentialBootstrapFailed) {
                $setupArgs = @((Join-Path $repo "ops/setup-codex-hooks.py"), "--source", $CodexHooks,
                    "--trust", $CodexHookTrust, "--instructions", $instructionChoice)
                if (-not $interactive) { $setupArgs += "--non-interactive" }
                $setupLaunchFailed = $false
                try {
                    $setupOutput = & $codexPython @setupArgs
                    $setupExit = $LASTEXITCODE
                } catch {
                    $setupLaunchFailed = $true
                } finally {
                    [Environment]::SetEnvironmentVariable(
                        "PSEUDOLIFE_MCP_TOKEN", $savedCredentialToken, "Process")
                    [Environment]::SetEnvironmentVariable(
                        "PSEUDOLIFE_MCP_TOKEN_FILE", $savedCredentialFile, "Process")
                    [Environment]::SetEnvironmentVariable(
                        "PSEUDOLIFE_MCP_DAEMON_URL", $savedCredentialUrl, "Process")
                }
                try {
                    if ($setupLaunchFailed) { throw "Codex setup launch failed" }
                    $result = ($setupOutput -join "`n") | ConvertFrom-Json
                    if (($setupExit -notin 0, 1) -or
                        ($result.status -notin "ready", "pending", "unavailable", "skipped") -or
                        ($result.source -notin "manual", "plugin", "skip") -or
                        ($result.instructions -notin "present", "appended", "skipped", "covered-by-hooks")) {
                        throw "Invalid Codex setup result"
                    }
                    $codexSetup = $result
                    $codexSetupValid = $true
                } catch {
                    $codexSetup.recovery = "Codex setup did not return a valid result. Run python ops/setup-codex-hooks.py to retry."
                }
            }
        }
        $hookState["codex"] = $codexSetup.status
        Step "Codex hooks: $($codexSetup.status); standing instructions: $($codexSetup.instructions)."
        if ($codexSetup.recovery) { Write-Host "    $($codexSetup.recovery)" }
        continue
    }
    Step "Installing $selectedClient session hook..."
    & (Join-Path $PSScriptRoot "install-hook.ps1") -Client $selectedClient -Command $briefingCommand
    $hookState[$selectedClient] = "hook"
}

# -- 10. standing memory instructions (consent; never edited without it) ----------
# Codex instructions are resolved by the setup helper after hook verification.
# Other clients retain the existing auto/append/skip behavior and prompts.
$instrState = @{}
function Add-MemoryBlock($path, $provider) {
    # Presence check HERE, not only at the loop top: the generic prompt
    # resolves its target path after that check ran against an empty
    # -AgentsFile, and a re-run must never double-append.
    if ((Test-Path $path) -and ((Get-Content $path -Raw) -match 'pseudolife-memory')) {
        Step "Memory block already present in $path - skipping."
        $instrState[$provider] = "present:$path"
        return
    }
    # A bare filename has no parent — Split-Path returns "", and New-Item ""
    # is a terminating binder error under EAP=Stop.
    $parent = Split-Path -Parent $path
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Add-Content -Path $path -Value (Get-Content (Join-Path $repo "examples\CLAUDE.memory.md") -Raw)
    Step "Appended memory block to $path"
    $instrState[$provider] = "appended:$path"
}
foreach ($selectedClient in $clients) {
    if ($selectedClient -eq "codex") {
        # Explicit consent also covers instruction-only setup when Python or
        # the helper is unavailable. A valid helper result already owns this.
        if (-not $codexSetupValid -and ($instructionChoice -eq "append" -or
            ($instructionChoice -eq "auto" -and $CodexHookTrust -eq "yes"))) {
            try {
                $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
                $override = Join-Path $codexHome "AGENTS.override.md"
                $fallbackPath = if ((Test-Path $override) -and ([string](Get-Content $override -Raw)).Trim()) {
                    $override
                } else { Join-Path $codexHome "AGENTS.md" }
                $existing = if (Test-Path $fallbackPath) { Get-Content $fallbackPath -Raw } else { "" }
                if ($existing -cmatch '(?m)^## Memory\b' -and $existing -match 'pseudolife-memory' -and
                    $existing -cmatch 'RECALL' -and $existing -cmatch 'CAPTURE' -and $existing -cmatch 'REFLECT') {
                    $codexSetup.instructions = "present"
                } else {
                    $block = Get-Content (Join-Path $repo "examples/CLAUDE.memory.md") -Raw
                    New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
                    if (Test-Path $fallbackPath) {
                        $saved = "$fallbackPath.bak-pseudolife-$([guid]::NewGuid().ToString('N'))"
                        Copy-Item -LiteralPath $fallbackPath -Destination $saved
                        Step "Backed up Codex standing instructions to $saved"
                    }
                    Add-Content -LiteralPath $fallbackPath -Value ("`n`n" + $block) -Encoding utf8
                    $codexSetup.instructions = "appended"
                }
                Step "Codex standing instructions: $($codexSetup.instructions) ($fallbackPath). Hooks still require setup."
            } catch {
                $codexSetup.recovery += " Could not write standing instructions; check the Codex home and file permissions."
                Step $codexSetup.recovery
            }
        }
        $instrState["codex"] = $codexSetup.instructions
        continue
    }
    if ($selectedClient -eq "claude-desktop") {
        # Desktop reads no standing file; the MCP instructions field is its
        # only briefing channel.
        continue
    }
    if (($selectedClient -eq "claude") -and $claudePluginInstalled) {
        # The plugin's SessionStart hook serves a compact memory core, not
        # this block: auto and skip leave CLAUDE.md alone (the summary says
        # so), and an explicit append still writes it.
        if ($instructionChoice -ne "append") {
            $instrState["claude"] = "covered-by-plugin"
            continue
        }
    }
    $instructionPath = switch ($selectedClient) {
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
                # Skipped by default. The settings.json SessionStart hook
                # serves the compact memory core and the live briefing, not
                # this block; the summary names the file to append it to.
                $choice = "skip"
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
$script:shimInstallPath = $null
function Resolve-InstalledShimPath($Manager = $null) {
    $shimBinDir = $null
    if (-not $Manager) {
        if (Get-Command pipx -ErrorAction SilentlyContinue) {
            $Manager = @{ Cmd = "pipx"; Args = @() }
        } else {
            foreach ($candidate in @(
                @{ Cmd = "py"; Args = @("-3") },
                @{ Cmd = "python"; Args = @() }
            )) {
                if (-not (Get-Command $candidate.Cmd -ErrorAction SilentlyContinue)) { continue }
                $candidateCmd = $candidate.Cmd
                $candidateArgs = $candidate.Args
                & $candidateCmd @candidateArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) { $Manager = $candidate; break }
            }
        }
    }
    if (-not $Manager) { return $null }
    if ($Manager.Cmd -eq "pipx") {
        $shimBinDir = (& pipx environment --value PIPX_BIN_DIR 2>$null | Select-Object -Last 1)
    } else {
        $managerCmd = $Manager.Cmd
        $managerArgs = $Manager.Args
        $shimBinDir = (& $managerCmd @managerArgs -c "import sysconfig; print(sysconfig.get_path('scripts', scheme=sysconfig.get_preferred_scheme('user')))" 2>$null | Select-Object -Last 1)
    }
    if (-not $shimBinDir) { return $null }
    foreach ($shimName in @("pseudolife-mcp.exe", "pseudolife-mcp")) {
        $candidatePath = Join-Path "$shimBinDir" $shimName
        if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidatePath).Path
        }
    }
    return $null
}
function Install-ShimOnce {
    if ($null -ne $script:shimInstallResult) { return $script:shimInstallResult }
    # NOTE: every native command in here pipes to Out-Host — a PS function
    # returns ALL uncaptured output, so a bare `pipx install` would pollute
    # the boolean return and make failures read as success at the call site
    # (the 2026-07-19 Invoke-WithRetry lesson; $LASTEXITCODE survives the pipe).
    $shimInstalled = $false
    $shimManager = $null
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        # --force also replaces an existing environment when the checkout's
        # version matches the installed one, so a stale same-version PyPI shim
        # cannot survive an installer rerun.
        pipx install --force $repo 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 0) {
            $shimManager = @{ Cmd = "pipx"; Args = @() }
        } else {
            Write-Warning "pipx install --force from the checkout failed (exit $LASTEXITCODE)."
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
            # A direct local requirement is rebuilt and reinstalled even at
            # the same version; --upgrade also refreshes changed requirements.
            & $exe @exeArgs -m pip install --user --upgrade $repo 2>&1 | Out-Host
            if ($LASTEXITCODE -eq 0) {
                $shimManager = @{ Cmd = $exe; Args = $exeArgs }
                break
            } else {
                Write-Warning "$($candidate.Label) -m pip install from the checkout failed (exit $LASTEXITCODE)."
            }
        }
    }
    if ($shimManager) {
        $script:shimInstallPath = Resolve-InstalledShimPath $shimManager
        $shimInstalled = [bool]$script:shimInstallPath
        if (-not $shimInstalled) {
            Write-Warning "Shim installation completed, but its installed executable was not found in the manager's scripts directory."
        }
    }
    $script:shimInstallResult = $shimInstalled
    return $shimInstalled
}

function Get-EnvFlag($cli) {
    $help = & $cli mcp add --help 2>$null
    if ("$help" -match '--env') { return "--env" }
    return $null
}
function Test-McpHttpRegistration([string]$config) {
    return $config -match '(?im)(?:^|\s)"?(?:transport|type)"?\s*:\s*"?(?:streamable_)?http"?(?:[,}\s]|$)|\(http\)'
}
function Test-NoSpawnGuardEnabled([string]$config) {
    return $config -match '(?im)(?:^|[,{}])[ \t]*"?PSEUDOLIFE_MCP_NO_SPAWN"?[ \t]*[:=][ \t]*"?(?:1|true|yes|on)"?[ \t]*(?:[,}]|\r?$)'
}
function Get-RegisteredStdioCommand([string]$config) {
    if ($config -match '(?mi)^\s*"?command"?\s*:\s*"?([^"\r\n]+?)"?[,]?\s*$') {
        return $Matches[1].Trim()
    }
    return $null
}
function Write-UnverifiedNoSpawnGuard([string]$client, [string]$verification) {
    Write-Warning "The existing $client stdio registration's no-spawn guard is missing or cannot be verified; the registration was preserved and Docker-tier setup is incomplete."
    Write-Host "  Edit the existing registration in place and set PSEUDOLIFE_MCP_NO_SPAWN=1; preserve its command, arguments, daemon URL, token file, and all other environment values."
    Write-Host "  Re-run this installer after verifying the effective value with $verification."
}

# Two env pairs ride each shim registration: PSEUDOLIFE_WRITER_ID (the shim
# forwards it as the X-PL-Writer header — per-provider write attribution)
# and PSEUDOLIFE_MCP_NO_SPAWN=1 (Docker-tier no-spawn guard, 2026-08-29
# incident). CLI env-flag support is probed, never assumed: a missing flag
# fails closed before registration. HTTP transport cannot carry env, so there
# the daemon default (ops/.env) applies and no shim exists to spawn anything.
function Get-InstallerPython {
    # A python >= 3.10 for the stdlib-only ops helpers. Probe candidates
    # independently: Store aliases and stale launchers must not block the
    # next one. Never alters the user's PATH.
    foreach ($candidate in @("python", "python3", "py")) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $probeArgs = if ($candidate -eq "py") { @("-3") } else { @() }
        try {
            $probe = & $candidate @probeArgs -c 'import sys; sys.exit(1) if sys.version_info < (3, 10) else print(sys.executable)' 2>$null
            if (($LASTEXITCODE -eq 0) -and $probe) { return "$probe".Trim() }
        } catch { continue }
    }
    return $null
}
# Claude Desktop launches MCP servers with a sanitized environment, so a
# token-gated daemon needs a token FILE path on the entry - never the value,
# and never an OS env var, which Desktop cannot see (2026-09-19 incident).
# Honour an explicit PSEUDOLIFE_MCP_TOKEN_FILE; otherwise, when any token is
# configured for the daemon, use a private default path. The registrar
# WRITES that file (owner-only) from the token source below, or migrates a
# literal already in the entry, and never points the entry at a file it did
# not write or validate.
function Get-DesktopTokenFile {
    if ($env:PSEUDOLIFE_MCP_TOKEN_FILE) { return $env:PSEUDOLIFE_MCP_TOKEN_FILE }
    $gated = $env:PSEUDOLIFE_MCP_TOKEN -or $env:PSEUDOLIFE_MCP_TOKENS -or
        (Get-EnvValue "PSEUDOLIFE_MCP_TOKEN") -or (Get-EnvValue "PSEUDOLIFE_MCP_TOKENS")
    if (-not $gated) { return $null }
    return (Join-Path $env:USERPROFILE ".pseudolife-mcp\claude-desktop.token")
}
# The singular daemon token, from the installer's environment or ops/.env
# (a per-principal PSEUDOLIFE_MCP_TOKENS map names no single value to copy).
function Get-DesktopTokenSource {
    if ($env:PSEUDOLIFE_MCP_TOKEN) { return $env:PSEUDOLIFE_MCP_TOKEN }
    $fromEnvFile = Get-EnvValue "PSEUDOLIFE_MCP_TOKEN"
    if ($fromEnvFile) { return $fromEnvFile }
    return $null
}
function Get-DesktopTokensSource {
    if ($env:PSEUDOLIFE_MCP_TOKENS) { return $env:PSEUDOLIFE_MCP_TOKENS }
    $fromEnvFile = Get-EnvValue "PSEUDOLIFE_MCP_TOKENS"
    if ($fromEnvFile) { return $fromEnvFile }
    return $null
}

$mcpState = @{}
# EAP=Stop does not trap native exit codes: every `mcp add` must be
# exit-checked, or the closing ladder can claim a transport that was never
# registered (review finding, 2026-08-29).
function Register-Result($provider, $okState, $okMessage) {
    if ($LASTEXITCODE -eq 0) {
        $mcpState[$provider] = $okState
        if ($okMessage) { Step $okMessage }
    } else {
        Write-Warning "$provider MCP registration failed (exit $LASTEXITCODE) - see the error above."
        $mcpState[$provider] = "failed"
    }
}
function Set-CodexRuntimeDefaults {
    if (-not $codexPython) {
        $mcpState["codex"] = "failed"
        $script:codexRuntimeDefaults = "failed"
        $script:codexRuntimeRecovery = "Install Python 3.10 or newer, then run python ops/setup-codex-coordination.py --runtime-defaults."
        Write-Warning "Codex was registered, but its runtime defaults could not be configured because Python 3.10 or newer was not found."
        return
    }
    try {
        $runtimeOutput = & $codexPython (Join-Path $repo "ops/setup-codex-coordination.py") --runtime-defaults
        $runtimeExit = $LASTEXITCODE
        $runtimeResult = ($runtimeOutput -join "`n") | ConvertFrom-Json
        if (($runtimeExit -ne 0) -or ($runtimeResult.status -ne "ready") -or
            ($runtimeResult.runtime_defaults -notin "configured", "preserved")) {
            throw "Invalid runtime-default setup result"
        }
        $script:codexRuntimeDefaults = [string]$runtimeResult.runtime_defaults
        Step "Codex runtime defaults ready (startup 240s, tools 240s, required)."
    } catch {
        $mcpState["codex"] = "failed"
        $script:codexRuntimeDefaults = "failed"
        $script:codexRuntimeRecovery = "Run python ops/setup-codex-coordination.py --runtime-defaults, then retry the Codex task."
        Write-Warning "Codex was registered, but its runtime defaults were not confirmed. $script:codexRuntimeRecovery"
    }
}
foreach ($selectedClient in $clients) {
    if ($selectedClient -eq "claude-desktop") {
        # No `mcp add` CLI: the entry is merged into claude_desktop_config.json
        # by ops/register_claude_desktop.py (absolute shim path - Desktop's
        # sanitized PATH omits pipx/venv bin dirs; token FILE when gated). It is
        # named pseudolife-desktop so Code-tab sessions keep their own
        # per-session pseudolife-memory server.
        if ($Transport -ne "shim") {
            Write-Warning "Claude Desktop needs the stdio shim (its connector dialog rejects plain-http URLs) - ignoring -Transport http for it."
        }
        $shimReady = Install-ShimOnce
        $desktopPython = Get-InstallerPython
        if ((-not $shimReady) -or (-not $script:shimInstallPath)) {
            Write-Warning "pseudolife-mcp shim installation did not yield a usable executable - Claude Desktop not wired. Re-run after fixing pipx/Python, or register by hand: python ops\register_claude_desktop.py --command <full path to pseudolife-mcp.exe>"
            $mcpState["claude-desktop"] = "failed"
            continue
        }
        if (-not $desktopPython) {
            Write-Warning "No python >= 3.10 found to write claude_desktop_config.json - Claude Desktop not wired."
            $mcpState["claude-desktop"] = "failed"
            continue
        }
        $desktopArgs = @("--command", $script:shimInstallPath, "--writer-id", "claude-desktop")
        $desktopTokenFile = Get-DesktopTokenFile
        if ($desktopTokenFile) {
            if ($env:PSEUDOLIFE_MCP_TOKEN_FILE) {
                $desktopArgs += @("--token-file", $desktopTokenFile)
            } else {
                $desktopArgs += @("--default-token-file", $desktopTokenFile)
            }
            # The token value rides a process-scoped env var the registrar
            # reads by NAME - never a command-line argument, never printed.
            $env:PSEUDOLIFE_DESKTOP_TOKEN_SOURCE = Get-DesktopTokenSource
            $env:PSEUDOLIFE_DESKTOP_TOKENS_SOURCE = Get-DesktopTokensSource
            if ($env:PSEUDOLIFE_DESKTOP_TOKEN_SOURCE) {
                $desktopArgs += @("--token-from-env", "PSEUDOLIFE_DESKTOP_TOKEN_SOURCE")
            } elseif ($env:PSEUDOLIFE_DESKTOP_TOKENS_SOURCE) {
                $desktopArgs += @("--tokens-from-env", "PSEUDOLIFE_DESKTOP_TOKENS_SOURCE")
            } else {
                Write-Warning "The daemon is token-gated but no token source is set (environment or ops\.env) - the registrar can only reuse a credential already in the Desktop entry. If registration fails, configure a singular token or exactly one claude-desktop principal in PSEUDOLIFE_MCP_TOKENS."
            }
        }
        & $desktopPython (Join-Path $repo "ops\register_claude_desktop.py") @desktopArgs 2>&1 | Out-Host
        $env:PSEUDOLIFE_DESKTOP_TOKEN_SOURCE = $null
        $env:PSEUDOLIFE_DESKTOP_TOKENS_SOURCE = $null
        Register-Result "claude-desktop" "shim-env" "Wired into Claude Desktop as pseudolife-desktop via the pseudolife-mcp shim (claude_desktop_config.json) - fully quit and relaunch Desktop to load it."
        continue
    }
    if ($selectedClient -eq "generic") {
        Write-Host ""
        Step "Other MCP-capable agents - paste-ready config:"
        Write-Host ""
        Show-GenericSnippets
        Write-Host ""
        continue
    }
    if ($selectedClient -eq "codex") {
        if ($codexCredentialBootstrapFailed) {
            $mcpState["codex"] = "failed"
            Write-Warning "Codex MCP registration was skipped because credential setup failed. Rerun the installer after repairing the reported credential problem."
            continue
        }
        $existingCodex = codex mcp get pseudolife-memory 2>$null | Out-String
        if ($LASTEXITCODE -eq 0) {
            $existingCodexGuard = codex mcp get pseudolife-memory --json 2>$null | Out-String
            if ($LASTEXITCODE -ne 0) { $existingCodexGuard = $existingCodex }
            $codexGuardUnverified = $false
            if (($Transport -eq "shim") -and -not (Test-McpHttpRegistration $existingCodexGuard) -and -not (Test-McpHttpRegistration $existingCodex)) {
                if (-not (Test-NoSpawnGuardEnabled $existingCodexGuard)) {
                    Write-UnverifiedNoSpawnGuard "Codex" "codex mcp get pseudolife-memory --json"
                    $codexGuardUnverified = $true
                }
                $registeredShim = Get-RegisteredStdioCommand $existingCodex
                $bareRegisteredShim = $registeredShim -match '(?i)^pseudolife-mcp(?:\.exe)?$'
                $managedRegisteredShim = $bareRegisteredShim
                if (-not $managedRegisteredShim) {
                    $knownInstalledShim = Resolve-InstalledShimPath
                    if ($knownInstalledShim -and $registeredShim) {
                        $registeredShim = if (Test-Path -LiteralPath $registeredShim -PathType Leaf) { (Resolve-Path -LiteralPath $registeredShim).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        $managedRegisteredShim = $registeredShim -and [string]::Equals($registeredShim, $knownInstalledShim, $pathComparison)
                    }
                }
                if ($managedRegisteredShim) {
                    if (Install-ShimOnce) {
                        if (-not $bareRegisteredShim) {
                            Step "Codex registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            $mcpState["codex"] = "present-upgraded"
                        } else {
                        $resolvedShim = Get-Command pseudolife-mcp -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                        $resolvedShimPath = if ($resolvedShim) { (Resolve-Path -LiteralPath $resolvedShim.Source).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        if ($resolvedShimPath -and [string]::Equals($resolvedShimPath, $script:shimInstallPath, $pathComparison)) {
                            Step "Codex registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            $mcpState["codex"] = "present-upgraded"
                        } else {
                            Write-Warning "The existing Codex registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run."
                            $mcpState["codex"] = "failed"
                        }
                        }
                    } else {
                        Write-Warning "The existing Codex registration was preserved, but its pseudolife-mcp shim upgrade failed - see the pip/pipx output above and re-run."
                        $mcpState["codex"] = "failed"
                    }
                } else {
                    Write-Warning "The existing Codex stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update."
                    $mcpState["codex"] = "present-custom"
                }
                if ($codexGuardUnverified) { $mcpState["codex"] = "failed" }
            } else {
                Step "MCP server already wired into Codex - registration preserved."
                $mcpState["codex"] = "present"
            }
            $codexRuntimeDefaults = "preserved"
        } elseif (($Transport -eq "shim") -and (Install-ShimOnce)) {
            $envFlag = Get-EnvFlag "codex"
            if ($envFlag) {
                # Name first, env after — the documented codex form (an env
                # flag directly before the name risks the variadic-option
                # parse that breaks claude's CLI).
                # PSEUDOLIFE_MCP_NO_SPAWN: Docker-tier install — the shim
                # must wait for the compose container, never spawn a
                # host-side fallback that can win the port-bind race against
                # a still-booting Docker Desktop and shadow the real bank
                # (2026-08-29 incident). Flag repeated per pair: codex's
                # --env takes one KEY=VALUE per occurrence.
                if ($codexConnectionConfigured -and $codexCredentialFile) {
                    codex mcp add pseudolife-memory $envFlag PSEUDOLIFE_WRITER_ID=codex $envFlag PSEUDOLIFE_MCP_NO_SPAWN=1 $envFlag "PSEUDOLIFE_MCP_DAEMON_URL=$codexCredentialUrl" $envFlag "PSEUDOLIFE_MCP_TOKEN_FILE=$codexCredentialFile" -- $script:shimInstallPath
                } elseif ($codexConnectionConfigured) {
                    codex mcp add pseudolife-memory $envFlag PSEUDOLIFE_WRITER_ID=codex $envFlag PSEUDOLIFE_MCP_NO_SPAWN=1 $envFlag "PSEUDOLIFE_MCP_DAEMON_URL=$codexCredentialUrl" -- $script:shimInstallPath
                } else {
                    codex mcp add pseudolife-memory $envFlag PSEUDOLIFE_WRITER_ID=codex $envFlag PSEUDOLIFE_MCP_NO_SPAWN=1 -- $script:shimInstallPath
                }
                Register-Result "codex" "shim-env" "Wired into Codex via the pseudolife-mcp shim - per-session identity (a Codex session no longer inherits a concurrent Claude session's episode)."
                if ($mcpState["codex"] -eq "shim-env") { Set-CodexRuntimeDefaults }
            } else {
                Write-Warning "This Codex CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed."
                $mcpState["codex"] = "failed"
            }
        } else {
            if ($Transport -eq "shim") {
                Write-Warning "Shim unavailable for Codex (see warnings above) - falling back to HTTP."
                Write-Host "  Without the shim, a Codex session running beside a Claude Code session shares its episode identity."
            }
            if ($codexCredentialFile) {
                Write-Warning "Codex authentication requires the stdio shim; HTTP fallback was not registered."
                $mcpState["codex"] = "failed"
            } else {
                $codexHttpUrl = if ($codexCredentialUrl) {
                    "$codexCredentialUrl/mcp"
                } else {
                    "http://127.0.0.1:8765/mcp"
                }
                codex mcp add pseudolife-memory --url $codexHttpUrl
                Register-Result "codex" "http" "Wired into Codex (codex mcp add, HTTP)."
                if ($mcpState["codex"] -eq "http") { Set-CodexRuntimeDefaults }
            }
        }
    } elseif ($selectedClient -eq "gemini") {
        $geminiList = gemini mcp list 2>$null
        if ("$geminiList" -match "pseudolife-memory") {
            $geminiRegistration = ("$geminiList" -split "`r?`n" | Where-Object { $_ -match 'pseudolife-memory:' } | Select-Object -First 1)
            $geminiGuardUnverified = $false
            if (($Transport -eq "shim") -and -not (Test-McpHttpRegistration "$geminiRegistration")) {
                # `gemini mcp list` does not expose env values, so an existing
                # stdio guard cannot be verified without inspecting settings.
                Write-UnverifiedNoSpawnGuard "Gemini CLI" "the pseudolife-memory entry in ~/.gemini/settings.json"
                $geminiGuardUnverified = $true
                $bareRegisteredShim = "$geminiRegistration" -match '(?i)pseudolife-memory:\s*pseudolife-mcp(?:\.exe)?\s*\(stdio\)'
                $managedRegisteredShim = $bareRegisteredShim
                if (-not $managedRegisteredShim) {
                    $knownInstalledShim = Resolve-InstalledShimPath
                    if ($knownInstalledShim -and ("$geminiRegistration" -match '(?i)pseudolife-memory:\s*(.+?)\s*\(stdio\)')) {
                        $registeredShim = if (Test-Path -LiteralPath $Matches[1] -PathType Leaf) { (Resolve-Path -LiteralPath $Matches[1]).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        $managedRegisteredShim = $registeredShim -and [string]::Equals($registeredShim, $knownInstalledShim, $pathComparison)
                    }
                }
                if ($managedRegisteredShim) {
                    if (Install-ShimOnce) {
                        if (-not $bareRegisteredShim) {
                            Step "Gemini CLI registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            $mcpState["gemini"] = "present-upgraded"
                        } else {
                        $resolvedShim = Get-Command pseudolife-mcp -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                        $resolvedShimPath = if ($resolvedShim) { (Resolve-Path -LiteralPath $resolvedShim.Source).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        if ($resolvedShimPath -and [string]::Equals($resolvedShimPath, $script:shimInstallPath, $pathComparison)) {
                            Step "Gemini CLI registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            $mcpState["gemini"] = "present-upgraded"
                        } else {
                            Write-Warning "The existing Gemini CLI registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run."
                            $mcpState["gemini"] = "failed"
                        }
                        }
                    } else {
                        Write-Warning "The existing Gemini CLI registration was preserved, but its pseudolife-mcp shim upgrade failed - see the pip/pipx output above and re-run."
                        $mcpState["gemini"] = "failed"
                    }
                } else {
                    Write-Warning "The existing Gemini CLI stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update."
                    $mcpState["gemini"] = "present-custom"
                }
                if ($geminiGuardUnverified) { $mcpState["gemini"] = "failed" }
            } else {
                Step "MCP server already wired into Gemini CLI - registration preserved."
                $mcpState["gemini"] = "present"
            }
        } elseif (($Transport -eq "shim") -and (Install-ShimOnce)) {
            # Probe gemini's own spelling (`-e, --env`): the command below
            # emits the short form, so a help listing only `-e` must still
            # count as env support (Get-EnvFlag matches `--env` alone,
            # which is claude/codex's spelling).
            $geminiHelp = gemini mcp add --help 2>$null
            $envFlag = if ("$geminiHelp" -match '--env|-e,') { "-e" } else { $null }
            if ($envFlag) {
                # -e repeated per pair (one KEY=VALUE each, verified on
                # gemini CLI 0.57.0); PSEUDOLIFE_MCP_NO_SPAWN carries the
                # same Docker-tier no-spawn guard as the claude and codex
                # registrations (2026-08-29 incident).
                gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 pseudolife-memory $script:shimInstallPath
                Register-Result "gemini" "shim-env" "Wired into Gemini CLI via the pseudolife-mcp shim - per-session identity."
            } else {
                Write-Warning "This Gemini CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed."
                $mcpState["gemini"] = "failed"
            }
        } else {
            if ($Transport -eq "shim") {
                Write-Warning "Shim unavailable for Gemini CLI (see warnings above) - falling back to HTTP."
            }
            gemini mcp add -s user -t http pseudolife-memory http://127.0.0.1:8765/mcp
            Register-Result "gemini" "http" "Wired into Gemini CLI (gemini mcp add, HTTP)."
        }
    } else {
        $existingClaude = claude mcp get pseudolife-memory 2>$null | Out-String
        if ($LASTEXITCODE -eq 0) {
            $claudeGuardUnverified = $false
            if (($Transport -eq "shim") -and -not (Test-McpHttpRegistration $existingClaude)) {
                if (-not (Test-NoSpawnGuardEnabled $existingClaude)) {
                    Write-UnverifiedNoSpawnGuard "Claude Code" "claude mcp get pseudolife-memory"
                    $claudeGuardUnverified = $true
                }
                $registeredShim = Get-RegisteredStdioCommand $existingClaude
                $bareRegisteredShim = $registeredShim -match '(?i)^pseudolife-mcp(?:\.exe)?$'
                $managedRegisteredShim = $bareRegisteredShim
                if (-not $managedRegisteredShim) {
                    $knownInstalledShim = Resolve-InstalledShimPath
                    if ($knownInstalledShim -and $registeredShim) {
                        $registeredShim = if (Test-Path -LiteralPath $registeredShim -PathType Leaf) { (Resolve-Path -LiteralPath $registeredShim).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        $managedRegisteredShim = $registeredShim -and [string]::Equals($registeredShim, $knownInstalledShim, $pathComparison)
                    }
                }
                if ($managedRegisteredShim) {
                    if (Install-ShimOnce) {
                        if (-not $bareRegisteredShim) {
                            Step "Claude Code registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            $mcpState["claude"] = "present-upgraded"
                        } else {
                        $resolvedShim = Get-Command pseudolife-mcp -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                        $resolvedShimPath = if ($resolvedShim) { (Resolve-Path -LiteralPath $resolvedShim.Source).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        if ($resolvedShimPath -and [string]::Equals($resolvedShimPath, $script:shimInstallPath, $pathComparison)) {
                            Step "Claude Code registration preserved; upgraded its pseudolife-mcp shim from this checkout."
                            $mcpState["claude"] = "present-upgraded"
                        } else {
                            Write-Warning "The existing Claude Code registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run."
                            $mcpState["claude"] = "failed"
                        }
                        }
                    } else {
                        Write-Warning "The existing Claude Code registration was preserved, but its pseudolife-mcp shim upgrade failed - see the pip/pipx output above and re-run."
                        $mcpState["claude"] = "failed"
                    }
                } else {
                    Write-Warning "The existing Claude Code stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update."
                    $mcpState["claude"] = "present-custom"
                }
                if ($claudeGuardUnverified) { $mcpState["claude"] = "failed" }
            } else {
                Step "MCP server already wired into Claude Code - registration preserved."
                $mcpState["claude"] = "present"
            }
        } elseif ($Transport -eq "shim") {
            if (Install-ShimOnce) {
                $envFlag = Get-EnvFlag "claude"
                if ($envFlag) {
                    # --env is variadic and must come AFTER the server name:
                    # placed earlier it swallows the name as another
                    # KEY=value pair and the whole add fails (verified
                    # against the claude CLI 2026-08-29; the `--` separator
                    # ends the value list).
                    # PSEUDOLIFE_MCP_NO_SPAWN: Docker-tier shims wait for
                    # the compose daemon instead of spawning a fallback that
                    # can shadow the real bank (see the Codex registration
                    # above).
                    claude mcp add --scope user pseudolife-memory $envFlag PSEUDOLIFE_WRITER_ID=claude-code PSEUDOLIFE_MCP_NO_SPAWN=1 -- $script:shimInstallPath
                    Register-Result "claude" "shim-env" "Wired into Claude Code via the pseudolife-mcp shim - per-session identity (required for correct episodes with concurrent sessions)."
                } else {
                    Write-Warning "This Claude CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed."
                    $mcpState["claude"] = "failed"
                }
            } else {
                Write-Warning "Could not install the pseudolife-mcp shim - no working pipx or Python (>=3.10, py -3 or python) was found, or the shim install itself failed (see warnings above)."
                Write-Host "  Without the shim, concurrent Claude Code sessions share one episode identity."
                Write-Host "  Install pipx or Python >=3.10 and re-run, or pass -Transport http to silence this."
                claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
                Register-Result "claude" "http" "Wired into Claude Code via HTTP (fallback - shim tooling not found or shim install failed)."
            }
        } else {
            claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
            Register-Result "claude" "http" "Wired into Claude Code via HTTP (-Transport http)."
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
        "present-upgraded" { "already wired; checkout shim upgraded" }
        "present-custom" { "already wired with custom command (unchanged)" }
        "failed" { "registration or shim upgrade FAILED - see the warning above and re-run" }
        default { "not wired" }
    }
}
function Get-McpMarker($state) {
    if ($state -in "shim-env", "shim", "http", "present", "present-upgraded", "present-custom") { "[x]" } else { "[!]" }
}
function Describe-Instr($state) {
    if (-not $state) { return "[-] Standing file        skipped" }
    $tail = ($state -split ":", 2)[-1]
    switch -Wildcard ($state) {
        "appended:*" { "[x] Standing file        $tail" }
        "present:*" { "[x] Standing file        $tail" }
        "covered-by-plugin" { "[-] Standing file        skipped - the plugin serves a compact memory core; append examples\CLAUDE.memory.md for the full guide" }
        "covered-by-hooks" { "[-] Standing file        skipped - verified hooks serve a compact memory core; append examples\CLAUDE.memory.md for the full guide" }
        "present" { "[x] Standing file        existing Codex memory fallback" }
        "appended" { "[x] Standing file        Codex memory fallback appended" }
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
            Write-Host "    $(Get-McpMarker $mcpState['claude']) MCP transport        $(Describe-Mcp $mcpState['claude'])"
            Write-Host "    [x] Server instructions  automatic (MCP instructions field)"
            if ($hookState["claude"] -eq "plugin") {
                Write-Host "    [x] Session briefing     Claude Code plugin"
                Write-Host "    [x] Per-turn discipline  Claude Code plugin"
            } else {
                Write-Host "    [x] Session briefing     SessionStart hook -> ~/.claude/settings.json"
                Write-Host "    [x] Per-turn discipline  UserPromptSubmit hook"
            }
            Write-Host "    $(Describe-Plugin $script:pluginClaude)"
            if ($script:legacyClaude) { Write-Host "    $(Describe-LegacyHooks $script:legacyClaude)" }
            Write-Host "    $(Describe-Instr $instrState['claude'])"
        }
        "claude-desktop" {
            Write-Host "  Claude Desktop"
            Write-Host "    $(Get-McpMarker $mcpState['claude-desktop']) MCP transport        $(Describe-Mcp $mcpState['claude-desktop'])"
            Write-Host "    [x] Server instructions  automatic (MCP instructions field)"
            Write-Host "    [!] Session briefing     unavailable - Claude Desktop has no hook system"
            Write-Host "    [!] Per-turn discipline  unavailable"
            Write-Host "    [-] Standing file        none - Desktop reads no CLAUDE.md"
            Write-Host "    Restart: fully quit Claude Desktop (tray / menu-bar icon) and relaunch to load the entry."
        }
        "codex" {
            Write-Host "  OpenAI Codex"
            Write-Host "    $(Get-McpMarker $mcpState['codex']) MCP transport        $(Describe-Mcp $mcpState['codex'])"
            Write-Host "    [x] Server instructions  automatic (MCP instructions field)"
            if ($hookState["codex"] -eq "ready") {
                Write-Host "    [x] Memory hooks         trusted and verified ($($codexSetup.source))"
            } else {
                Write-Host "    [!] Memory hooks         $($hookState['codex'])"
                if ($codexSetup.recovery) { Write-Host "    $($codexSetup.recovery)" }
            }
            Write-Host "    Verify runtime: codex mcp get pseudolife-memory; run doctor from that command's environment."
            switch ($codexRuntimeDefaults) {
                "configured" { Write-Host "    [x] Runtime defaults     startup 240s; tools 240s; required" }
                "preserved" { Write-Host "    [-] Runtime settings     existing registration unchanged" }
                "failed" { Write-Host "    [!] Runtime defaults     not confirmed - $codexRuntimeRecovery" }
                default { Write-Host "    [!] Runtime defaults     unavailable" }
            }
            Write-Host "    $(Describe-Instr $instrState['codex'])"
        }
        "gemini" {
            Write-Host "  Gemini CLI"
            Write-Host "    $(Get-McpMarker $mcpState['gemini']) MCP transport        $(Describe-Mcp $mcpState['gemini'])"
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
    { $_ -in "sonnet-fallback", "codex-fallback" } {
        Write-Host "Verify: memory_dream(action=""status"") - fallback_url set and primary_healthy: true (shim up)."
    }
    { $_ -in "sonnet-only", "codex-only" } {
        Write-Host "Verify: memory_dream(action=""status"") - primary_url on :$ShimPort, extractor_mode: primary."
        Write-Host "Note: dreams pause (and retry next sweep) whenever the shim is down or the CLI is logged out."
    }
}
if ($codexShimMode) {
    Write-Host "Note: Codex-served extraction quality is unmeasured - see the 'OpenAI primary' section of docs/guide/dreaming.md."
}
Write-Host "Done. First session: tell your coding agent to remember something."
