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
#   ops\install.ps1 -Extractor claude-only -Client claude,gemini
#   ops\install.ps1 -Extractor claude-fallback -Instructions append
#   ops\install.ps1 -Extractor openai-fallback -Client codex
#   ops\install.ps1 -Extractor endpoint -ExtractorUrl http://127.0.0.1:1234/v1 `
#       -Model qwen3.6-27b -Client claude
#   ops\install.ps1 -DaemonUrl http://100.64.0.2:8765 `
#       -TokenFile ~\.pseudolife-mcp\claude-code.token -ReadToken -Client claude
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
# -NoToken (switch): open-loopback install - mint no bearer token, so the
#   agent board stays off.
#
# Client-only install (this machine runs no daemon; one runs elsewhere,
# typically reached over a tailnet): no Docker, volumes, ops/.env, token
# minting or local daemon. Shim, registrations, plugin, hooks and standing
# instructions are set up as usual, aimed at the remote daemon.
# -DaemonUrl <url>: the daemon's URL (default: the PSEUDOLIFE_MCP_DAEMON_URL
#   environment variable). A host other than 127.0.0.1, localhost or ::1
#   implies -ClientOnly.
# -TokenFile <path>: client-only - an owner-only file holding the daemon's
#   bearer token (default: PSEUDOLIFE_MCP_TOKEN_FILE); the installer never
#   mints one here.
# -ClientOnly (switch): wire clients to -DaemonUrl only; with a loopback URL,
#   for an SSH tunnel.
# -ReadToken (switch): client-only - create the -TokenFile (it must not exist
#   yet) from the token typed, unechoed, or piped on stdin.
# One token file is one principal: every client a run wires shares it. For
# per-client attribution on the board, run the installer once per client,
# each run with that client's own token file.
#
# Extractor modes (spec: docs/superpowers/specs/
# 2026-07-14-installer-extractor-choice-design.md; the modes and models:
# docs/guide/dreaming.md, "Extractor modes and dreamer models"):
#   claude-only      Claude CLI shim only — the ~11.8 GB sidecar image is never built
#   claude-fallback  Claude CLI shim primary, sidecar fallback
#   openai-only      Codex CLI (ChatGPT-plan) shim only — sidecar never built
#   openai-fallback  Codex CLI shim primary, sidecar fallback
#   endpoint         any OpenAI-compatible server you name (-ExtractorUrl,
#                    -Model) — sidecar never built
#   endpoint-fallback  that server primary, sidecar fallback
#   sidecar          bundled local CPU extractor only (no plan or server needed)
# The earlier spellings sonnet-only, sonnet-fallback, codex-only and
# codex-fallback still work, as deprecated aliases of the claude-* and
# openai-* modes.
# -Model <name>: the dreamer model - one the CLI shim modes list
#   (docs/guide/dreaming.md), or any name an endpoint mode's server serves.
# -ExtractorUrl <url>: endpoint modes - the server's OpenAI-compatible base
#   URL (where /models and /chat/completions live, usually /v1).
param(
    # The modes, and the deprecated spellings the extractor modes block maps.
    [ValidateSet("", "sidecar", "claude-only", "claude-fallback", "openai-only",
                 "openai-fallback", "endpoint", "endpoint-fallback",
                 "sonnet-only", "sonnet-fallback", "codex-only", "codex-fallback")]
    [string]$Extractor = "",
    # Checked by the extractor modes block: an endpoint mode takes any name.
    [string]$Model = "",
    [string]$ExtractorUrl = "",
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
    [switch]$NoArt,
    # Open-loopback install: mint no bearer token, so the agent board stays off.
    [switch]$NoToken,
    # Client-only install against a daemon running elsewhere (see the header).
    [string]$DaemonUrl = "",
    [string]$TokenFile = "",
    [switch]$ClientOnly,
    [switch]$ReadToken
)
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repo "ops\docker-compose.yml"
$envFile = Join-Path $repo "ops\.env"
$overrideFile = Join-Path $repo "ops\docker-compose.override.yml"
$OverrideMarker = "# pseudolife-mcp install: managed override (sidecar disabled) — do not edit; installer rewrites/removes this file"
# Pre-codex installs wrote the mode-specific text; keep recognizing it so a
# mode switch still removes/rewrites their override file.
$LegacyOverrideMarker = "# pseudolife-mcp install: managed override (sonnet-only) — do not edit; installer rewrites/removes this file"
# ... and the text between the codex modes (2026-08-31) and the endpoint modes.
$LegacyShimOverrideMarker = "# pseudolife-mcp install: managed override (shim-only extractor) — do not edit; installer rewrites/removes this file"
$EnvBegin = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
$EnvEnd = "# <<< pseudolife-mcp install <<<"
$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

# Needed from here on: the client-only mode block asks this checkout's shim
# code whether a daemon URL is loopback.
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

# >>> extractor modes >>>
# The dream extractor modes, named for the extractor family, and the models
# the CLI shim modes offer. tests/test_extractor_model_lists.py keeps these
# lists identical to install.sh's, the Claude shim autostart scripts',
# docs/guide/dreaming.md's and the Console's. The model lists are the menu,
# not a gate: a CLI shim mode passes an id it does not list to the shim
# unchanged, so a model release is usable the day it ships. (-Model has no
# ValidateSet for that reason.)
$extractorModes = @("sidecar", "claude-only", "claude-fallback", "openai-only",
    "openai-fallback", "endpoint", "endpoint-fallback")
$claudeModels = @("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5",
    "claude-haiku-4-5", "claude-fable-5")
$openaiModels = @("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-6-sol",
    "gpt-6-luna")
# An explicit -Model "" is refused like a whitespace one; an absent -Model
# picks the menu's default.
$modelGiven = $PSBoundParameters.ContainsKey("Model")
# The model-era names (2026-07-14 to 2026-09-28) stay accepted.
$extractorAliases = @{
    "sonnet-only" = "claude-only"; "sonnet-fallback" = "claude-fallback"
    "codex-only" = "openai-only"; "codex-fallback" = "openai-fallback"
}
if ($Extractor -and $extractorAliases.ContainsKey($Extractor)) {
    Write-Host "note: -Extractor $Extractor is a deprecated spelling of -Extractor $($extractorAliases[$Extractor]), which this run uses."
    $Extractor = $extractorAliases[$Extractor]
}
if ($ExtractorUrl.EndsWith("/")) { $ExtractorUrl = $ExtractorUrl.Substring(0, $ExtractorUrl.Length - 1) }
function Test-ExtractorDisablesSidecar {
    # $true for the single-extractor modes.
    return $Extractor -in "claude-only", "openai-only", "endpoint"
}
function Test-ModelBlank {
    # $true for a -Model given empty or all whitespace.
    return ($modelGiven -or $Model) -and -not "$Model".Trim()
}
function Assert-ExtractorArgs {
    # The mode, its model and its URL, together.
    if (Test-ModelBlank) {
        Write-Host "-Model needs a model id (it was empty)"
        exit 2
    }
    if ($Extractor -notin $extractorModes) {
        Write-Host "invalid -Extractor '$Extractor' ($($extractorModes -join '|'))"
        exit 2
    }
    if ($Extractor -in "endpoint", "endpoint-fallback") {
        # Any server's own model names: none is listed here.
        if (-not $ExtractorUrl -or -not $Model) {
            Write-Host "-Extractor $Extractor needs -ExtractorUrl <the server's OpenAI-compatible base URL, e.g. http://127.0.0.1:1234/v1> and -Model <a model name that server serves>"
            exit 2
        }
        if ($ExtractorUrl -cnotmatch '^https?://.') {
            Write-Host "invalid -ExtractorUrl: expected the server's http(s) base URL, e.g. http://127.0.0.1:1234/v1"
            exit 2
        }
    } else {
        if ($ExtractorUrl) {
            Write-Host "-ExtractorUrl applies only to -Extractor endpoint or endpoint-fallback"
            exit 2
        }
        if ($Model -and ($Model -notin ($claudeModels + $openaiModels))) {
            if ($Extractor -eq "sidecar") {
                Write-Host "-Model '$Model' is not one this installer knows, and -Extractor sidecar serves only its bundled model; -Model picks a CLI shim or endpoint mode's model"
                exit 2
            }
            Write-Host "model $Model is not in this installer's known list; passing it to the shim unchanged (add it to the lists when it is a real release)"
        }
    }
}
if (Test-ModelBlank) {
    Write-Host "-Model needs a model id (it was empty)"
    exit 2
}
if ($Extractor) { Assert-ExtractorArgs }
# <<< extractor modes <<<

# >>> client-only mode >>>
# A client-only install wires this machine's clients to a daemon that runs
# elsewhere (typically over a tailnet). The URL comes from -DaemonUrl, else
# from PSEUDOLIFE_MCP_DAEMON_URL in the environment; a host other than
# loopback implies -ClientOnly, since no local daemon answers there.
function Get-DaemonUrlHost([string]$url) {
    # Its host, lowercased, without brackets, read from the text as given:
    # [Uri] would rewrite a short form such as 127.1 that the shim rejects.
    $authority = ($url -split '://', 2)[-1]
    $authority = ($authority -split '/', 2)[0]
    $authority = ($authority -split '@')[-1]
    $name = if ($authority.StartsWith("[")) {
        ($authority.Substring(1) -split '\]', 2)[0]
    } else { ($authority -split ':', 2)[0] }
    return $name.ToLowerInvariant()
}
function Test-LoopbackUrl([string]$url) {
    # The shim's own answer (daemon_url._is_loopback_url), so the installer
    # and the shim never disagree on a form such as 127.0.0.01 or
    # ::ffff:127.0.0.1. Exit 10/11 are its answers; anything else (no Python,
    # an import that failed) falls back to the host rule below.
    $python = Get-InstallerPython
    if ($python) {
        try {
            & $python -c "import sys; sys.path.insert(0, sys.argv[1]); from pseudolife_memory.daemon_url import _is_loopback_url; sys.exit(10 if _is_loopback_url(sys.argv[2]) else 11)" $repo $url 2>$null | Out-Null
            if ($LASTEXITCODE -eq 10) { return $true }
            if ($LASTEXITCODE -eq 11) { return $false }
        } catch { }
    }
    $name = Get-DaemonUrlHost $url
    if ($name -eq "localhost") { return $true }
    # Dotted quads without leading zeros (Python's ipaddress rejects those).
    if (($name -notmatch '^(0|[1-9]\d{0,2})(\.(0|[1-9]\d{0,2})){3}$') -and -not $name.Contains(":")) { return $false }
    $address = $null
    return [Net.IPAddress]::TryParse($name, [ref]$address) -and [Net.IPAddress]::IsLoopback($address)
}
$clientOnlyVia = ""
$daemonUrlFromEnv = $false
if (-not $DaemonUrl -and $env:PSEUDOLIFE_MCP_DAEMON_URL) {
    $DaemonUrl = "$env:PSEUDOLIFE_MCP_DAEMON_URL"
    $daemonUrlFromEnv = $true
}
if ($DaemonUrl.EndsWith("/")) { $DaemonUrl = $DaemonUrl.Substring(0, $DaemonUrl.Length - 1) }
if ($DaemonUrl) {
    # An origin only, as the shim requires (daemon_url._validated_daemon_url):
    # http(s), a host and an optional numeric port; no credentials, path,
    # query, fragment or whitespace. The URL is not echoed: it may hold a
    # password.
    $urlAuthority = ($DaemonUrl -split '://', 2)[-1]
    $urlPort = ""
    if ($urlAuthority -match '^\[.*\]:(.*)$') { $urlPort = $Matches[1] }
    elseif ($urlAuthority -match '^[^\[][^:]*:(.*)$') { $urlPort = $Matches[1] }
    $urlOk = ($DaemonUrl -cmatch '^https?://.') -and ($urlAuthority -notmatch '[/?#@\s]') -and
        ($urlPort -notmatch '\D') -and [bool](Get-DaemonUrlHost $DaemonUrl)
    if (-not $urlOk) {
        Write-Host "invalid daemon URL: use an http(s) origin without credentials, a path, query, or fragment (http://<host>:<port> or https://<host>)"
        exit 2
    }
    if (-not $ClientOnly -and -not (Test-LoopbackUrl $DaemonUrl)) {
        $ClientOnly = [switch]$true
        if ($daemonUrlFromEnv) {
            $clientOnlyVia = " (implied by PSEUDOLIFE_MCP_DAEMON_URL in the environment; unset it for a local install)"
        }
    }
}
if ($ClientOnly) {
    if (-not $DaemonUrl) {
        Write-Host "-ClientOnly needs the daemon's URL: pass -DaemonUrl http://<host>:8765, or set PSEUDOLIFE_MCP_DAEMON_URL"
        exit 2
    }
    $localFlags = @()
    if ($Extractor) { $localFlags += "-Extractor" }
    if ($ExtractorUrl) { $localFlags += "-ExtractorUrl" }
    if ($Model) { $localFlags += "-Model" }
    if ($ShimPort -ne 0) { $localFlags += "-ShimPort" }
    if ($NoToken) { $localFlags += "-NoToken" }
    if ($Transport -ne "shim") { $localFlags += "-Transport http" }
    if ($localFlags) {
        Write-Host "client-only install: the daemon runs elsewhere, so these flags do not apply: $($localFlags -join ' ') (a local daemon's settings, or an HTTP registration, which cannot carry the token file)$clientOnlyVia"
        exit 2
    }
} elseif ($TokenFile -or $ReadToken) {
    Write-Host "client-only install: -TokenFile and -ReadToken name a remote daemon's token, and a local install keeps its token in ops/.env. Add -ClientOnly -DaemonUrl <url>, or drop them"
    exit 2
}
# <<< client-only mode <<<

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

# >>> client-only notes >>>
function Show-ClientOnlyNotes {
    Write-Host @'
  This machine is a client only: the daemon, its bank and its dream
  extractor run on the other host, and nothing here starts, stops or
  upgrades them.
  - A new token there: write it into the token file here. The shims and
    the Claude Code hooks read that file on every call. Codex keeps its
    own copy in ~/.codex/pseudolife/token: write it there too.
  - A daemon upgrade there: bring this checkout to the same release and
    re-run this installer, so the shim matches the daemon.
'@
}
# <<< client-only notes <<<

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
# The status field of an ops/client_credentials.py JSON report, or $null.
function Get-HelperStatus($output) {
    try { return [string](($output -join "`n") | ConvertFrom-Json).status } catch { return $null }
}
# >>> client-only preflight >>>
# Nothing Docker-shaped to check: a client-only install depends on the
# token file and on the remote daemon answering. The token is never read
# into output.
function Invoke-ClientOnlyPreflight {
    Step "Client-only install$($clientOnlyVia): this machine's clients will use the daemon at $DaemonUrl (no Docker, volumes or local daemon here)."
    if (-not $script:TokenFile) { $script:TokenFile = "$env:PSEUDOLIFE_MCP_TOKEN_FILE" }
    if (-not $script:TokenFile) {
        Write-Host "client-only install: pass -TokenFile <path> (or set PSEUDOLIFE_MCP_TOKEN_FILE) naming the file that holds the remote daemon's bearer token, and -ReadToken to create it. The installer never mints one for a remote daemon$clientOnlyVia"
        exit 2
    }
    $script:TokenFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($script:TokenFile)
    $tokenPython = Get-InstallerPython
    if (-not $tokenPython) {
        throw "client-only install: Python 3.10 or newer is needed to check the token file and to install the shim. Install it, then re-run"
    }
    $helper = Join-Path $repo "ops/client_credentials.py"
    if ($ReadToken) {
        if (Test-Path -LiteralPath $script:TokenFile) {
            throw "client-only install: -ReadToken creates the token file, and it already exists: $($script:TokenFile). Drop -ReadToken to use it, or remove it first"
        }
        # Read unechoed (or from piped input) and handed to the helper on
        # stdin: the token never reaches an argument list or the output.
        $tokenValue = if ([Console]::IsInputRedirected) {
            [Console]::In.ReadLine()
        } else {
            [Net.NetworkCredential]::new("", (Read-Host -AsSecureString "Paste the daemon's bearer token (not shown)")).Password
        }
        $tokenReport = "$tokenValue" | & $tokenPython $helper write-token-file --path $script:TokenFile
        $tokenValue = $null
        if ("$(Get-HelperStatus $tokenReport)" -ne "written") {
            $problem = try { [string](($tokenReport -join "`n") | ConvertFrom-Json).recovery } catch { "no result" }
            throw "client-only install: could not write the token file ($problem): $($script:TokenFile)"
        }
        Step "Wrote the token file $($script:TokenFile) (owner-only; the token is not shown)."
    }
    $tokenItem = Get-Item -LiteralPath $script:TokenFile -Force -ErrorAction SilentlyContinue
    if (-not $tokenItem -or $tokenItem.PSIsContainer -or $tokenItem.Length -eq 0) {
        throw "client-only install: the token file is missing or empty: $($script:TokenFile). Create it with -ReadToken, or write the daemon's token into it owner-only, then re-run"
    }
    # The shim's own check, on every OS: an owner-only regular file (the ACL
    # on Windows, mode bits on POSIX), no link, one well-formed token.
    $tokenReport = & $tokenPython $helper check-token-file --path $script:TokenFile
    if ("$(Get-HelperStatus $tokenReport)" -ne "ready") {
        $problem = try { [string](($tokenReport -join "`n") | ConvertFrom-Json).recovery } catch { "no result" }
        throw "client-only install: the shim cannot use the token file ($problem): $($script:TokenFile). It reads only an owner-only regular file: chmod 600 it on Linux or macOS, or delete it and re-run with -ReadToken to create it again"
    }
    foreach ($selectedClient in $clients) {
        if (($selectedClient -in "claude", "codex", "gemini") -and
            -not (Get-Command $selectedClient -ErrorAction SilentlyContinue)) {
            throw "client-only install: the $selectedClient CLI is not on PATH. Install it, or drop it from -Client, then re-run"
        }
    }
    # A redirect is refused, as curl -f and the shim refuse one.
    $daemonHealth = $null
    try {
        $daemonHealth = Invoke-RestMethod -Uri "$DaemonUrl/health" -TimeoutSec 5 -MaximumRedirection 0
    } catch { $daemonHealth = $null }
    if (-not $daemonHealth -or ($daemonHealth.status -ne "ok")) {
        Write-Host "client-only install: no healthy daemon answered at $DaemonUrl/health.$clientOnlyVia"
        Write-Host "  The daemon must be exposed to this machine, for example through the tailnet: check the host and port, that the daemon listens beyond loopback on its host, and that this machine reaches it (Invoke-RestMethod $DaemonUrl/health)."
        exit 1
    }
    if (-not (Test-LoopbackUrl $DaemonUrl)) {
        if (($daemonHealth.PSObject.Properties.Name -contains "auth") -and ($daemonHealth.auth -eq $false)) {
            throw "client-only install: the daemon runs without a bearer token (auth: false) at $DaemonUrl. An unauthenticated bank must never be reached over a network: set PSEUDOLIFE_MCP_TOKEN for the daemon on its host, restart it, and re-run"
        }
        if ($DaemonUrl -like "http://*") {
            Write-Warning "$DaemonUrl is plain HTTP: the link itself is unencrypted, so it must be a private network such as a tailnet, or a TLS reverse proxy must front the daemon."
        }
    }
    Step "Daemon answered at $DaemonUrl/health."
}
# <<< client-only preflight <<<
Step "Preflight..."
if ($ClientOnly) {
    Invoke-ClientOnlyPreflight
} else {
    # `&` on a .ps1 only refreshes $LASTEXITCODE when the script exits explicitly;
    # clear the stale value a prior native command may have left.
    $global:LASTEXITCODE = 0
    & (Join-Path $PSScriptRoot "preflight.ps1") -Client $clientList
    if ($LASTEXITCODE -ne 0) { throw "Preflight failed - fix the line(s) above and re-run." }
}

# -- 3. extractor choice (explicit, no default) ---------------------------------
# A client-only install has none: the remote daemon runs its own extractor.
if (-not $Extractor -and -not $ClientOnly) {
    if (-not $interactive) {
        throw "Non-interactive run: -Extractor $($extractorModes -join '|') is required."
    }
    Write-Host ""
    Write-Host "Which dream extractor should consolidate memories?"
    Write-Host "  1) claude-only       - lightest: Claude CLI shim only; sidecar never built (~11.8 GB lighter; needs logged-in Max-plan CLI; dreams pause when the shim is down)"
    Write-Host "  2) claude-fallback   - Claude CLI shim primary, sidecar auto-fallback (Max-plan CLI plus the ~11.8 GB image)"
    Write-Host "  3) sidecar           - bundled local CPU model (no Claude plan needed, works for everyone; ~11.8 GB image)"
    Write-Host "  4) openai-fallback   - Codex CLI (ChatGPT-plan) shim primary, sidecar auto-fallback (ladder-measured at parity with the Claude ceiling - see docs/guide/dreaming.md)"
    Write-Host "  5) openai-only       - Codex CLI shim only; sidecar never built (ladder-measured; dreams pause when the shim is down)"
    Write-Host "  6) endpoint          - any OpenAI-compatible server you name (LM Studio, Ollama, vLLM, a hosted API); sidecar never built"
    Write-Host "  7) endpoint-fallback - that server primary, sidecar auto-fallback (~11.8 GB image)"
    while (-not $Extractor) {
        switch (Read-Host "Choose 1-7") {
            "1" { $Extractor = "claude-only" }
            "2" { $Extractor = "claude-fallback" }
            "3" { $Extractor = "sidecar" }
            "4" { $Extractor = "openai-fallback" }
            "5" { $Extractor = "openai-only" }
            "6" { $Extractor = "endpoint" }
            "7" { $Extractor = "endpoint-fallback" }
            default { Write-Host "  please answer 1-7" }
        }
    }
    if ($Extractor -in "endpoint", "endpoint-fallback") {
        while (-not $ExtractorUrl) {
            $ExtractorUrl = (Read-Host "The server's OpenAI-compatible base URL (e.g. http://127.0.0.1:1234/v1)").TrimEnd("/")
        }
        while (-not $Model) { $Model = Read-Host "The model name that server serves" }
    }
    Assert-ExtractorArgs
}
if (-not $ClientOnly) { Step "Extractor mode: $Extractor" }
$claudeShimMode = $Extractor -in "claude-only", "claude-fallback"
$codexShimMode = $Extractor -in "openai-only", "openai-fallback"
if ($ShimPort -eq 0) { $ShimPort = $codexShimMode ? 8086 : 8082 }
# A model from the wrong family would silently serve the shim's launch
# default (the per-request override only honours its own prefixes).
if (($claudeShimMode -and $Model -and -not $Model.StartsWith("claude-")) -or
    ($codexShimMode -and $Model -and -not $Model.StartsWith("gpt-"))) {
    throw "-Model $Model does not match extractor mode $Extractor"
}
# Fail fast on a missing shim CLI: preflight only knows -Client, so e.g.
# -Extractor openai-fallback -Client claude would otherwise sail through and
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
# >>> endpoint preflight >>>
# An endpoint mode's server must answer before the stack is configured to
# depend on it. Any HTTP answer from <url>/models counts: a server may list
# no models, and a key it wants (PSEUDOLIFE_DREAM_API_KEY) is never taken on
# the command line, so a 401 is a warning, not a refusal.
function Invoke-EndpointPreflight {
    $answer = $null
    try {
        $answer = Invoke-WebRequest -Uri "$ExtractorUrl/models" -TimeoutSec 10 -MaximumRedirection 0 -SkipHttpErrorCheck
    } catch { $answer = $null }
    $code = if ($answer) { [int]$answer.StatusCode } else { 0 }
    if (($code -ge 200) -and ($code -lt 300)) {
        Step "Extractor endpoint answered at $ExtractorUrl/models."
    } elseif ($code -in 401, 403) {
        Write-Warning "$ExtractorUrl/models answered HTTP $($code): the server wants a key. Set PSEUDOLIFE_DREAM_API_KEY in ops/.env (outside the installer's managed block) before the first dream."
    } elseif ($code -ge 100) {
        Write-Warning "$ExtractorUrl/models answered HTTP $($code). The server is reachable; if dreams fail, check that the URL is its OpenAI-compatible base (usually ending in /v1)."
    } else {
        throw "The extractor endpoint did not answer at $ExtractorUrl/models. Start the server, or correct the URL (its OpenAI-compatible base, usually ending in /v1), then re-run."
    }
}
# <<< endpoint preflight <<<
if ($Extractor -in "endpoint", "endpoint-fallback") { Invoke-EndpointPreflight }

# -- 3b. dreamer model choice (Claude-shim modes only) ---------------------------
# Opus is the recommended family per the 2026-08-02 same-harness comparison
# (evals/results/dreamer-choice-verdict.json, run on claude-opus-5); Opus 5.5
# is the default since 2026-09-29, when it cleared the paired extraction-ladder
# gate with no regression against claude-opus-5
# (evals/results/ladder-opus55-paired-verdict-threshold.json). The shim honours per-request
# claude-* names, so this is only the launch default — switchable later from
# the Console's Extractor panel without a reinstall.
if ($claudeShimMode -and -not $Model) {
    if ($interactive) {
        Write-Host ""
        Write-Host "Which Claude model should extract memories (the 'dreamer')?"
        Write-Host "  1) claude-opus-5-5  - recommended: clears the extraction-ladder gate with no regression against claude-opus-5 (evals/results/ladder-opus55-paired-verdict-threshold.json, 2026-09-28); the 2026-08-02 judged comparison that established Opus as the best extractor ran on claude-opus-5"
        Write-Host "  2) claude-opus-5    - the earlier default: the 2026-08-02 judged comparison measured it as the best extractor (evals/results/dreamer-choice-verdict.json)"
        Write-Host "  3) claude-sonnet-5  - balanced"
        Write-Host "  4) claude-haiku-4-5 - fastest / lightest on plan usage"
        Write-Host "  5) claude-fable-5   - most capable tier"
        while (-not $Model) {
            switch (Read-Host "Choose 1/2/3/4/5 (Enter = 1)") {
                { $_ -in "", "1" } { $Model = "claude-opus-5-5" }
                "2" { $Model = "claude-opus-5" }
                "3" { $Model = "claude-sonnet-5" }
                "4" { $Model = "claude-haiku-4-5" }
                "5" { $Model = "claude-fable-5" }
                default { Write-Host "  please answer 1, 2, 3, 4 or 5" }
            }
        }
    } else {
        $Model = "claude-opus-5-5"
    }
    Step "Dreamer model: $Model"
}
# GPT menu: no 'recommended' — extraction quality is unmeasured for all of
# them (the ladder's terra rung exists to measure it); Terra is only the
# shim's balanced default. The GPT-6 ids were accepted by the Codex CLI on
# 2026-09-29; no ladder run has measured them.
if ($codexShimMode -and -not $Model) {
    if ($interactive) {
        Write-Host ""
        Write-Host "Which GPT model should extract memories (the 'dreamer')?"
        Write-Host "  1) gpt-5.6-terra - balanced default (extraction quality unmeasured)"
        Write-Host "  2) gpt-5.6-sol   - flagship (unmeasured)"
        Write-Host "  3) gpt-5.6-luna  - fastest / lightest on plan usage (unmeasured)"
        Write-Host "  4) gpt-6-sol     - GPT-6 flagship (unmeasured)"
        Write-Host "  5) gpt-6-luna    - GPT-6 fastest / lightest (unmeasured)"
        while (-not $Model) {
            switch (Read-Host "Choose 1/2/3/4/5 (Enter = 1)") {
                { $_ -in "", "1" } { $Model = "gpt-5.6-terra" }
                "2" { $Model = "gpt-5.6-sol" }
                "3" { $Model = "gpt-5.6-luna" }
                "4" { $Model = "gpt-6-sol" }
                "5" { $Model = "gpt-6-luna" }
                default { Write-Host "  please answer 1, 2, 3, 4 or 5" }
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
if (-not $ClientOnly) {
    docker volume create $bankVol | Out-Null
    docker volume create $stateVol | Out-Null
    Step "Volumes ready: $bankVol, $stateVol"
}

# -- 5. managed env block --------------------------------------------------------
# >>> extractor env >>>
# What the managed block of ops/.env says about the extractor. The daemon
# runs in Docker, where this host is host.docker.internal (extra_hosts in
# ops/docker-compose.yml), so a server named on loopback is written that way.
function ConvertTo-ExtractorContainerUrl([string]$url) {
    $scheme, $rest = $url -split '://', 2
    $authority = ($rest -split '/', 2)[0]
    $path = $rest.Substring($authority.Length)
    $hostPart = if ($authority.StartsWith("[")) {
        $authority.Substring(0, $authority.IndexOf("]") + 1)
    } else { ($authority -split ':', 2)[0] }
    $portPart = $authority.Substring($hostPart.Length)
    if (($hostPart -in "localhost", "[::1]") -or ($hostPart -like "127.*")) {
        $hostPart = "host.docker.internal"
    }
    return "${scheme}://$hostPart$portPart$path"
}
function Get-ExtractorEnvLines {
    switch ($Extractor) {
        "sidecar" { return @("# extractor: sidecar (stock defaults - nothing to set)") }
        { $_ -in "claude-fallback", "openai-fallback" } {
            return @("PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$ShimPort/v1",
                "PSEUDOLIFE_DREAM_MODEL=extractor",
                "PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1",
                "PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor",
                "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto")
        }
        { $_ -in "claude-only", "openai-only" } {
            # `primary` (not `auto`): states the single-extractor intent and
            # keeps the auto-without-fallback startup warning silent.
            return @("PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:$ShimPort/v1",
                "PSEUDOLIFE_DREAM_MODEL=extractor",
                "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary")
        }
        { $_ -in "endpoint", "endpoint-fallback" } {
            $lines = @("PSEUDOLIFE_DREAM_BASE_URL=$(ConvertTo-ExtractorContainerUrl $ExtractorUrl)",
                "PSEUDOLIFE_DREAM_MODEL=$Model")
            if ($Extractor -eq "endpoint-fallback") {
                $lines += @("PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1",
                    "PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor",
                    "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto")
            } else {
                $lines += "PSEUDOLIFE_DREAM_EXTRACTOR_MODE=primary"
            }
            return $lines
        }
    }
}
# <<< extractor env <<<
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
if (-not $ClientOnly) {
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
    foreach ($line in @(Get-ExtractorEnvLines)) { $block.Add($line) }
    $block.Add("PSEUDOLIFE_WRITER_ID=$writerId")
    $block.Add($EnvEnd)
    Set-Content -Path $envFile -Value (@($kept) + @($block)) -Encoding utf8
    Step "Wrote managed block in ops/.env"
    if ($Extractor -in "endpoint", "endpoint-fallback") {
        $endpointInContainer = ConvertTo-ExtractorContainerUrl $ExtractorUrl
        if ($endpointInContainer -ne $ExtractorUrl) {
            Step "Extractor endpoint written as $($endpointInContainer): the daemon runs in Docker, where this host is host.docker.internal."
        }
        Write-Host "    If the server wants a key, set PSEUDOLIFE_DREAM_API_KEY in ops/.env outside the managed block."
    }
}

# -- 5b. bearer token (the agent board needs one) --------------------------------
# A default install mints one, so the board is on; every client wired below
# then carries it in an owner-only token file. The value is never printed.
# Open loopback (docs/guide/configuration.md) stays available: -NoToken, and
# -Transport http, whose registrations cannot carry a token file; a host with
# no Python for the shim falls back to HTTP, so it mints none.
# >>> mint token >>>
# Whether stage 11 can be expected to install the shim: pipx, or a Python
# >= 3.10 with pip outside a PEP 668 externally managed environment (where
# `pip install --user` refuses). The helper needs that Python either way.
function Test-ShimToolingReady($python) {
    if (-not $python) { return $false }
    if (Get-Command pipx -ErrorAction SilentlyContinue) { return $true }
    try {
        & $python -c "import os, sys, sysconfig, pip; marker = os.path.join(sysconfig.get_path('stdlib'), 'EXTERNALLY-MANAGED'); sys.exit(1 if sys.prefix == sys.base_prefix and os.path.exists(marker) else 0)" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}
$tokenState = $null
$tokenPython = $null
# A client-only install never mints: the remote daemon's token is the file
# the operator supplied. A token already set gates the daemon whatever this
# run may mint, and the HTTP-registration warning and the board hint depend
# on knowing that.
if ($ClientOnly) {
    $tokenState = "remote"
} elseif ($env:PSEUDOLIFE_MCP_TOKEN -or $env:PSEUDOLIFE_MCP_TOKENS -or
        (Get-EnvValue "PSEUDOLIFE_MCP_TOKEN") -or (Get-EnvValue "PSEUDOLIFE_MCP_TOKENS")) {
    $tokenState = "present"
} elseif ($NoToken) {
    $tokenState = "opted-out"
} elseif ($Transport -ne "shim") {
    $tokenState = "http"
} elseif (-not (Test-ShimToolingReady ($tokenPython = Get-InstallerPython))) {
    $tokenState = "no-shim"
} else {
    $mintOutput = & $tokenPython (Join-Path $repo "ops/client_credentials.py") mint --env-file $envFile
    switch ("$(Get-HelperStatus $mintOutput)") {
        "minted" {
            $tokenState = "minted"
            Step "Minted a bearer token in ops/.env (owner-only, never printed) - the agent board needs one."
        }
        "present" { $tokenState = "present" }
        default {
            $tokenState = "failed"
            Write-Warning "Could not mint a bearer token in ops/.env, so the agent board stays off. Set PSEUDOLIFE_MCP_TOKEN there by hand and re-run."
        }
    }
}
# <<< mint token <<<

# -- 6. sidecar enable/disable via the compose override --------------------------
function InstallerOwnsOverride {
    (Test-Path $overrideFile) -and
        ((Get-Content $overrideFile -TotalCount 1) -in $OverrideMarker, $LegacyOverrideMarker, $LegacyShimOverrideMarker)
}
if ($ClientOnly) {
    # No local daemon here, so its compose override is not this run's to change.
} elseif (Test-ExtractorDisablesSidecar) {
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
if (-not $ClientOnly) {
    $compose = @("--env-file", $envFile, "-f", $composeFile)
    if (Test-Path $overrideFile) { $compose += @("-f", $overrideFile) }
    Step "docker compose up -d --build (first build downloads images - grab a coffee)..."
    docker compose @compose up -d --build
    if ($LASTEXITCODE -ne 0) { throw "compose up failed" }
}

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
# A client-only install leaves any local daemon's extractor shims alone.
if (-not $codexShimMode -and -not $ClientOnly) { Remove-ShimTask "Pseudolife Codex Shim" }
if (-not $claudeShimMode -and -not $ClientOnly) {
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
            # A client-only install: the remote daemon's token, from the
            # operator's file on stdin, and its URL.
            $installerToken = if ($ClientOnly) {
                (Get-Content -LiteralPath $TokenFile -Raw).Trim()
            } else { Get-EnvValue "PSEUDOLIFE_MCP_TOKEN" }
            $installerUrl = if ($DaemonUrl) { $DaemonUrl } else { Get-EnvValue "PSEUDOLIFE_MCP_DAEMON_URL" }
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
            # An existing Codex registration keeps its own daemon URL.
            if ($ClientOnly -and -not $codexCredentialBootstrapFailed -and
                ("$codexCredentialUrl".TrimEnd("/") -ne $DaemonUrl)) {
                $codexCredentialBootstrapFailed = $true
                $codexSetup.recovery = "The existing Codex registration names the daemon at $(if ($codexCredentialUrl) { $codexCredentialUrl } else { '(none)' }), not the daemon at $DaemonUrl. Edit it in place in the Codex config.toml: set PSEUDOLIFE_MCP_DAEMON_URL=$DaemonUrl in [mcp_servers.pseudolife-memory.env], then re-run."
                [Environment]::SetEnvironmentVariable("PSEUDOLIFE_MCP_TOKEN", $savedCredentialToken, "Process")
                [Environment]::SetEnvironmentVariable("PSEUDOLIFE_MCP_TOKEN_FILE", $savedCredentialFile, "Process")
                [Environment]::SetEnvironmentVariable("PSEUDOLIFE_MCP_DAEMON_URL", $savedCredentialUrl, "Process")
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
    if ($ClientOnly) {
        # No daemon container here: the hook runs the installed shim by its
        # path, known once section 11 installed it (after section 12's check).
        $hookState["claude"] = "deferred"
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
$script:shimUpgradeHeld = $null
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
# On Windows neither pipx nor pip can replace a shim that sessions are running,
# and neither puts back what it removed first. pip 24.0 stashes an uninstall in
# sorted order, so site-packages is already renamed to `~` siblings when the
# running launcher raises WinError 32, from uninstall(), outside the try that
# rolls back. `pipx install --force` deletes the venv with
# rmtree(ignore_errors=True), taking everything that is not locked. On
# 2026-09-25 this left a runtime without its own package. So an installed shim
# that a session is running is left in place, still usable, and the rerun named.
function Get-ShimProcessTable {
    # $null off Windows, where an open or running file does not stop pip or pipx.
    if (-not $IsWindows) { return $null }
    $rows = @(Get-CimInstance -ClassName Win32_Process -Property ProcessId, ParentProcessId, ExecutablePath -ErrorAction Stop)
    if (-not $rows) { throw "it came back empty" }
    return $rows
}
function Get-ShimHeldPaths($Manager) {
    # What a running session of the installed shim executes from: the whole
    # pipx venv (its launcher and the venv's python.exe redirector both run
    # there) and the installed launcher. A --user scripts directory holds other
    # tools' launchers too, so only the shim's own launcher counts there.
    $paths = @()
    if ($Manager.Cmd -eq "pipx") {
        $pipxHome = (& pipx environment --value PIPX_HOME 2>$null | Select-Object -Last 1)
        if ($pipxHome) {
            $venv = Join-Path (Join-Path "$pipxHome" "venvs") "pseudolife-mcp"
            if (Test-Path -LiteralPath $venv -PathType Container) { $paths += $venv }
        }
    }
    $launcher = Resolve-InstalledShimPath $Manager
    if ($launcher) { $paths += $launcher }
    return $paths
}
function Test-ShimUpgradeHeld($Manager) {
    # $true when the installed shim must not be upgraded now; the reason is
    # kept in $script:shimUpgradeHeld for the registration steps and the end.
    $tableError = $null
    $rows = $null
    try { $rows = Get-ShimProcessTable } catch { $tableError = $_.Exception.Message }
    if (($null -eq $tableError) -and ($null -eq $rows)) { return $false }
    $held = @(Get-ShimHeldPaths $Manager | Where-Object { $_ } | ForEach-Object {
        [IO.Path]::GetFullPath("$_").TrimEnd([IO.Path]::DirectorySeparatorChar)
    })
    # Nothing installed yet: nothing a session could be running.
    if (-not $held) { return $false }
    $rerun = "& `"$([IO.Path]::Combine($repo, 'ops', 'install.ps1'))`" (with the options you used)"
    if ($null -ne $tableError) {
        $script:shimUpgradeHeld = "Could not read the Windows process table ($tableError) to see whether a session is running the pseudolife-mcp shim from $($held -join ', '); it was left as it is rather than upgraded. Close every Claude Code / Codex session using it, then rerun: $rerun"
        Write-Warning $script:shimUpgradeHeld
        return $true
    }
    $running = @{}
    foreach ($row in $rows) {
        if (-not $row.ExecutablePath) { continue }
        $image = [IO.Path]::GetFullPath("$($row.ExecutablePath)")
        foreach ($root in $held) {
            if ($image.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
                $image.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                $running[[long]$row.ProcessId] = [long]$row.ParentProcessId
                break
            }
        }
    }
    if ($running.Count -eq 0) { return $false }
    # A Claude session is the launcher plus the venv redirector it starts, a
    # Codex one the redirector alone: each process tree counts once.
    $sessions = @($running.Values | Where-Object { -not $running.ContainsKey($_) }).Count
    $who = if ($sessions -eq 1) { "1 session is" } else { "$sessions sessions are" }
    $what = if ($running.Count -eq 1) { "1 process" } else { "$($running.Count) processes" }
    $tool = if ($Manager.Cmd -eq "pipx") { "pipx" } else { "pip" }
    $script:shimUpgradeHeld = "$who running the pseudolife-mcp shim from $($held -join ', ') ($what); it was not upgraded, because on Windows $tool cannot replace a running shim and would leave it half-removed. The installed shim stays in place and registered. Close every Claude Code / Codex session using it, then rerun: $rerun"
    Write-Warning $script:shimUpgradeHeld
    return $true
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
        $pipxManager = @{ Cmd = "pipx"; Args = @() }
        if (Test-ShimUpgradeHeld $pipxManager) {
            $shimManager = $pipxManager
        } else {
            # --force also replaces an existing environment when the checkout's
            # version matches the installed one, so a stale same-version PyPI shim
            # cannot survive an installer rerun.
            pipx install --force $repo 2>&1 | Out-Host
            if ($LASTEXITCODE -eq 0) {
                $shimManager = $pipxManager
            } else {
                Write-Warning "pipx install --force from the checkout failed (exit $LASTEXITCODE)."
            }
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
            if (Test-ShimUpgradeHeld $candidate) {
                $shimManager = @{ Cmd = $exe; Args = $exeArgs }
                break
            }
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
# Board credentials for the Claude Code and Gemini shims: an owner-only token
# file per client holding its principal's own PSEUDOLIFE_MCP_TOKENS entry,
# else the singular token; the shim re-reads it per call. Claude Code also
# gets a private directory for its board address, keyed by session id so
# `claude --resume <id>` keeps it. The plugin's hooks read the Claude Code
# process environment, not the registration, so they get the file through
# settings.json unless the user's own environment supplies a credential.
# >>> client token files >>>
$clientDaemonUrl = if ($DaemonUrl) { $DaemonUrl } else { Get-EnvValue "PSEUDOLIFE_MCP_DAEMON_URL" }
if (-not $clientDaemonUrl) { $clientDaemonUrl = "http://127.0.0.1:8765" }
$claudeAgentStateDir = Join-Path $env:USERPROFILE ".pseudolife-mcp\claude-code-agents"
$claudeConfigHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { $env:USERPROFILE }
$claudeJson = Join-Path $claudeConfigHome ".claude.json"
function Get-ClientTokenFile([string]$principal) {
    # The token file it wrote or kept, or $null.
    if ($Transport -ne "shim") { return $null }
    $clientPython = Get-InstallerPython
    if (-not $clientPython) { return $null }
    $target = Join-Path $env:USERPROFILE ".pseudolife-mcp\$principal.token"
    # The values ride process-scoped env vars the helper reads by NAME -
    # never a command-line argument, never printed.
    $env:PSEUDOLIFE_INSTALLER_TOKEN = Get-DesktopTokenSource
    $env:PSEUDOLIFE_INSTALLER_TOKENS = Get-DesktopTokensSource
    try {
        $clientOutput = & $clientPython (Join-Path $repo "ops/client_credentials.py") token-file --principal $principal --path $target
    } finally {
        $env:PSEUDOLIFE_INSTALLER_TOKEN = $null
        $env:PSEUDOLIFE_INSTALLER_TOKENS = $null
    }
    switch ("$(Get-HelperStatus $clientOutput)") {
        "ready" { return $target }
        "tokenless" { return $null }
        default {
            Write-Warning "Could not write the $principal token file ($($clientOutput -join ' ')); its registration gets no board credential."
            return $null
        }
    }
}
# A remote daemon's token stays in the file the operator supplied.
$claudeTokenFile = if ($clients -notcontains "claude") { $null } elseif ($ClientOnly) { $TokenFile } else { Get-ClientTokenFile "claude-code" }
$geminiTokenFile = if ($clients -notcontains "gemini") { $null } elseif ($ClientOnly) { $TokenFile } else { Get-ClientTokenFile "gemini" }
# A client-only install's settings.json hooks run the host shim, which needs
# the file and the URL there as much as the plugin's hooks do.
if ((($hookState["claude"] -eq "plugin") -or $ClientOnly) -and $claudeTokenFile) {
    $claudeSettingsHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE ".claude" }
    $settingsArgs = @((Join-Path $repo "ops/client_credentials.py"), "claude-settings-env",
        "--settings", (Join-Path $claudeSettingsHome "settings.json"),
        "--token-file", $claudeTokenFile, "--daemon-url", $clientDaemonUrl)
    # A client-only install's hooks must reach the remote daemon with the
    # operator's file, whatever an earlier local install left there.
    if ($ClientOnly) { $settingsArgs += "--replace" }
    $settingsOutput = & (Get-InstallerPython) @settingsArgs
    $settingsReport = try { ($settingsOutput -join "`n") | ConvertFrom-Json } catch { $null }
    switch ("$($settingsReport.status)") {
        "updated" {
            if ($ClientOnly) {
                $settingsBackup = if ($settingsReport.backup) { " (backup: $($settingsReport.backup))" } else { "" }
                Step "Claude Code hooks: set $(@($settingsReport.changed) -join ', ') in ~/.claude/settings.json (env) for the daemon at $clientDaemonUrl$settingsBackup."
            } else {
                Step "Claude Code plugin hook settings: PSEUDOLIFE_MCP_TOKEN_FILE saved in ~/.claude/settings.json (env); hook authentication not verified."
            }
        }
        { $_ -in "kept", "unchanged" } { }
        default { Write-Warning "Could not give the Claude Code hooks the token file ($($settingsOutput -join ' ')). Set PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile and PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl in the env block of ~/.claude/settings.json." }
    }
}
# <<< client token files <<<
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
        if ($ClientOnly) {
            # The operator's file as it is: a remote daemon's token is never copied.
            $desktopArgs += @("--daemon-url", $clientDaemonUrl, "--token-file", $TokenFile)
        } elseif ($desktopTokenFile) {
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
        if ($ClientOnly) {
            Write-Host "  This daemon is remote: use the stdio shape, with"
            Write-Host "  `"PSEUDOLIFE_MCP_DAEMON_URL`": `"$clientDaemonUrl`" and"
            Write-Host "  `"PSEUDOLIFE_MCP_TOKEN_FILE`": `"$($TokenFile.Replace('\', '\\'))`" added to its env"
            Write-Host "  (the HTTP shape cannot carry the token)."
            Write-Host ""
        }
        continue
    }
    if ($selectedClient -eq "codex") {
        if ($codexCredentialBootstrapFailed) {
            $mcpState["codex"] = "failed"
            Write-Warning "Codex MCP registration was skipped because credential setup failed. Rerun the installer after repairing the reported credential problem."
            continue
        }
        if ($ClientOnly -and -not $codexCredentialFile) {
            # Without it Codex would reach whatever daemon its defaults name.
            $mcpState["codex"] = "failed"
            Write-Warning "Codex was not registered: a client-only install points it at $clientDaemonUrl through the Codex credential setup, which did not run (it needs Python 3.10 or newer; see above). Fix that, then re-run."
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
                        if ($script:shimUpgradeHeld) {
                            Write-Warning "The existing Codex registration was preserved, but its pseudolife-mcp shim was not upgraded - see the warning above."
                            $mcpState["codex"] = "failed"
                        } elseif (-not $bareRegisteredShim) {
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
            if ($codexCredentialFile -or $ClientOnly) {
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
                        if ($script:shimUpgradeHeld) {
                            Write-Warning "The existing Gemini CLI registration was preserved, but its pseudolife-mcp shim was not upgraded - see the warning above."
                            $mcpState["gemini"] = "failed"
                        } elseif (-not $bareRegisteredShim) {
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
            # `gemini mcp list` shows no env, so the installer cannot tell
            # whether this registration carries a token; say how to add one.
            if ($geminiTokenFile) {
                Write-Warning "The daemon requires a bearer token; unless the existing Gemini CLI registration already carries one, its memory calls will be refused. Edit it in place in ~/.gemini/settings.json and set PSEUDOLIFE_MCP_TOKEN_FILE=$geminiTokenFile (an HTTP registration cannot carry it: register the stdio shim instead), or re-run with -NoToken."
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
                if ($geminiTokenFile) {
                    gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 -e "PSEUDOLIFE_MCP_TOKEN_FILE=$geminiTokenFile" -e "PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl" pseudolife-memory $script:shimInstallPath
                } else {
                    gemini mcp add -s user -e PSEUDOLIFE_WRITER_ID=gemini -e PSEUDOLIFE_MCP_NO_SPAWN=1 pseudolife-memory $script:shimInstallPath
                }
                Register-Result "gemini" "shim-env" "Wired into Gemini CLI via the pseudolife-mcp shim - per-session identity."
            } else {
                Write-Warning "This Gemini CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed."
                $mcpState["gemini"] = "failed"
            }
        } elseif ($ClientOnly) {
            Write-Warning "Shim unavailable for Gemini CLI (see warnings above), and a remote daemon needs it: an HTTP registration cannot carry the token file. Install pipx or Python >=3.10 and re-run."
            $mcpState["gemini"] = "failed"
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
                        if ($script:shimUpgradeHeld) {
                            Write-Warning "The existing Claude Code registration was preserved, but its pseudolife-mcp shim was not upgraded - see the warning above."
                            $mcpState["claude"] = "failed"
                        } elseif (-not $bareRegisteredShim) {
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
            # An install from before the board token: the upgraded shim reads
            # a token file, so add it, the daemon URL and the state directory
            # to the registration in place (never a remove and re-add),
            # keeping any credential it already has. Anything else gets the
            # manual fix.
            $claudeCredentialOk = $false
            if (($mcpState["claude"] -eq "present-upgraded") -and $claudeTokenFile) {
                $claudeEnvOutput = & (Get-InstallerPython) (Join-Path $repo "ops/client_credentials.py") registration-env --config $claudeJson --set "PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile" --set "PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl" --set "PSEUDOLIFE_AGENT_STATE_DIR=$claudeAgentStateDir"
                switch ("$(Get-HelperStatus $claudeEnvOutput)") {
                    "updated" {
                        $claudeCredentialOk = $true
                        Step "Claude Code registration: added the board token file, daemon URL and state directory in place. Restart Claude Code sessions to load them."
                    }
                    "unchanged" { $claudeCredentialOk = $true }
                }
            }
            if ((-not $claudeCredentialOk) -and $claudeTokenFile -and ($existingClaude -notmatch 'PSEUDOLIFE_MCP_TOKEN')) {
                Write-Warning "The daemon requires a bearer token, but the existing Claude Code registration carries none, so its memory calls will be refused. Edit the registration in place and set PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile (an HTTP registration cannot carry it: register the stdio shim instead), or re-run with -NoToken."
            }
            # The in-place edit above only adds what is missing, so a
            # registration from an earlier local install may still name that
            # daemon.
            if ($ClientOnly -and -not ([string](claude mcp get pseudolife-memory 2>$null | Out-String)).Contains($clientDaemonUrl)) {
                Write-Warning "The existing Claude Code registration does not name the daemon at $clientDaemonUrl. Edit it in place and set PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl and PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile."
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
                    # With a token, the board credential rides along: the
                    # token file (re-read per call), the daemon URL, and the
                    # state directory that keeps a resumed session's board
                    # address.
                    if ($claudeTokenFile) {
                        claude mcp add --scope user pseudolife-memory $envFlag PSEUDOLIFE_WRITER_ID=claude-code PSEUDOLIFE_MCP_NO_SPAWN=1 "PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile" "PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl" "PSEUDOLIFE_AGENT_STATE_DIR=$claudeAgentStateDir" -- $script:shimInstallPath
                    } else {
                        claude mcp add --scope user pseudolife-memory $envFlag PSEUDOLIFE_WRITER_ID=claude-code PSEUDOLIFE_MCP_NO_SPAWN=1 -- $script:shimInstallPath
                    }
                    Register-Result "claude" "shim-env" "Wired into Claude Code via the pseudolife-mcp shim - per-session identity (required for correct episodes with concurrent sessions)."
                } else {
                    Write-Warning "This Claude CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed."
                    $mcpState["claude"] = "failed"
                }
            } elseif ($ClientOnly) {
                Write-Warning "Could not install the pseudolife-mcp shim (see warnings above), and a remote daemon needs it: an HTTP registration cannot carry the token file. Install pipx or Python >=3.10 and re-run."
                $mcpState["claude"] = "failed"
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
# A client-only install checked the remote daemon at preflight, and nothing
# starts locally, so there is nothing to wait for.
if (-not $ClientOnly) {
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
}

# >>> client-only claude hook >>>
# A client-only install's Claude Code session hook (deferred from section
# 9): the installed shim by its path, since a hook's PATH need not include
# pipx's directory. It reads the daemon's address and the token file from
# the settings.json env block section 11 wrote. Forward slashes and no .exe:
# the hook runs under Claude Code's shell.
if ($hookState["claude"] -eq "deferred") {
    if ((Install-ShimOnce) -and $script:shimInstallPath) {
        $shimCommand = ($script:shimInstallPath -replace '\.exe$', '').Replace('\', '/')
        if ($shimCommand -match '\s') {
            Write-Warning "The shim's path holds a space, so the session hook runs pseudolife-mcp from PATH instead: $shimCommand"
            $shimCommand = "pseudolife-mcp"
        }
        Step "Installing claude session hook..."
        & (Join-Path $PSScriptRoot "install-hook.ps1") -Client claude -Command "$shimCommand briefing --hook-json"
        $hookState["claude"] = "hook"
    } else {
        Write-Warning "The Claude Code session hook was not installed: it runs the pseudolife-mcp shim, which is unavailable (see above)."
        $hookState["claude"] = "failed"
    }
}
# <<< client-only claude hook <<<

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
if ($ClientOnly) {
    Write-Host ""
    Write-Host "  Daemon: remote at $clientDaemonUrl - this machine runs none (client-only install)"
}
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
            } elseif ($hookState["claude"] -eq "failed") {
                Write-Host "    [!] Session briefing     not installed - the shim is unavailable"
                Write-Host "    [!] Per-turn discipline  not installed"
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
# One line for the agent board, as the daemon answers it for the first client
# token file (else the daemon's singular token): on, or off and why.
# >>> board line >>>
$boardHint = switch ($tokenState) {
    "opted-out" { " (installed with -NoToken; re-run without it to turn the board on)" }
    "http" { " (-Transport http registrations cannot carry a token file)" }
    "no-shim" { " (no pipx or pip-capable Python >= 3.10 to install the shim, so no token was minted; install one and re-run)" }
    default { "" }
}
$boardFile = @($claudeTokenFile, $geminiTokenFile, $codexCredentialFile, $TokenFile) | Where-Object { $_ } | Select-Object -First 1
$boardPython = Get-InstallerPython
if (-not $boardPython) {
    $boardLine = "unknown - no Python >= 3.10 to ask the daemon"
} else {
    $boardArgs = @((Join-Path $repo "ops/client_credentials.py"), "board", "--daemon-url", $clientDaemonUrl)
    if ($boardFile) {
        $boardArgs += @("--token-file", $boardFile)
    } else {
        # The singular token rides a process-scoped env var, never an argument.
        $env:PSEUDOLIFE_INSTALLER_TOKEN = Get-DesktopTokenSource
    }
    try {
        $boardLine = (& $boardPython @boardArgs) -join " "
        if (($LASTEXITCODE -ne 0) -or -not $boardLine) { $boardLine = "unknown" }
    } catch {
        $boardLine = "unknown"
    } finally {
        $env:PSEUDOLIFE_INSTALLER_TOKEN = $null
    }
}
if ($boardLine.StartsWith("on")) {
    Write-Host "  [x] Agent board          $boardLine"
} else {
    Write-Host "  [!] Agent board          $boardLine$boardHint"
}
if (($tokenState -in "minted", "present") -and
    (@($mcpState["claude"], $mcpState["gemini"], $mcpState["codex"]) -contains "http")) {
    Write-Warning "An HTTP registration sends no bearer token, and this daemon requires one, so its memory calls will be refused. Register the stdio shim (fix its install and re-run without -Transport http), or remove the token from ops/.env for an open-loopback install."
}
Write-Host ""
# <<< board line <<<
switch ($Extractor) {
    "sidecar" {
        Write-Host "Verify: memory_dream(action=""status"") - primary_url should point at pseudolife-extractor:8081."
    }
    { $_ -in "claude-fallback", "openai-fallback" } {
        Write-Host "Verify: memory_dream(action=""status"") - fallback_url set and primary_healthy: true (shim up)."
    }
    { $_ -in "claude-only", "openai-only" } {
        Write-Host "Verify: memory_dream(action=""status"") - primary_url on :$ShimPort, extractor_mode: primary."
        Write-Host "Note: dreams pause (and retry next sweep) whenever the shim is down or the CLI is logged out."
    }
    "endpoint-fallback" {
        Write-Host "Verify: memory_dream(action=""status"") - primary_url on your endpoint, fallback_url set, primary_healthy: true."
    }
    "endpoint" {
        Write-Host "Verify: memory_dream(action=""status"") - primary_url on your endpoint, extractor_mode: primary."
        Write-Host "Note: dreams pause (and retry next sweep) whenever the endpoint is down."
    }
}
if ($codexShimMode) {
    Write-Host "Note: Codex-served extraction quality is unmeasured - see the 'OpenAI primary' section of docs/guide/dreaming.md."
}
if ($ClientOnly) { Show-ClientOnlyNotes; Write-Host "" }
if ($script:shimUpgradeHeld) { Write-Warning $script:shimUpgradeHeld }
Write-Host "Done. First session: tell your coding agent to remember something."
