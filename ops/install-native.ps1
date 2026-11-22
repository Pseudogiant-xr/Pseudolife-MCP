#Requires -Version 7
# ^ Windows PowerShell 5.1 (powershell.exe) writes UTF-8 WITH a BOM, which
#   garbles the first key of ops/.env and can break settings.json parsing —
#   run this under pwsh 7+ (winget install Microsoft.PowerShell).
# The native client's installer: ops\install.ps1 runs it, with the same
# parameters, when PSEUDOLIFE_SHIM_ENGINE=native asks for the native client.
# Prompt, rerun and refusal behaviour follow docs/dev/install-ux.md; read it
# before changing what this script asks or prints.
# One-shot idempotent installer for the Pseudolife-MCP stack (issue #13
# tier 2). Everything downstream of Docker: provider selection -> preflight ->
# extractor choice -> compose up -> client hooks -> standing instructions ->
# MCP registration -> health. Re-running is safe; re-running with a different
# -Extractor is the supported way to switch modes.
# Exit statuses: 0 completed, 1 operational failure, 2 usage or prerequisite refusal, 5 requested client or extractor setup incomplete.
#
#   ops\install.ps1                                    # interactive
#   ops\install.ps1 -Extractor sidecar -Client codex   # non-interactive
#   ops\install.ps1 -Extractor claude-only -Client claude,gemini
#   ops\install.ps1 -Extractor claude-fallback -Instructions append
#   ops\install.ps1 -Extractor openai-fallback -Client codex
#   ops\install.ps1 -Extractor endpoint -ExtractorUrl http://127.0.0.1:1234/v1 `
#       -Model qwen3.6-27b -Client claude
#   ops\install.ps1 -DaemonUrl http://100.64.0.2:8765 `
#       -PairingCode ABCD-EFGH-JKMN -Client claude
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
# -Yes: no prompts; take documented defaults, not broad consent.
# PSEUDOLIFE_UNATTENDED=1 is the explicit agent-under-pty signal; redirected
# stdin also enables it. Omitted -Client detects installed CLIs (else generic);
# omitted -Extractor uses sidecar, the local CPU default.
# -NoToken (switch): open-loopback install - mint no bearer token, so the
#   agent board stays off.
# -Engine rust|python: the Docker daemon. rust pulls the release's published
#   Rust image; a rerun keeps the engine installed (default for a new
#   install: rust, or python without a bearer or a published image); naming the other engine switches it through
#   pseudolife-mcp update.
# -TestLogin (switch): also give the test suite its own Postgres login, which
#   cannot open the bank (`pseudolife-mcp test-login create`): for
#   contributors who run the suite against this server; off by default.
#
# Client-only install (this machine runs no daemon; one runs elsewhere,
# typically reached over a tailnet): no Docker, volumes, ops/.env, token
# minting or local daemon. Shim, registrations, plugin, hooks and standing
# instructions are set up as usual, aimed at the remote daemon. An
# interactive run that names no daemon asks first where the bank lives:
# answering "another machine" is this mode, with the URL and token asked for.
# -DaemonUrl <url>: the daemon's URL (default: the PSEUDOLIFE_MCP_DAEMON_URL
#   environment variable). A host other than 127.0.0.1, localhost or ::1
#   implies -ClientOnly.
# -TokenFile <path>: client-only - an owner-only file holding the daemon's
#   bearer token (default: PSEUDOLIFE_MCP_TOKEN_FILE); the installer never
#   mints one here. The fallback: prefer -PairingCode.
# -ClientOnly (switch): wire clients to -DaemonUrl only; with a loopback URL,
#   for an SSH tunnel.
# -ReadToken (switch): client-only - create the -TokenFile (it must not exist
#   yet) from the token typed, unechoed, or piped on stdin (the fallback:
#   prefer -PairingCode).
# -PairingCode <code>: client-only, recommended - a pairing code from
#   pseudolife-mcp invite <name> on the daemon's host, instead of a token, so
#   this machine gets its own principal. If two machines use one token,
#   rotating it breaks both and neither can be revoked alone. The installed
#   shim's pair creates the
#   -TokenFile (default ~/.pseudolife-mcp/<client>.token;
#   it must not exist yet). An interactive run also takes a code pasted
#   where it asks for the token, which keeps it off the command line (and
#   the shell history).
# -ClientUpdateSchedule ask|yes|no|HH:MM: client-only, Windows - keep this
#   machine's clients on the daemon's release with a daily scheduled task
#   (pseudolife-mcp update --schedule): ask (default) prompts once, default
#   no, and unattended runs skip it and print the command; yes sets it up at
#   04:30, HH:MM at that time; no skips it. A task already in place is left
#   alone.
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
    [switch]$Yes,
    [switch]$NoArt,
    # Open-loopback install: mint no bearer token, so the agent board stays off.
    [switch]$NoToken,
    # Client-only install against a daemon running elsewhere (see the header).
    [string]$DaemonUrl = "",
    [string]$TokenFile = "",
    [switch]$ClientOnly,
    [switch]$ReadToken,
    [string]$PairingCode = "",
    # Client-only: the daily client update task (see the header).
    [string]$ClientUpdateSchedule = "ask",
    # Guided Secure MCP Tunnel setup needs the Python client.
    [switch]$Tunnel,
    # Also give the test suite its own Postgres login (see the header).
    [switch]$TestLogin,
    # The Docker daemon engine (see the header).
    [ValidateSet("", "rust", "python")]
    [string]$Engine = ""
)
if ($Tunnel) {
    [Console]::Error.WriteLine('The native client has no guided tunnel setup yet. Nothing was changed. Rerun without -Tunnel.')
    [Console]::Error.WriteLine('After install, prepare a native tunnel profile with pseudolife-mcp tunnel setup --daemon-url "<daemon-url>" --token-file "<owner-only token file>" --accept-access.')
    [Console]::Error.WriteLine('Save its ID with pseudolife-mcp tunnel setup --tunnel-id "<tunnel-id>", import the key with pseudolife-mcp tunnel setup --read-key, then run pseudolife-mcp tunnel run in a terminal you keep open. See docs/guide/tunnels.md.')
    [Console]::Error.WriteLine('For guided setup, run Remove-Item Env:PSEUDOLIFE_SHIM_ENGINE and rerun .\ops\install.ps1 -Tunnel with your other install options.')
    exit 1
}
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
# >>> unattended mode >>>
$interactive = ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) -and -not $Yes -and $env:PSEUDOLIFE_UNATTENDED -ne '1'
if (-not $interactive -or $script:installerRerun) {
    Write-Host "Unattended install: no prompts; documented defaults apply (explicit flags override)."
}
# <<< unattended mode <<<
Write-Host "Native client install (PSEUDOLIFE_SHIM_ENGINE=native): rerun it with that variable set; without it, ops\install.ps1 installs the Python client."
# >>> client update schedule flag >>>
if ("$ClientUpdateSchedule" -notmatch '^(ask|yes|no|([01]?[0-9]|2[0-3]):[0-5][0-9])$') {
    Write-Host "invalid -ClientUpdateSchedule '$ClientUpdateSchedule' (ask|yes|no, or a time of day as HH:MM)"
    exit 2
}
# <<< client update schedule flag <<<

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
$claudeModels = @("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
    "claude-sonnet-5", "claude-haiku-5-5", "claude-haiku-4-5", "claude-fable-5-1",
    "claude-fable-5")
$openaiModels = @("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-6-sol",
    "gpt-6-luna", "gpt-6.1-sol")
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
# What may reach ops/.env (compose expands $ and reads " #" as a comment),
# a systemd ExecStart line and a Windows task's command line unquoted: model
# ids and endpoint URLs are held to these characters. Brackets are allowed
# in a URL only, for an IPv6 host.
$safeIdPattern = '^[A-Za-z0-9._:/@+-]+$'
$safeUrlPattern = '^[\[\]A-Za-z0-9._:/@+-]+$'
function Assert-ExtractorArgs {
    # The mode, its model and its URL, together.
    if (Test-ModelBlank) {
        Write-Host "-Model needs a model id (it was empty)"
        exit 2
    }
    if ($Model -and ($Model -cnotmatch $safeIdPattern)) {
        Write-Host "-Model '$Model' holds characters the installer cannot write safely; use letters, digits and . _ : / @ + -"
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
        # Checked before any other message, which never echoes the URL.
        $urlAuthority = (($ExtractorUrl -split '://', 2)[-1] -split '/', 2)[0]
        if ($urlAuthority.Contains("@")) {
            Write-Host "-ExtractorUrl must not carry credentials (user:password@host); a key the server wants goes in PSEUDOLIFE_DREAM_API_KEY in ops/.env"
            exit 2
        }
        if ($ExtractorUrl -cnotmatch '^https?://.') {
            Write-Host "invalid -ExtractorUrl: expected the server's http(s) base URL, e.g. http://127.0.0.1:1234/v1"
            exit 2
        }
        if ($ExtractorUrl -cnotmatch $safeUrlPattern) {
            Write-Host "invalid -ExtractorUrl: it holds characters the installer cannot write safely; use letters, digits and . _ : / @ + - (and [ ] around an IPv6 host)"
            exit 2
        }
    } else {
        if ($ExtractorUrl) {
            Write-Host "-ExtractorUrl applies only to -Extractor endpoint or endpoint-fallback"
            exit 2
        }
        # One family rule, the same in install.sh, before any note: a shim
        # only honours its own family's ids per request.
        $familyOk = switch -Regex ($Extractor) {
            '^sidecar$' { $true }
            '^claude-' { $Model -clike "claude-?*" }
            '^openai-' { ($Model -clike "gpt-?*") -or ($Model -clike "codex-?*") }
            default { $true }
        }
        if ($Model -and -not $familyOk) {
            Write-Host "-Model '$Model' does not match extractor mode $($Extractor): a claude-* mode takes a claude-* id, an openai-* mode a gpt-* or codex-* id"
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

# >>> native client >>>
# How long the staged client may take to answer `version` and
# `installer --capability` (seconds); PSEUDOLIFE_NATIVE_PROBE_SECONDS overrides it.
# The setting is read as a decimal whole number; anything else is named and
# the probe keeps 30.
$NativeProbeSeconds = 30
$nativeProbeSetting = "$env:PSEUDOLIFE_NATIVE_PROBE_SECONDS"
if ($nativeProbeSetting -ne "") {
    $nativeProbeDigits = $nativeProbeSetting.TrimStart([char]'0')
    if (($nativeProbeSetting -cmatch '^[0-9]+$') -and ($nativeProbeDigits -cmatch '^[1-9][0-9]{0,4}$')) {
        $NativeProbeSeconds = [int]$nativeProbeDigits
    } else {
        [Console]::Error.WriteLine("PSEUDOLIFE_NATIVE_PROBE_SECONDS='$nativeProbeSetting' is not a whole number of seconds from 1 to 99999; the probe waits 30 s.")
    }
}
function Invoke-NativeBounded([string]$Exe, [string[]]$Arguments) {
    # The staged client's answer, or TimedOut when it does not finish in time.
    # Shares this console (no new window, and Ctrl-C still reaches it).
    $info = [System.Diagnostics.ProcessStartInfo]::new($Exe)
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::Start($info)
    $out = $process.StandardOutput.ReadToEndAsync()
    $err = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($NativeProbeSeconds * 1000)) {
        # Killed and waited for, so its folder can be removed after.
        try { $process.Kill($true); $null = $process.WaitForExit(10000) } catch { }
        return [pscustomobject]@{ TimedOut = $true; Code = $null; Out = ""; Err = "" }
    }
    $process.WaitForExit()
    return [pscustomobject]@{ TimedOut = $false; Code = $process.ExitCode; Out = $out.Result; Err = $err.Result }
}
# The installer-protocol level the staged release must answer (Initialize-NativeClient).
$NativeInstallerProtocol = 4
# The client side (the stdio MCP shim every agent registers, and the
# pseudolife-mcp CLI) is the native binary published with this checkout's
# release, so no Python is looked for or installed (docs/dev/native-runtimes.md,
# docs/dev/release-assets.md). It is fetched, verified against the release's
# SHA256SUMS and run once from a private staging directory before anything on
# this host changes; Install-ShimOnce then installs that exact archive with the
# native runtime installer, which keeps every older runtime a running session
# uses. PSEUDOLIFE_NATIVE_ARCHIVE_URL replaces the release download URL with
# another http(s) directory URL, or a local directory, holding the archive and
# SHA256SUMS.
$script:nativeVersion = $null
$script:nativeArchive = $null
$script:nativeSums = $null
$script:nativeStaged = $null
$script:nativeStageDir = $null
# Set once the release is staged, verified and probed; it stays set after the
# install removes the staged copy, so nothing stages a second time.
$script:nativeProbed = $false
# Set at this run's first change to the host (a write or a container): from
# then on an exit is told as a run that stopped part way, never "nothing
# changed" (the try/finally at the end). nativeInstalledAt names the native
# client once it is installed.
$script:nativeHostChanging = $false
$script:nativeKeptPython = $false
$script:nativeInstalledAt = $null
$script:nativePartWaySaid = $false
$script:nativeRunDone = $false
$script:nativeHelper = $null
function Get-NativeTarget {
    # This host's release target triple, or $null. PSEUDOLIFE_NATIVE_TARGET
    # picks one of the five release targets instead (an emulated one, or a
    # test's); any other value is $null.
    if ($env:PSEUDOLIFE_NATIVE_TARGET) {
        if ($env:PSEUDOLIFE_NATIVE_TARGET -cin @("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-apple-darwin", "aarch64-apple-darwin", "x86_64-pc-windows-msvc")) { return $env:PSEUDOLIFE_NATIVE_TARGET }
        return $null
    }
    if ($IsWindows -or ($null -eq $IsWindows)) {
        $arch = "$env:PROCESSOR_ARCHITEW6432$env:PROCESSOR_ARCHITECTURE"
        if ($arch -match '^AMD64') { return "x86_64-pc-windows-msvc" }
        return $null
    }
    $machine = (& uname -m 2>$null)
    if ($IsLinux) {
        if ($machine -in @("x86_64", "amd64")) { return "x86_64-unknown-linux-gnu" }
        if ($machine -in @("aarch64", "arm64")) { return "aarch64-unknown-linux-gnu" }
    }
    if ($IsMacOS) {
        if ($machine -eq "x86_64") { return "x86_64-apple-darwin" }
        if ($machine -in @("arm64", "aarch64")) { return "aarch64-apple-darwin" }
    }
    return $null
}
function Stop-NativeInstall([string]$Cause, [string]$Fix) {
    Remove-NativeStage
    if ($script:nativeHostChanging) {
        $script:nativePartWaySaid = $true
        [Console]::Error.WriteLine("The install stopped part way: $Cause. $Fix Then rerun the installer to finish; the steps it already completed are kept.")
        exit 1
    }
    # A download already made is kept for a rerun, and the refusal names what
    # is there: the download, or only SHA256SUMS when the archive is not.
    $left = " Nothing was changed."
    if ($script:nativeDownloads -and (Test-Path -LiteralPath $script:nativeDownloads) -and
            @(Get-ChildItem -LiteralPath $script:nativeDownloads -Force -ErrorAction SilentlyContinue).Count) {
        $kept = "the download"
        if ($script:nativeArchive -and -not (Test-Path -LiteralPath $script:nativeArchive) -and
                $script:nativeSums -and (Test-Path -LiteralPath $script:nativeSums)) { $kept = "SHA256SUMS" }
        $left = " Nothing else was changed: $kept in $($script:nativeDownloads) is kept for a rerun (delete it to free the space)."
    }
    [Console]::Error.WriteLine("The native pseudolife-mcp client cannot be installed: $Cause. $Fix$left")
    exit 1
}
function Remove-NativeStage {
    # This run's staging directory; the verified download is kept.
    if ($script:nativeStageDir -and (Test-Path -LiteralPath $script:nativeStageDir)) {
        Remove-Item -LiteralPath $script:nativeStageDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    $script:nativeStageDir = $null
}
function Get-NativeAsset([string]$Base, [string]$Name, [string]$Destination) {
    # "ok", "missing" (no such file there: an HTTP 404 or an absent local
    # file) or the failure's message.
    try {
        if ($Base -match '^https?://') {
            $part = "$Destination.part"
            try {
                Invoke-WebRequest -UseBasicParsing -Uri "$Base/$Name" -OutFile $part -TimeoutSec 900 -ErrorAction Stop
            } catch {
                Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
                $status = $null
                if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
                if ($status -eq 404) { return "missing" }
                if ($status) { return "HTTP $status" }
                return "$($_.Exception.Message)"
            }
            Move-Item -LiteralPath $part -Destination $Destination -Force
        } else {
            $source = Join-Path $Base $Name
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { return "missing" }
            Copy-Item -LiteralPath $source -Destination $Destination -Force
        }
        return "ok"
    } catch { return "$($_.Exception.Message)" }
}
function Initialize-NativeClient {
    # Fetch, verify, extract and run the release binary; refuses on any failure.
    $pyproject = Get-Content -LiteralPath (Join-Path $repo "pyproject.toml") -Raw -ErrorAction SilentlyContinue
    if ("$pyproject" -notmatch '(?m)^version\s*=\s*"([^"]+)"') {
        Stop-NativeInstall "this checkout's pyproject.toml names no version" "Check out a release tag (git checkout v<version>) and rerun."
    }
    $script:nativeVersion = $Matches[1]
    $target = Get-NativeTarget
    if (-not $target) {
        if ($env:PSEUDOLIFE_NATIVE_TARGET) {
            Stop-NativeInstall "PSEUDOLIFE_NATIVE_TARGET names $($env:PSEUDOLIFE_NATIVE_TARGET), which no release publishes" "Set it to one of x86_64-unknown-linux-gnu, aarch64-unknown-linux-gnu, x86_64-apple-darwin, aarch64-apple-darwin, x86_64-pc-windows-msvc, or unset it."
        }
        Stop-NativeInstall "no native client is published for this platform ($env:PROCESSOR_ARCHITECTURE); releases carry Windows x86_64, Linux x86_64 and aarch64, and macOS Intel and Apple silicon" "Run this installer on one of those (a machine there can use a daemon elsewhere with -ClientOnly)."
    }
    $isZip = $target -like "*windows*"
    $name = "pseudolife-mcp-$($script:nativeVersion)-$target." + $(if ($isZip) { "zip" } else { "tar.gz" })
    $binaryName = if ($isZip) { "pseudolife-mcp.exe" } else { "pseudolife-mcp" }
    $base = if ($env:PSEUDOLIFE_NATIVE_ARCHIVE_URL) { "$env:PSEUDOLIFE_NATIVE_ARCHIVE_URL".TrimEnd('/') } else { "https://github.com/Pseudogiant-xr/Pseudolife-MCP/releases/download/v$($script:nativeVersion)" }
    if ($base -match '^https?://([^/]*)') {
        if ($Matches[1] -like '*@*') {
            Stop-NativeInstall "PSEUDOLIFE_NATIVE_ARCHIVE_URL carries a user name or password, which would be printed with any download error" "Serve the archives without credentials in the URL."
        }
    }
    $downloads = Join-Path (Join-Path (Join-Path $HOME ".pseudolife-mcp") "downloads") $script:nativeVersion
    # A folder that could not be made is the cause, not the download after it.
    try { New-Item -ItemType Directory -Force -Path $downloads -ErrorAction Stop | Out-Null; $script:nativeDownloads = $downloads }
    catch { Stop-NativeInstall "the download folder $downloads could not be made ($($_.Exception.Message))" "Check that $(Join-Path $HOME '.pseudolife-mcp') is a folder you can write (free space, permissions, or a file in its place) and rerun." }
    $script:nativeSums = Join-Path $downloads "SHA256SUMS"
    $script:nativeArchive = Join-Path $downloads $name
    $sumsFetch = Get-NativeAsset $base "SHA256SUMS" $script:nativeSums
    if ($sumsFetch -eq "missing") {
        Stop-NativeInstall "release v$($script:nativeVersion) has no SHA256SUMS at $base (releases before the native client carry none)" "Update this checkout to a newer release (git pull, or git checkout of a newer tag) and rerun."
    } elseif ($sumsFetch -ne "ok") {
        if ($sumsFetch -like "HTTP *") {
            Stop-NativeInstall "SHA256SUMS could not be fetched from $base ($sumsFetch)" "Retry in a few minutes; if it persists, check a proxy or firewall in between (or PSEUDOLIFE_NATIVE_ARCHIVE_URL)."
        }
        Stop-NativeInstall "SHA256SUMS could not be fetched from $base ($sumsFetch)" "Check the connection (or PSEUDOLIFE_NATIVE_ARCHIVE_URL) and retry."
    }
    # Either standard form of a checksum line: two spaces, or space-asterisk
    # (binary mode, as sha256sum writes under Git Bash); nothing looser.
    $pattern = '^[0-9a-f]{64} [ *]' + [regex]::Escape($name) + '$'
    $lines = @(Get-Content -LiteralPath $script:nativeSums | Where-Object { $_ -cmatch $pattern })
    if ($lines.Count -ne 1) {
        Stop-NativeInstall "SHA256SUMS of release v$($script:nativeVersion) lists $name $($lines.Count) times, not once" "Rerun later; if it persists, report it at https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues."
    }
    $expected = $lines[0].Substring(0, 64)
    $have = (Test-Path -LiteralPath $script:nativeArchive -PathType Leaf) -and ((Get-FileHash -LiteralPath $script:nativeArchive -Algorithm SHA256).Hash.ToLowerInvariant() -eq $expected)
    if (-not $have) {
        $archiveFetch = Get-NativeAsset $base $name $script:nativeArchive
        if ($archiveFetch -ne "ok") {
            Stop-NativeInstall "the download of $name from $base failed ($archiveFetch)" "Check the network (or PSEUDOLIFE_NATIVE_ARCHIVE_URL) and rerun."
        }
    }
    if ((Get-FileHash -LiteralPath $script:nativeArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        Remove-Item -LiteralPath $script:nativeArchive -Force
        Stop-NativeInstall "$name does not match the release's SHA256SUMS (the downloaded archive was removed)" "Rerun to download it again."
    }
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ("pseudolife-native-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $script:nativeStageDir = $stage
    try {
        if ($isZip) {
            Expand-Archive -LiteralPath $script:nativeArchive -DestinationPath $stage -Force
        } else {
            & tar -xzf $script:nativeArchive -C $stage $binaryName
            if ($LASTEXITCODE -ne 0) { throw "tar exited $LASTEXITCODE" }
        }
    } catch {
        Stop-NativeInstall "$name could not be unpacked ($($_.Exception.Message))" "Rerun; the verified archive is kept in $downloads."
    }
    $script:nativeStaged = Join-Path $stage $binaryName
    if (-not (Test-Path -LiteralPath $script:nativeStaged -PathType Leaf)) {
        Stop-NativeInstall "$name holds no $binaryName" "Rerun later; if it persists, report it at https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues."
    }
    if (-not $isZip) {
        # tar keeps the archive's 0755; set it again without a PATH lookup.
        try { [System.IO.File]::SetUnixFileMode($script:nativeStaged, [System.IO.UnixFileMode]"UserRead, UserWrite, UserExecute, GroupRead, GroupExecute, OtherRead, OtherExecute") } catch { }
    }
    try {
        $answer = Invoke-NativeBounded $script:nativeStaged @("version")
        if ($answer.TimedOut) {
            Stop-NativeInstall "the downloaded client did not answer version within $NativeProbeSeconds s" "Rerun; if it persists, report it at https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues."
        }
        $says = "$($answer.Out)$($answer.Err)".Trim()
        $ran = ($answer.Code -eq 0)
    } catch { $says = $_.Exception.Message; $ran = $false }
    if (-not $ran) {
        $runFix = if ($target -like "*windows*") { "Check that nothing (an antivirus or an execution policy) blocks it, and rerun." } elseif ($target -like "*linux*") { "The Linux client needs glibc 2.35 or newer (Ubuntu 22.04, Debian 12); run this installer on such a system." } else { "The macOS client needs macOS 11 or newer." }
        Stop-NativeInstall "the downloaded client does not run on this host ($(($says -split "`n")[0]))" $runFix
    }
    # The installer steps this script runs belong to the staged release
    # (ops/install-native.sh, NATIVE_INSTALLER_PROTOCOL, says why): exactly
    # `pseudolife-mcp installer-protocol <N>`, N at least this level.
    $level = -1
    try {
        $probe = Invoke-NativeBounded $script:nativeStaged @("installer", "--capability")
        if ($probe.TimedOut) {
            Stop-NativeInstall "the downloaded client did not answer installer --capability within $NativeProbeSeconds s" "Rerun; if it persists, report it at https://github.com/Pseudogiant-xr/Pseudolife-MCP/issues."
        }
        $answer = @("$($probe.Out)" -split "`n" | ForEach-Object { $_.TrimEnd("`r") })
        if ($answer.Count -gt 1 -and $answer[-1] -eq "") { $answer = $answer[0..($answer.Count - 2)] }
        # Exactly one line, and a level of 1 to 9 digits with no leading zero.
        if (($probe.Code -eq 0) -and ($answer.Count -eq 1) -and ($answer[0] -cmatch '^pseudolife-mcp installer-protocol (0|[1-9][0-9]{0,8})$')) { $level = [int]$Matches[1] }
    } catch { $level = -1 }
    if ($level -lt $NativeInstallerProtocol) {
        Stop-NativeInstall "the native client $($script:nativeVersion) predates this installer: it does not answer installer --capability with pseudolife-mcp installer-protocol $NativeInstallerProtocol or newer, so it lacks installer steps this checkout runs" "Install the default client by running the installer without PSEUDOLIFE_SHIM_ENGINE (in PowerShell, Remove-Item Env:PSEUDOLIFE_SHIM_ENGINE first), then move this machine with PSEUDOLIFE_SHIM_ENGINE=native pseudolife-mcp update --clients-only (PowerShell: `$env:PSEUDOLIFE_SHIM_ENGINE = 'native'; pseudolife-mcp update --clients-only); this native installer itself works from a checkout of a release that carries it."
    }
    # A tunnel registered to start by itself through this machine's Python
    # client keeps it on that client (issue #1632): refused here, before the
    # host changes. A release without the check answers a usage error, and
    # one that does not answer in time is not waited for; runtime install
    # itself refuses the same move later.
    $check = $null
    try { $check = Invoke-NativeBounded $script:nativeStaged @("runtime", "install", "--check") } catch { $check = $null }
    if ($check -and $check.TimedOut) {
        [Console]::Error.WriteLine("  the tunnel-service check did not answer in $NativeProbeSeconds s; runtime install checks again later")
    } elseif ($check) {
        $says = "$($check.Err)$($check.Out)".Trim()
        if ($check.Code -eq 3) {
            if ($says) { [Console]::Error.WriteLine($says) }
            Stop-NativeInstall "a tunnel is registered to start by itself through this machine's Python client. The native client has no tunnel service command to manage that registration after the move. It can run the tunnel in the foreground with pseudolife-mcp tunnel run" "Rerun the installer without PSEUDOLIFE_SHIM_ENGINE=native (in PowerShell, Remove-Item Env:PSEUDOLIFE_SHIM_ENGINE first) to keep the Python client. To move to the native client, run the autostart removal command above, then rerun. Removing autostart means the tunnel will no longer start on its own."
        } elseif (($check.Code -eq 0) -and $says) {
            [Console]::Error.WriteLine($says)
        }
    }
    $script:nativeProbed = $true
}
function Get-NativeLauncherPath {
    # The stable launcher's path, whether or not it exists yet.
    if ($env:PSEUDOLIFE_SHIM_LAUNCHER) { return $env:PSEUDOLIFE_SHIM_LAUNCHER }
    if ($IsWindows -or ($null -eq $IsWindows)) { return (Join-Path $env:LOCALAPPDATA "pseudolife-mcp\bin\pseudolife-mcp.exe") }
    $data = if ($env:XDG_DATA_HOME) { $env:XDG_DATA_HOME } else { Join-Path $HOME ".local/share" }
    return (Join-Path $data "pseudolife-mcp/bin/pseudolife-mcp")
}
function Get-NativeInstalledLauncher {
    # The stable launcher when it is the native client, else $null.
    $candidate = Get-NativeLauncherPath
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $null }
    try {
        $capability = (& $candidate update --native-capability 2>$null | Select-Object -Last 1)
    } catch { return $null }
    if (($LASTEXITCODE -eq 0) -and ("$capability".Trim() -eq "pseudolife-mcp native-update-v1")) { return $candidate }
    return $null
}
function Add-NativeLauncherToUserPath([string]$Launcher) {
    # The launcher's directory goes first on the user PATH (HKCU\Environment
    # only, %VAR% entries kept as written; ops/add-user-path.ps1), so
    # `pseudolife-mcp` in a new terminal reaches it; an older copy further
    # down is left in place. This run's PATH gets it too. Returns the one
    # line to show, or $null.
    if (-not ($IsWindows -or ($null -eq $IsWindows))) { return $null }
    if ($env:PSEUDOLIFE_SHIM_LAUNCHER) { return $null }
    $dir = Split-Path -Parent $Launcher
    $env:PATH = $dir + [IO.Path]::PathSeparator + $env:PATH
    try {
        $state = (& (Join-Path $repo "ops/add-user-path.ps1") -Dir $dir | Select-Object -Last 1)
    } catch {
        $script:shimPathState = "failed"
        return "the user PATH could not be updated ($($_.Exception.Message)); run the launcher by its full path"
    }
    if ("$state" -eq "current") { return $null }
    return "$dir is now first on your user PATH; new terminals run pseudolife-mcp from there"
}
function Install-NativeClient {
    # Install-ShimOnce's install: the verified archive into a new side-by-side
    # runtime behind the stable launcher. Returns the launcher path, or $null.
    if (-not $script:nativeStaged) { Initialize-NativeClient }
    $launcherPath = Get-NativeLauncherPath
    $before = $null
    if (Get-NativeInstalledLauncher) { $before = (Get-FileHash -LiteralPath $launcherPath -Algorithm SHA256).Hash }
    $changingBefore = $script:nativeHostChanging
    $script:nativeHostChanging = $true
    # A failed install keeps the staged copy: the installer steps after this
    # one still run it (Invoke-NativeHelper), and the script's end removes it.
    try {
        $launcher = & $script:nativeStaged runtime install --archive $script:nativeArchive --sha256sums $script:nativeSums --version $script:nativeVersion | Select-Object -Last 1
    } catch { return $null }
    $installExit = $LASTEXITCODE
    if ($installExit -eq 3) {
        # Refused before it changed anything (a tunnel registered to start by
        # itself runs this machine's Python client; its line is above).
        $script:nativeHostChanging = $changingBefore
        $script:nativeKeptPython = $true
        return $null
    }
    if (($installExit -ne 0) -or (-not $launcher)) { return $null }
    $launcher = "$launcher".Trim()
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { return $null }
    $script:nativeInstalledAt = $launcher
    # The installer steps now run the launcher just installed: the same
    # release as the staged copy, which is removed.
    $script:nativeHelper = $launcher
    Remove-NativeStage
    $script:nativeStaged = $null
    if ($before -and ($before -eq (Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash)) {
        Step "Native client $($script:nativeVersion) already installed at $launcher; nothing changed"
    } else {
        Step "Native client $($script:nativeVersion) installed at $launcher (running sessions keep the client they started with)"
    }
    $pathNote = Add-NativeLauncherToUserPath $launcher
    if ($pathNote) {
        if (-not $script:shimPathState) { $script:shimPathState = "linked" }
        $script:shimPathNotes = @($pathNote)
        Write-Host "  shim: $pathNote"
    }
    try { & $launcher installer expose 2>$null | Out-Null } catch { }
    return $launcher
}
function Invoke-NativeHelper {
    # One installer step of this run's verified native client: the staged
    # copy until Install-NativeClient installs that release, then its
    # launcher. Never a launcher found on the host, which may be an older
    # release without these steps. Input piped to this function goes to it.
    $exe = if ($script:nativeStaged) { $script:nativeStaged } else { $script:nativeHelper }
    # Before a first install's stage 2 nothing is staged: exit 127, and a
    # caller with a rule of its own (the loopback test) uses that.
    if (-not $exe) { $global:LASTEXITCODE = 127; return }
    if ($MyInvocation.ExpectingInput) { $input | & $exe installer @args } else { & $exe installer @args }
}
# <<< native client <<<
# The rest of the run is inside try/finally, so the staging folder goes
# whatever way it ends: an exit, an error or Ctrl-C, also when the script
# returns to an open pwsh session (Remove-NativeStage; the download is kept).
try {
# >>> native client stage >>>
# Fetched and verified before anything here changes: a refusal leaves the
# host as it was. A rerun reads its recorded choices through it, so it is
# staged here; a first install stages it at stage 2, after its parameters
# are checked, so a mistyped one is refused without a download.
if (-not $ClientOnly -and -not $DaemonUrl -and -not $env:PSEUDOLIFE_MCP_DAEMON_URL -and (Test-Path -LiteralPath $envFile)) {
    Initialize-NativeClient
}
# <<< native client stage <<<

# >>> recorded choices >>>
function Invoke-InstallerState {
    if ($args[0] -in 'write-env', 'write-file', 'started', 'record') {
        $input | Invoke-NativeHelper state @args
    } else {
        Invoke-NativeHelper state @args
    }
    if ($LASTEXITCODE -ne 0) { throw "Installer state could not be read or saved; fix the line above and rerun." }
}
$script:installerRerun = $false
$script:recordedBankLocation = $null
$script:recordedInstructionChoices = @{}
$restoredExtractor = $false
$restoredExtractorRequested = $false
$restoredClient = $false
$clientExplicit = [bool]$Client
if (-not $ClientOnly -and -not $DaemonUrl -and -not $env:PSEUDOLIFE_MCP_DAEMON_URL -and (Test-Path -LiteralPath $envFile)) {
    $recordedChoices = @(Invoke-InstallerState choices --env-file $envFile)
    foreach ($recordedLine in $recordedChoices) {
        $key, $value = "$recordedLine" -split '=', 2
        switch ($key) {
            'EXTRACTOR' { if (-not $Extractor) { $Extractor = $value; $restoredExtractor = $true } }
            'MODEL' { if ($restoredExtractor -and -not $Model) { $Model = $value } }
            'EXTRACTOR_URL' { if ($restoredExtractor -and -not $ExtractorUrl) { $ExtractorUrl = $value } }
            'SHIM_PORT' { if ($restoredExtractor -and $ShimPort -eq 0 -and -not $PSBoundParameters.ContainsKey('ShimPort')) { $ShimPort = [int]$value } }
            'CLIENT' { if (-not $Client) { $Client = $value; $restoredClient = $true } }
            'BANK_LOCATION' { $script:recordedBankLocation = $value; $script:installerRerun = $true }
            'INSTRUCTIONS_GEMINI' { $script:recordedInstructionChoices['gemini'] = $value }
            'INSTRUCTIONS_GENERIC' { $script:recordedInstructionChoices['generic'] = $value }
            'GENERIC_TARGET_JSON' { if (-not $PSBoundParameters.ContainsKey('AgentsFile')) { $AgentsFile = $value | ConvertFrom-Json } }
            'INSTRUCTIONS' { if (-not $PSBoundParameters.ContainsKey('Instructions') -and -not $PSBoundParameters.ContainsKey('ClaudeMd')) { $Instructions = $value } }
            'CODEX_HOOKS' { if (-not $PSBoundParameters.ContainsKey('CodexHooks')) { $CodexHooks = $value } }
            'CLAUDE_PLUGIN' { if (-not $PSBoundParameters.ContainsKey('ClaudePlugin')) { $ClaudePlugin = $value } }
            'CLAUDE_LEGACY_HOOKS' { if (-not $PSBoundParameters.ContainsKey('ClaudeLegacyHooks')) { $ClaudeLegacyHooks = $value } }
            'TRANSPORT' { if (-not $PSBoundParameters.ContainsKey('Transport')) { $Transport = $value } }
            'NO_TOKEN' { if (-not $PSBoundParameters.ContainsKey('NoToken') -and $value -eq '1') { $NoToken = [switch]$true } }
        }
    }
    if ($restoredClient -or $restoredExtractor) {
        # A marker inside the managed block must match the restored choice.
        $insideManaged = $false
        $savedClientRequest = $null
        $savedExtractorRequest = $null
        foreach ($line in (Get-Content -LiteralPath $envFile)) {
            if ($line -ceq $EnvBegin) { $insideManaged = $true; continue }
            if ($line -ceq $EnvEnd) { $insideManaged = $false; continue }
            if (-not $insideManaged) { continue }
            switch -CaseSensitive -Regex ($line) {
                '^PSEUDOLIFE_INSTALL_CLIENT_REQUESTED=(.*)$' { $savedClientRequest = $Matches[1] }
                '^PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=(.*)$' { $savedExtractorRequest = $Matches[1] }
            }
        }
        if ($restoredClient -and $Client) { $clientExplicit = $savedClientRequest -ceq $Client }
        $restoredExtractorRequested = $restoredExtractor -and $Extractor -and ($savedExtractorRequest -ceq $Extractor)
    }
    if ($restoredExtractor) { Assert-ExtractorArgs }
}
# <<< recorded choices <<<

# -- presentation helpers -------------------------------------------------------
# Art and color are interactive sugar only: NO_COLOR unset, no -NoArt, and an
# interactive session. Escapes are generated ([char]27), never raw ESC bytes —
# the tracked-tree control-byte guard bans those.
$Esc = [char]27
function Test-ArtOk {
    $interactive -and -not $script:installerRerun -and -not $env:NO_COLOR -and -not $NoArt -and
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
  - Its token revoked or replaced there: a machine paired with invite
    pairs again (pseudolife-mcp invite <name> --replace on the daemon
    host, then connect <url> --read-code here, run as the update command
    below is), which also re-points Codex. Only a client using a token
    from the daemon's environment writes the new token into its
    token file here (the shims and the Claude Code hooks read it on
    every call), and into Codex's own copy in ~/.codex/pseudolife/token.
  - A daemon upgrade there: the daily client update job (offered
    below, on Linux and Windows) moves the shim to the daemon's
    release; without it, run the update command below with
    --clients-only here, so the shim matches the daemon.
  - Open the Console signed in: pseudolife-mcp console (run as the update
    command below is). From this machine it needs the Console's HTTPS
    address (maintainer setup on the daemon host); until then, run it there.
'@
}
# <<< client-only notes <<<

# >>> shared bank notes >>>
function Show-SharedBankNotes {
    Write-Host @'
  Other machines will use this bank: two commands here, then the
  installer on each of them.
  1. Put the daemon on the tailnet. It checks first that the daemon
     requires a bearer token, and shows its plan before it changes
     anything:
       pseudolife-mcp expose tailscale
  2. For each machine that will join, make a one-time pairing code:
       pseudolife-mcp invite <machine>
  On that machine, run the installer, answer 2, give it the daemon's URL
  that expose printed, and paste the code where it asks for the token
  (or run pseudolife-mcp pair <url> <code> there). The steps by hand,
  and the other ways to expose the daemon: docs/guide/remote-bank.md.
'@
}
# Step 1 changes this machine's tailnet serve, so it is only offered, at a
# terminal, with no as the default; expose then shows its own plan and asks
# again before it changes anything. A failure is reported, not fatal: the
# install itself is complete.
function Invoke-SharedBankExposeOffer {
    if ($script:installerRerun) {
        Write-Host "Shared-bank exposure unchanged; run pseudolife-mcp expose tailscale to change it."
        return
    }
    if (-not $interactive -or $script:installerRerun) { return }
    $reply = Read-BankAnswer "Run pseudolife-mcp expose tailscale now? It changes this machine's tailnet serve [y/N]"
    if ((Get-BankReply $reply) -cnotin "y", "Y", "yes", "Yes", "YES") {
        Write-Host "  Not run: run pseudolife-mcp expose tailscale when you are ready."
        return
    }
    if (-not (Install-ShimOnce) -or -not $script:shimInstallPath) {
        Write-Warning "The pseudolife-mcp shim is unavailable (see above), so expose did not run: run pseudolife-mcp expose tailscale once it is installed."
        return
    }
    try {
        $global:LASTEXITCODE = 0
        & $script:shimInstallPath expose tailscale
        $exposeExit = $LASTEXITCODE
    } catch {
        Write-Host "  $($_.Exception.Message)"
        $exposeExit = 1
    }
    if ($exposeExit -ne 0) {
        Write-Warning "pseudolife-mcp expose tailscale exited $exposeExit (see its message above). The install itself is complete: run it again once that is fixed."
    }
}
# <<< shared bank notes <<<

# >>> maintainer setup offer >>>
# The maintainer's passkeys (signed Console messages and roles) on a daemon
# host: `pseudolife-mcp maintainer setup` does every host step and asks
# before each change; only the passkey itself needs the maintainer. A
# tokenless daemon refuses passkeys, so a tokenless install is not asked.
function Invoke-MaintainerSetupOffer {
    if ($tokenState -notin "minted", "present") { return }
    $haveShim = (Install-ShimOnce) -and $script:shimInstallPath
    # A re-run on a host where they are set up asks nothing.
    if ($haveShim) {
        $global:LASTEXITCODE = 1
        try { & $script:shimInstallPath maintainer setup --check *> $null } catch { $global:LASTEXITCODE = 1 }
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Maintainer passkeys: in place."
            return
        }
    }
    if ($script:installerRerun -or -not $interactive) {
        Write-Host "Maintainer passkeys (sign Console messages and roles): run pseudolife-mcp maintainer setup in a terminal when you want them."
        return
    }
    # One question for the feature and its host changes, default no (it can
    # put the daemon on the tailnet); --yes carries the answer, and setup
    # still asks the passkey check itself.
    $reply = Read-BankAnswer "Set up maintainer passkeys (sign Console messages and roles with Windows Hello, Touch ID, a phone or a security key)? When Tailscale runs, this serves the daemon over HTTPS with tailscale serve (devices on your tailnet can reach it at https://<this machine>:8443), sets its config and restarts it. [y/N]"
    if ((Get-BankReply $reply) -cnotin "y", "Y", "yes", "Yes", "YES") {
        Write-Host "  Not now: run pseudolife-mcp maintainer setup when you want them."
        return
    }
    if (-not $haveShim) {
        Write-Warning "The pseudolife-mcp shim is unavailable (see above), so the passkey setup did not run: run pseudolife-mcp maintainer setup once it is installed."
        return
    }
    $oldTokenFile = $env:PSEUDOLIFE_MCP_TOKEN_FILE
    try {
        if ($boardFile) { $env:PSEUDOLIFE_MCP_TOKEN_FILE = $boardFile }
        $global:LASTEXITCODE = 0
        & $script:shimInstallPath maintainer setup --yes
        $setupExit = $LASTEXITCODE
    } catch {
        Write-Host "  $($_.Exception.Message)"
        $setupExit = 1
    } finally {
        $env:PSEUDOLIFE_MCP_TOKEN_FILE = $oldTokenFile
    }
    if ($setupExit -ne 0) {
        Write-Warning "pseudolife-mcp maintainer setup exited $setupExit (see its message above). The install itself is complete: run it again once that is fixed."
    }
}
# <<< maintainer setup offer <<<

# >>> console sign-in >>>
# A daemon host with a bearer token ends the install with the Console open in
# the browser, signed in: `pseudolife-mcp console` asks the daemon for a
# one-time sign-in link with this machine's token and opens it, and the
# browser asks the person to confirm once. Opening a browser changes nothing
# persistent, so nothing is asked; without a terminal one line names the
# command. It runs after the passkey offer, so a link made after that setup
# names the Console's HTTPS address. A tokenless daemon needs no sign-in.
function Invoke-ConsoleSignin {
    if ($tokenState -notin "minted", "present") { return }
    $haveShim = (Install-ShimOnce) -and $script:shimInstallPath
    if (-not $interactive -or $script:installerRerun) {
        Write-Host "Open the Console signed in: pseudolife-mcp console"
        return
    }
    if (-not $haveShim) {
        Write-Warning "The pseudolife-mcp shim is unavailable (see above), so the Console was not opened: run pseudolife-mcp console once it is installed."
        $script:consoleSigninWarning = $true
        return
    }
    $oldTokenFile = $env:PSEUDOLIFE_MCP_TOKEN_FILE
    try {
        if ($boardFile) { $env:PSEUDOLIFE_MCP_TOKEN_FILE = $boardFile }
        $global:LASTEXITCODE = 0
        & $script:shimInstallPath console --unless-signed-in
        $consoleExit = $LASTEXITCODE
    } catch {
        Write-Host "  $($_.Exception.Message)"
        $consoleExit = 1
    } finally {
        $env:PSEUDOLIFE_MCP_TOKEN_FILE = $oldTokenFile
    }
    if ($consoleExit -ne 0) {
        Write-Warning "pseudolife-mcp console exited $consoleExit (see its message above). The install itself is complete: run pseudolife-mcp console to open the Console signed in."
        $script:consoleSigninWarning = $true
    }
}
# <<< console sign-in <<<

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

# >>> bank location >>>
# The installer's first question, for an interactive run that does not name
# the daemon already (-DaemonUrl, -ClientOnly, or PSEUDOLIFE_MCP_DAEMON_URL in
# the environment): where the memory bank lives. "Another machine" is the
# client-only install, with the daemon's URL asked for here and its token
# after the agents are chosen; "shared" is a local install that ends with the
# steps to expose it. -TokenFile, -ReadToken or -PairingCode already mean
# another machine, so with any of them only the URL is asked. The mode block below then
# resolves and checks the answer exactly as it does the parameters. Without
# a terminal nothing is asked; end of input at one is no answer (exit 2).
$script:bankLocation = if ($script:recordedBankLocation) { $script:recordedBankLocation } else { "local" }
function Show-BankLocationQuestion {
    Write-Host @'

Where does the memory bank live?
  1) On this machine (default)
  2) On another machine that already runs it
  3) On this machine, and other machines will connect to it
'@
}
function Read-NativeBeforeAsking {
    # The first question waits for the staged, verified and probed release;
    # a question after that (the install included) stages nothing again.
    if (-not $script:nativeProbed -and -not $script:nativeHelper) { Initialize-NativeClient }
}
function Read-BankAnswer([string]$Prompt) {
    # The reply, or $null at the end of input.
    Read-NativeBeforeAsking
    return (Read-Host $Prompt)
}
function Get-BankReply($reply) {
    # A reply without surrounding whitespace or one pair of quotes: a path
    # pasted from a file manager, or a URL copied with its quotes.
    $value = "$reply".Trim()
    if (($value.Length -ge 2) -and ($value[0] -in '"', "'") -and ($value[-1] -eq $value[0])) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}
function Invoke-BankLocationQuestion {
    if ($script:recordedBankLocation) {
        Step "Bank location unchanged: $($script:BankLocation)"
        return
    }
    if ($script:DaemonUrl -or $script:ClientOnly -or $env:PSEUDOLIFE_MCP_DAEMON_URL) { return }
    if (-not $interactive -or $script:installerRerun) {
        Write-Host "Unattended default: bank on this machine (local Docker)."
        return
    }
    if (-not $script:TokenFile -and -not $script:ReadToken -and -not $script:PairingCode) {
        Read-NativeBeforeAsking
        Show-BankLocationQuestion
        $remote = $false
        while (-not $remote) {
            $reply = Read-BankAnswer "Choose 1-3 (Enter = 1)"
            if ($null -eq $reply) {
                Write-Host "no answer was given: re-run and answer, or pass -DaemonUrl <url> to use a daemon on another machine"
                exit 2
            }
            switch (Get-BankReply $reply) {
                { $_ -in "", "1" } { return }
                "2" { $remote = $true }
                "3" {
                    if ($script:NoToken) {
                        Write-Host "-NoToken installs a bank without a bearer token, and an unauthenticated bank must never be exposed to other machines (docs/guide/remote-bank.md). Drop -NoToken, or answer 1"
                        exit 2
                    }
                    $script:bankLocation = "shared"
                    return
                }
                default { Write-Host "  please answer 1, 2 or 3" }
            }
        }
    }
    $script:bankLocation = "remote"
    $script:ClientOnly = [switch]$true
    while (-not $script:DaemonUrl) {
        $reply = Read-BankAnswer "The daemon's URL (for example http://100.64.0.2:8765)"
        if ($null -eq $reply) {
            Write-Host "no daemon URL was given: re-run and answer, or pass -DaemonUrl <url>"
            exit 2
        }
        $script:DaemonUrl = Get-BankReply $reply
    }
}
function Get-BankTokenName {
    # The new token file's name, one per principal: named for the one client
    # this run wires, as docs/guide/remote-bank.md recommends, else shared.
    if (@($clients).Count -ne 1) { return "shared" }
    switch ($clients[0]) {
        "claude" { return "claude-code" }
        "generic" { return "mcp-client" }
        default { return $clients[0] }
    }
}
function Invoke-BankTokenQuestion {
    # The other machine's token file: an existing one, or a new one from a
    # pasted token or a pairing code.
    if (($script:bankLocation -ne "remote") -or $script:TokenFile) { return }
    # A token or pairing code to come always goes to a new file.
    $newFile = [bool]($script:ReadToken -or $script:PairingCode)
    if (-not $newFile -and $env:PSEUDOLIFE_MCP_TOKEN_FILE) {
        Step "Token file: $($env:PSEUDOLIFE_MCP_TOKEN_FILE) (PSEUDOLIFE_MCP_TOKEN_FILE in the environment)."
        return
    }
    $name = Get-BankTokenName
    if (-not $newFile) {
        Write-Host ""
        Write-Host "The daemon's bearer token, or a pairing code from pseudolife-mcp invite:"
        Write-Host "  1) Paste it now: it makes a new owner-only token file (default)"
        Write-Host "  2) The token is already in a token file on this machine"
        $choice = $null
        while (-not $choice) {
            $reply = Read-BankAnswer "Choose 1-2 (Enter = 1)"
            if ($null -eq $reply) {
                Write-Host "no answer was given: re-run and answer, or pass -TokenFile <path>"
                exit 2
            }
            switch (Get-BankReply $reply) {
                { $_ -in "", "1" } { $choice = "paste" }
                "2" { $choice = "file" }
                default { Write-Host "  please answer 1 or 2" }
            }
        }
        if ($choice -eq "paste") { $script:ReadToken = [switch]$true; $newFile = $true }
    }
    $userHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    while (-not $script:TokenFile) {
        $prompt = if ($newFile) {
            "The token file to write (Enter = ~/.pseudolife-mcp/$name.token)"
        } else { "The token file" }
        $reply = Read-BankAnswer $prompt
        if ($null -eq $reply) {
            Write-Host "no token file was given: re-run and answer, or pass -TokenFile <path>"
            exit 2
        }
        $path = Get-BankReply $reply
        if (-not $path -and $newFile) { $path = "~/.pseudolife-mcp/$name.token" }
        if ($path -eq "~") {
            $path = $userHome
        } elseif ($path.StartsWith("~/") -or $path.StartsWith("~\")) {
            $path = Join-Path $userHome ($path.Substring(2) -replace '[\\/]', [IO.Path]::DirectorySeparatorChar)
        }
        # A pasted token or a pairing code only ever goes to a new file.
        if ($path -and $newFile -and (Test-Path -LiteralPath $path)) {
            Write-Host "  $path exists already: name another file, or re-run and answer 2 to use it"
            $path = ""
        }
        $script:TokenFile = $path
    }
}
# <<< bank location <<<
Invoke-BankLocationQuestion

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
    # The client's own answer, so the installer and the client never
    # disagree on a form such as 127.0.0.01 or ::ffff:127.0.0.1. Exit 10/11
    # are its answers; anything else falls back to the host rule below.
    # Before the client is staged (a first install's parameters), only a host
    # the rule below could read differently from it stages it: an unusual IP
    # form or a name around localhost. An ordinary name or address is decided
    # here, so a mistyped parameter is still refused without a download.
    if (-not $script:nativeStaged -and -not $script:nativeHelper) {
        $plain = Get-DaemonUrlHost $url
        if ($plain -in "localhost", "::1") { return $true }
        if ($plain -match 'localhost' -or $plain.Contains(":")) { Initialize-NativeClient | Out-Null }
        elseif ($plain -match '[a-z]') { return $false }
        elseif ($plain -notmatch '^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$') { Initialize-NativeClient | Out-Null }
    }
    try {
        Invoke-NativeHelper is-loopback $url 2>$null | Out-Null
        if ($LASTEXITCODE -eq 10) { return $true }
        if ($LASTEXITCODE -eq 11) { return $false }
    } catch { }
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
    if ($TestLogin) { $localFlags += "-TestLogin" }
    if ($Transport -ne "shim") { $localFlags += "-Transport http" }
    if ($localFlags) {
        Write-Host "client-only install: the daemon runs elsewhere, so these flags do not apply: $($localFlags -join ' ') (a local daemon's settings, or an HTTP registration, which cannot carry the token file)$clientOnlyVia"
        exit 2
    }
    if ($PairingCode -and $ReadToken) {
        Write-Host "client-only install: -PairingCode and -ReadToken both say where the token comes from: pass one of them"
        exit 2
    }
} elseif ($TokenFile -or $ReadToken -or $PairingCode) {
    Write-Host "client-only install: -TokenFile, -ReadToken and -PairingCode name a remote daemon's credential, and a local install keeps its token in ops/.env. Add -ClientOnly -DaemonUrl <url>, or drop them"
    exit 2
}
# The client update job follows a daemon on another machine; a daemon host
# keeps its own job (pseudolife-mcp update --schedule there).
if (-not $ClientOnly -and "$ClientUpdateSchedule" -notin "", "ask", "no") {
    Write-Host "-ClientUpdateSchedule $ClientUpdateSchedule asks for the client update job, which only a client-only install (the daemon runs elsewhere) gets; on the daemon's host, pseudolife-mcp update --schedule HH:MM installs the daemon's own job. Drop it, or add -ClientOnly -DaemonUrl <url>"
    exit 2
}
# <<< client-only mode <<<

# >>> engine flag >>>
# After every client-only inference (-DaemonUrl, the environment, the bank
# question), so an implied client-only install refuses it too. ValidateSet
# ignores case; the helper and update do not.
$Engine = "$Engine".ToLowerInvariant()
if ($Engine -and $ClientOnly) {
    [Console]::Error.WriteLine("-Engine selects this host's Docker daemon, and a client-only install runs none; drop -Engine, or run the installer on the daemon's host. Nothing was changed.")
    exit 2
}
# Rust needs a bearer: -NoToken mints none, and neither does -Transport http
# unless ops/.env already sets one.
$envHasToken = [bool]"$env:PSEUDOLIFE_MCP_TOKEN$env:PSEUDOLIFE_MCP_TOKENS" -or ((Test-Path $envFile) -and [bool](Select-String -LiteralPath $envFile -Pattern '^PSEUDOLIFE_MCP_TOKENS?=\S' -Quiet))
$rustBearer = -not ($NoToken -or ($Transport -eq 'http' -and -not $envHasToken))
if ($Engine -eq 'rust' -and -not $rustBearer) {
    [Console]::Error.WriteLine("-Engine rust needs the daemon's bearer token: pseudolife-mcp update verifies the Rust daemon with an authenticated read, and this install would mint none (-NoToken, or -Transport http with no token in ops/.env). Drop that option or use -Engine python. Nothing was changed.")
    exit 2
}
# <<< engine flag <<<
# -- 1. provider selection (before preflight, so it checks what you picked) ------
# The official Windows CLI may be installed outside PATH. Keep its forwarding
# command in this script's scope so discovery, preflight and registration use
# the same executable without changing the user's PATH.
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    $installedCodex = Get-ChildItem "$env:LOCALAPPDATA/OpenAI/Codex/bin/*/codex.exe" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($installedCodex) {
        $script:installerCodexExecutable = $installedCodex.FullName
        function script:codex { & $script:installerCodexExecutable @args }
    }
}
if (-not $Client) {
    if (-not $interactive -or $script:installerRerun) {
        $found = @()
        foreach ($candidate in @("claude", "codex", "gemini")) {
            if ((Get-Command $candidate -ErrorAction SilentlyContinue) -or
                ($candidate -eq "codex" -and (Get-ChildItem "$env:LOCALAPPDATA/OpenAI/Codex/bin/*/codex.exe" -ErrorAction SilentlyContinue))) {
                $found += $candidate
            }
        }
        $Client = if ($found) { $found -join "," } else { "generic" }
        Write-Host "Unattended default: clients=$Client (installed CLIs; generic when none are found)."
    } else {
        Read-NativeBeforeAsking
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
                $clientExplicit = $true
            }
        }
    }
}
$clients = @(Get-ProviderList $Client)
$clientList = $clients -join ","
$requestedClients = if ($clientExplicit) { $clients }
Step "Providers: $($clients -join ' ')"
if ($interactive -and -not $script:installerRerun) {
    Write-Host ""
    Write-Host "What each agent gets:"
    Write-Host ""
    Show-Matrix
    Write-Host ""
}
Invoke-BankTokenQuestion

# -- 2. preflight --------------------------------------------------------------
# >>> native client late stage >>>
if (-not $script:nativeProbed -and -not $script:nativeHelper) { Initialize-NativeClient }
# <<< native client late stage <<<

# The status field of an installer credentials step's JSON report, or $null.
function Get-HelperStatus($output) {
    try { return [string](($output -join "`n") | ConvertFrom-Json).status } catch { return $null }
}
# >>> client-only preflight >>>
# Nothing Docker-shaped to check: a client-only install depends on the
# token file and on the remote daemon answering. The token is never read
# into output. With a pairing code (-PairingCode, or one pasted where the
# token is asked for) the token file does not exist yet: the installed
# shim's pair creates it after the shim is installed (the client-only pair
# block), so here it must not exist, and only the daemon is checked.
function Test-PairingCode([string]$value) {
    # The client's own rule, asked with the value on stdin, never in an
    # argument list. Exit 10/11 are its answers;
    # anything else falls back to the same rule written here: 12 Crockford
    # base32 characters once case, dashes and surrounding whitespace are
    # ignored, O read as 0, and I and L as 1.
    try {
        $global:LASTEXITCODE = 0
        "$value" | Invoke-NativeHelper pairing-code-valid 2>$null | Out-Null
        if ($LASTEXITCODE -eq 10) { return $true }
        if ($LASTEXITCODE -eq 11) { return $false }
    } catch { }
    $code = ("$value".Trim() -replace '-', '').ToUpperInvariant().Replace('O', '0').Replace('I', '1').Replace('L', '1')
    return ($code -cmatch '^[0-9A-HJKMNP-TV-Z]{12}$')
}
function Assert-ClientTokenFile {
    # The shim's own check of the token file, on every OS: an owner-only
    # regular file (the ACL on Windows, mode bits on POSIX), no link, one
    # well-formed token. Stops the install when it cannot use it.
    $tokenReport = Invoke-NativeHelper credentials check-token-file --path $script:TokenFile
    if ("$(Get-HelperStatus $tokenReport)" -ne "ready") {
        $problem = try { [string](($tokenReport -join "`n") | ConvertFrom-Json).recovery } catch { "no result" }
        throw "client-only install: the shim cannot use the token file ($problem): $($script:TokenFile). It reads only an owner-only regular file: chmod 600 it on Linux or macOS, or delete it and re-run with -ReadToken to create it again"
    }
}
function Invoke-ClientOnlyPreflight {
    Step "Client-only install$($clientOnlyVia): this machine's clients will use the daemon at $DaemonUrl (no Docker, volumes or local daemon here)."
    if (-not $script:TokenFile -and $script:PairingCode) {
        # The file the paste path offers: nothing asked where the token goes.
        $userHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
        $script:TokenFile = Join-Path (Join-Path $userHome ".pseudolife-mcp") "$(Get-BankTokenName).token"
    }
    if (-not $script:TokenFile) { $script:TokenFile = "$env:PSEUDOLIFE_MCP_TOKEN_FILE" }
    if (-not $script:TokenFile) {
        Write-Host "client-only install: pass -PairingCode <code> (from pseudolife-mcp invite <name> on the daemon's host) to pair this machine, or, as the fallback, -TokenFile <path> (or set PSEUDOLIFE_MCP_TOKEN_FILE) naming the file that holds the remote daemon's bearer token, with -ReadToken to create it. The installer never mints one for a remote daemon$clientOnlyVia"
        exit 2
    }
    $script:TokenFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($script:TokenFile)
    if ($script:PairingCode) {
        # pair never replaces a file: refused here, before any code is spent.
        if (Test-Path -LiteralPath $script:TokenFile) {
            throw "client-only install: pairing creates the token file, and it already exists: $($script:TokenFile). Pass -TokenFile with a new path, or remove it first"
        }
    } elseif ($ReadToken) {
        if (Test-Path -LiteralPath $script:TokenFile) {
            throw "client-only install: -ReadToken creates the token file, and it already exists: $($script:TokenFile). Drop -ReadToken to use it, or remove it first"
        }
        if (-not $interactive -and -not [Console]::IsInputRedirected) {
            throw "Unattended -ReadToken needs redirected stdin; pipe the credential, or use -TokenFile with an existing file. Nothing changed."
        }
        # Read unechoed (or from piped input) and handed to the helper on
        # stdin: the token never reaches an argument list or the output.
        $tokenValue = if ([Console]::IsInputRedirected) {
            [Console]::In.ReadLine()
        } else {
            [Net.NetworkCredential]::new("", (Read-Host -AsSecureString "Paste the daemon's bearer token, or a pairing code from pseudolife-mcp invite (not shown)")).Password
        }
        if (Test-PairingCode "$tokenValue") {
            $script:PairingCode = "$tokenValue"
            $tokenValue = $null
            Step "That is a pairing code: the token file $($script:TokenFile) is created by pairing, once the shim is installed."
        } else {
            $script:nativeHostChanging = $true
            $tokenReport = "$tokenValue" | Invoke-NativeHelper credentials write-token-file --path $script:TokenFile
            $tokenValue = $null
            if ("$(Get-HelperStatus $tokenReport)" -ne "written") {
                $problem = try { [string](($tokenReport -join "`n") | ConvertFrom-Json).recovery } catch { "no result" }
                throw "client-only install: could not write the token file ($problem): $($script:TokenFile)"
            }
            Step "Wrote the token file $($script:TokenFile) (owner-only; the token is not shown)."
        }
    }
    if ($script:PairingCode) {
        # Its directory, so pair can create the file in it.
        $tokenDir = Split-Path -Parent $script:TokenFile
        try {
            $null = New-Item -ItemType Directory -Force -Path $tokenDir
        } catch {
            throw "client-only install: could not create the token file's directory: $tokenDir"
        }
    } else {
        $tokenItem = Get-Item -LiteralPath $script:TokenFile -Force -ErrorAction SilentlyContinue
        # A Windows symlink can report length zero. Let the shim reject
        # reparse points with the regular-file diagnosis.
        $emptyTokenFile = $tokenItem -and $tokenItem.Length -eq 0 -and
            -not ($tokenItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
        if (-not $tokenItem -or $tokenItem.PSIsContainer -or $emptyTokenFile) {
            throw "client-only install: the token file is missing or empty: $($script:TokenFile). Pair this machine instead: remove it if it is empty, then re-run with -PairingCode <code> from pseudolife-mcp invite <name> on the daemon's host; or, as the fallback, create it with -ReadToken or write the daemon's token into it owner-only, then re-run"
        }
        Assert-ClientTokenFile
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
    # A raw token against another machine's daemon may be one another machine
    # also holds (docs/guide/remote-bank.md): the update line names pairing at
    # the end of the run, with the installed shim's path. Not for a pairing
    # code, nor on loopback (the daemon's own host, or an SSH tunnel).
    $script:pairingHintUrl = ""
    if (-not $script:PairingCode -and -not (Test-LoopbackUrl $DaemonUrl)) {
        $script:pairingHintUrl = "$DaemonUrl"
    }
}
# <<< client-only preflight <<<
Step "Preflight..."
if ($ClientOnly) {
    Invoke-ClientOnlyPreflight
} else {
    # Missing client tools are reported after the host prerequisites. Do
    # not change the standalone preflight's CLI checks.
    $preflightClients = @($clients | Where-Object {
        $_ -notin "claude", "codex", "gemini" -or (Get-Command $_ -ErrorAction SilentlyContinue)
    }) -join ","
    if (-not $preflightClients) { $preflightClients = "generic" }
    # `&` on a .ps1 only refreshes $LASTEXITCODE when the script exits explicitly;
    # clear the stale value a prior native command may have left.
    $global:LASTEXITCODE = 0
    # Its Docker ownership check is a step of this run's verified client.
    $savedNativeCli = $env:PSEUDOLIFE_NATIVE_CLI
    $env:PSEUDOLIFE_NATIVE_CLI = if ($script:nativeStaged) { $script:nativeStaged } else { $script:nativeHelper }
    try {
        & (Join-Path $PSScriptRoot "preflight-native.ps1") -Client $preflightClients
    } finally {
        $env:PSEUDOLIFE_NATIVE_CLI = $savedNativeCli
    }
    if ($LASTEXITCODE -ne 0) { throw "Preflight failed - fix the line(s) above and re-run." }
}

# -- 3. extractor choice (explicit, no default) ---------------------------------
# A client-only install has none: the remote daemon runs its own extractor.
$extractorRequested = $restoredExtractorRequested -or ([bool]$Extractor -and -not $restoredExtractor)
if (-not $Extractor -and -not $ClientOnly) {
    if (-not $interactive -or $script:installerRerun) {
        $insideManaged = $false
        $previousExtractor = $false
        if ($envFile -and (Test-Path -LiteralPath $envFile)) {
            foreach ($line in Get-Content -LiteralPath $envFile) {
                if ($line -eq $EnvBegin) { $insideManaged = $true; continue }
                if ($line -eq $EnvEnd) { $insideManaged = $false; continue }
                if ($insideManaged -and $line -match '^PSEUDOLIFE_DREAM_BASE_URL=.+') { $previousExtractor = $true }
            }
        }
        if ($previousExtractor) {
            throw "The original extractor family is not recorded; rerun with -Extractor endpoint -ExtractorUrl <url> -Model extractor, -Extractor claude-only, or -Extractor openai-only (use the corresponding -fallback mode if configured), and nothing has been changed."
        }
        $Extractor = "sidecar"
        Write-Host "Unattended default: extractor=sidecar (local CPU; no CLI login required)."
    } else {
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
            $choice = Read-Host "Choose 1-7 (Enter = 3, sidecar)"
            switch ($choice) {
                "1" { $Extractor = "claude-only" }
                "2" { $Extractor = "claude-fallback" }
                "" { $Extractor = "sidecar" }
                "3" { $Extractor = "sidecar" }
                "4" { $Extractor = "openai-fallback" }
                "5" { $Extractor = "openai-only" }
                "6" { $Extractor = "endpoint" }
                "7" { $Extractor = "endpoint-fallback" }
                default { Write-Host "  please answer 1-7" }
            }
            if ($Extractor -and $choice) { $extractorRequested = $true }
        }
        if ($Extractor -in "endpoint", "endpoint-fallback") {
            while (-not $ExtractorUrl) {
                $ExtractorUrl = (Read-Host "The server's OpenAI-compatible base URL (e.g. http://127.0.0.1:1234/v1)").TrimEnd("/")
            }
            while (-not $Model) { $Model = Read-Host "The model name that server serves" }
        }
    }
    Assert-ExtractorArgs
}
if (-not $ClientOnly) { Step "Extractor mode: $Extractor" }
$claudeShimMode = $Extractor -in "claude-only", "claude-fallback"
$codexShimMode = $Extractor -in "openai-only", "openai-fallback"
if ($ShimPort -eq 0) { $ShimPort = $codexShimMode ? 8086 : 8082 }
# A model from the wrong family would silently serve the shim's launch
# default: Assert-ExtractorArgs (the extractor modes block) refuses it.
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
    # A redirect past -MaximumRedirection 0 is an error under this script's
    # ErrorAction Stop (measured on pwsh 7.6.6: InvalidOperationException,
    # MaximumRedirectExceeded, no response attached), yet it is a server that
    # answered: -ErrorAction SilentlyContinue returns the 3xx response, and a
    # build that throws anyway is read by its error id.
    $code = 0
    try {
        $answer = Invoke-WebRequest -Uri "$ExtractorUrl/models" -TimeoutSec 10 -MaximumRedirection 0 -SkipHttpErrorCheck -ErrorAction SilentlyContinue
        if ($answer) { $code = [int]$answer.StatusCode }
    } catch {
        if ("$($_.FullyQualifiedErrorId)" -like "MaximumRedirectExceeded*") { $code = 300 }
    }
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
    if ($interactive -and -not $script:installerRerun) {
        Write-Host ""
        Write-Host "Which Claude model should extract memories (the 'dreamer')?"
        Write-Host "  1) claude-opus-5-5   - recommended: clears the extraction-ladder gate with no regression against claude-opus-5 (evals/results/ladder-opus55-paired-verdict-threshold.json, 2026-09-28); the 2026-08-02 judged comparison that chose Opus over Sonnet (best measured extraction quality) ran on claude-opus-5"
        Write-Host "  2) claude-opus-5     - the earlier default: the 2026-08-02 judged comparison chose Opus over Sonnet (best measured extraction quality) on it (evals/results/dreamer-choice-verdict.json)"
        Write-Host "  3) claude-sonnet-5-5 - balanced"
        Write-Host "  4) claude-sonnet-5   - the earlier Sonnet"
        Write-Host "  5) claude-haiku-5-5  - fastest / lightest on plan usage"
        Write-Host "  6) claude-haiku-4-5  - the earlier Haiku"
        Write-Host "  7) claude-fable-5-1  - most capable tier"
        Write-Host "  8) claude-fable-5    - the earlier Fable"
        while (-not $Model) {
            switch (Read-Host "Choose 1-8 (Enter = 1)") {
                { $_ -in "", "1" } { $Model = "claude-opus-5-5" }
                "2" { $Model = "claude-opus-5" }
                "3" { $Model = "claude-sonnet-5-5" }
                "4" { $Model = "claude-sonnet-5" }
                "5" { $Model = "claude-haiku-5-5" }
                "6" { $Model = "claude-haiku-4-5" }
                "7" { $Model = "claude-fable-5-1" }
                "8" { $Model = "claude-fable-5" }
                default { Write-Host "  please answer 1-8" }
            }
        }
    } else {
        $Model = "claude-opus-5-5"
    }
    Step "Dreamer model: $Model$(if (-not $interactive -or $script:installerRerun) { ' (unattended default)' })"
}
# GPT menu: no 'recommended' — extraction quality is unmeasured for all of
# them (the ladder's terra rung exists to measure it); Terra is only the
# shim's balanced default. The GPT-6 ids were accepted by the Codex CLI on
# 2026-09-29; no ladder run has measured them.
if ($codexShimMode -and -not $Model) {
    if ($interactive -and -not $script:installerRerun) {
        Write-Host ""
        Write-Host "Which GPT model should extract memories (the 'dreamer')?"
        Write-Host "  1) gpt-5.6-terra - balanced default (extraction quality unmeasured)"
        Write-Host "  2) gpt-5.6-sol   - flagship (unmeasured)"
        Write-Host "  3) gpt-5.6-luna  - fastest / lightest on plan usage (unmeasured)"
        Write-Host "  4) gpt-6-sol     - GPT-6 flagship (unmeasured)"
        Write-Host "  5) gpt-6-luna    - GPT-6 fastest / lightest (unmeasured)"
        Write-Host "  6) gpt-6.1-sol   - GPT-6.1 flagship (unmeasured)"
        while (-not $Model) {
            switch (Read-Host "Choose 1/2/3/4/5/6 (Enter = 1)") {
                { $_ -in "", "1" } { $Model = "gpt-5.6-terra" }
                "2" { $Model = "gpt-5.6-sol" }
                "3" { $Model = "gpt-5.6-luna" }
                "4" { $Model = "gpt-6-sol" }
                "5" { $Model = "gpt-6-luna" }
                "6" { $Model = "gpt-6.1-sol" }
                default { Write-Host "  please answer 1, 2, 3, 4, 5 or 6" }
            }
        }
    } else {
        $Model = "gpt-5.6-terra"
    }
    Step "Dreamer model: $Model$(if (-not $interactive -or $script:installerRerun) { ' (unattended default)' })"
}

# -- 4. volumes (respect names overridden in an existing ops/.env) --------------
# >>> volumes >>>
function Get-EnvValue($name) {
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match "^$name=" } | Select-Object -Last 1
        if ($line) { return $line.Substring($name.Length + 1) }
    }
    return $null
}
if (-not $ClientOnly) {
    $resolvedVolumes = @(Invoke-NativeHelper volume-names --repo $repo --env-file $envFile)
    if ($LASTEXITCODE -ne 0 -or $resolvedVolumes.Count -ne 2) { throw "Could not resolve Docker volumes; check ops/.env with ops/preflight-native.ps1 and rerun." }
    $bankVol, $stateVol = $resolvedVolumes
    $existingVolumes = @(docker volume ls --format '{{.Name}}')
    if ($LASTEXITCODE -ne 0) { throw "Could not list Docker volumes; check docker info and rerun." }
    $volumesChanged = $false
    $bankExisted = $existingVolumes -contains $bankVol
}
# <<< volumes <<<

# -- 4b. daemon engine -----------------------------------------------------------
# >>> daemon engine >>>
# The bearer stage 5b will have: one already set, or one it mints for the
# shim transport. Rust is verified with an authenticated read.
function Test-RustTokenExpected {
    if (-not $rustBearer) { return $false }
    if ($env:PSEUDOLIFE_MCP_TOKEN -or $env:PSEUDOLIFE_MCP_TOKENS -or
            (Get-EnvValue "PSEUDOLIFE_MCP_TOKEN") -or (Get-EnvValue "PSEUDOLIFE_MCP_TOKENS")) { return $true }
    return ($Transport -eq 'shim')
}
# The engine already installed is kept; a new install takes -Engine (default
# rust since G2, python without a bearer or an available Rust image). Naming the other engine on an existing bank switches it after the
# install through pseudolife-mcp update (backup, rollback tag, health gate).
# Rust pulls the release's image and provisions its models into their own
# volume here, before any container starts, so a refusal starts nothing.
function Invoke-EngineHelper {
    Invoke-NativeHelper engine @args
}
$installedEngine = ""
$composeEngine = ""
$switchEngine = ""
$engineDefaulted = $false
$engineFiles = @()
if (-not $ClientOnly) {
    $installedEngine = "$(Invoke-EngineHelper detect --repo $repo)".Trim()
    if ($LASTEXITCODE -ne 0) { exit 1 }
    if ($installedEngine -eq 'none') {
        # Rust is the default since G2; without a bearer the default stays Python.
        if ($Engine) { $composeEngine = $Engine }
        # An existing bank moves only through update's checks, which its own
        # default does on the next attended update.
        elseif ($bankExisted) {
            $composeEngine = 'python'
            Step "Daemon engine: python for this existing bank; the next attended pseudolife-mcp update moves it to Rust after its checks"
        }
        elseif (Test-RustTokenExpected) { $composeEngine = 'rust'; $engineDefaulted = $true }
        else { $composeEngine = 'python'; Step "Daemon engine: python (no bearer token for the default Rust daemon)" }
        # An existing bank reaches Rust only through update, which checks its
        # schema and counters first.
        if ($composeEngine -eq 'rust' -and $bankExisted) { $composeEngine = 'python'; $switchEngine = 'rust' }
    } elseif ($installedEngine -eq 'rust-unaccepted') {
        # This checkout's own unfinished Rust install: redo it, or install Python.
        $composeEngine = if ($Engine) { $Engine } else { 'rust' }
    } else {
        $composeEngine = $installedEngine
        if ($Engine -and ($installedEngine -replace '-checkout$', '') -ne $Engine) { $switchEngine = $Engine }
    }
    # Assigned, not `= if (...)`: an if-expression unrolls a one-item array to a string.
    $defaultArgs = @()
    # Before anything is pulled: Rust with no bearer to come refuses here.
    if ($composeEngine -eq 'rust' -and -not $engineDefaulted -and -not (Test-RustTokenExpected)) {
        [Console]::Error.WriteLine("The Rust daemon needs its bearer token, and this install cannot mint one (no pipx or pip-capable Python >= 3.10 to install the shim): set PSEUDOLIFE_MCP_TOKEN in ops/.env, or install pipx, then rerun; or rerun with -Engine python. Nothing was pulled or started.")
        exit 2
    }
    if ($engineDefaulted) { $defaultArgs = @('--default') }
    $engineFiles = @(Invoke-EngineHelper prepare --repo $repo --engine $composeEngine @defaultArgs | Where-Object { $_ })
    if ($LASTEXITCODE -ne 0) { exit 1 }
    # A defaulted Rust install whose image or models are unavailable falls back
    # to Python (the helper said why); an explicit -Engine rust refused above.
    if ($composeEngine -eq 'rust' -and $engineFiles.Count -eq 0) { $composeEngine = 'python' }
    if ($switchEngine) { Step "Daemon engine: $($composeEngine -replace '-checkout$', '') for now; switching to $switchEngine after the install" }
    elseif ($installedEngine -in 'none', 'rust-unaccepted') { Step "Daemon engine: $($composeEngine -replace '-checkout$', '')" }
    else { Step "Daemon engine unchanged: $($composeEngine -replace '-checkout$', '')" }
}
# <<< daemon engine <<<

# -- 4c. create volumes (after every refusal above) -----------------------------
# >>> create volumes >>>
if (-not $ClientOnly) {
    foreach ($volume in @($bankVol, $stateVol)) {
        if ($existingVolumes -notcontains $volume) {
            $script:nativeHostChanging = $true
            docker volume create $volume | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not create volume $volume; fix Docker access and rerun." }
            $volumesChanged = $true
        }
    }
    if ($volumesChanged) { Step "Volumes ready: $bankVol, $stateVol" }
    else { Step "Volumes unchanged: $bankVol, $stateVol" }
}
# <<< create volumes <<<

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
# >>> managed configuration >>>
if (-not $ClientOnly) {
    $block = New-Object System.Collections.Generic.List[string]
    $block.Add($EnvBegin)
    foreach ($line in @(Get-ExtractorEnvLines)) { $block.Add($line) }
    $block.Add("PSEUDOLIFE_WRITER_ID=$writerId")
    $block.Add("PSEUDOLIFE_INSTALL_EXTRACTOR=$Extractor")
    $block.Add("PSEUDOLIFE_INSTALL_MODEL=$Model")
    $block.Add("PSEUDOLIFE_INSTALL_EXTRACTOR_URL=$ExtractorUrl")
    $block.Add("PSEUDOLIFE_INSTALL_SHIM_PORT=$ShimPort")
    $block.Add("PSEUDOLIFE_INSTALL_CLIENT=$clientList")
    if ($requestedClients) { $block.Add("PSEUDOLIFE_INSTALL_CLIENT_REQUESTED=$clientList") }
    if ($extractorRequested) { $block.Add("PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=$Extractor") }
    $block.Add("PSEUDOLIFE_INSTALL_BANK_LOCATION=$($script:BankLocation)")
    $block.Add("PSEUDOLIFE_INSTALL_INSTRUCTIONS=$(if ($Instructions) { $Instructions } elseif ($ClaudeMd) { $ClaudeMd } else { 'auto' })")
    $block.Add("PSEUDOLIFE_INSTALL_CODEX_HOOKS=$CodexHooks")
    $block.Add("PSEUDOLIFE_INSTALL_CLAUDE_PLUGIN=$ClaudePlugin")
    $block.Add("PSEUDOLIFE_INSTALL_CLAUDE_LEGACY_HOOKS=$ClaudeLegacyHooks")
    $block.Add("PSEUDOLIFE_INSTALL_TRANSPORT=$Transport")
    $block.Add("PSEUDOLIFE_INSTALL_NO_TOKEN=$(if ($NoToken) { '1' } else { '0' })")
    $block.Add($EnvEnd)
    $script:nativeHostChanging = $true
    $managedStatus = $block | Invoke-InstallerState write-env --env-file $envFile --template (Join-Path $repo "ops/.env.example")
    if ($managedStatus -eq 'unchanged') { Step "Managed configuration unchanged: ops/.env" }
    else { Step "Wrote managed block in ops/.env" }
    if ($Extractor -in "endpoint", "endpoint-fallback") {
        $endpointInContainer = ConvertTo-ExtractorContainerUrl $ExtractorUrl
        if ($endpointInContainer -ne $ExtractorUrl) {
            Step "Extractor endpoint written as $($endpointInContainer): the daemon runs in Docker, where this host is host.docker.internal."
        }
        Write-Host "    If the server wants a key, set PSEUDOLIFE_DREAM_API_KEY in ops/.env outside the managed block."
    }
}
# <<< managed configuration <<<

# -- 5b. bearer token (the agent board needs one) --------------------------------
# A default install mints one, so the board is on; every client wired below
# then carries it in an owner-only token file. The value is never printed.
# Open loopback (docs/guide/configuration.md) stays available: -NoToken, and
# -Transport http, whose registrations cannot carry a token file; a host with
# no Python for the shim falls back to HTTP, so it mints none.
# >>> mint token >>>
$tokenState = $null
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
} else {
    $script:nativeHostChanging = $true
    $mintOutput = Invoke-NativeHelper credentials mint --env-file $envFile
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
# >>> engine token >>>
# The default Rust daemon is verified with an authenticated read, so it
# follows the bearer this stage actually produced, not the one stage 4b
# predicted (a mint can still fail).
if ($composeEngine -eq 'rust' -and $tokenState -notin 'minted', 'present') {
    if ($engineDefaulted) {
        $composeEngine = 'python'
        $engineFiles = @()
        Step "Daemon engine: python after all (no bearer token for the default Rust daemon: $tokenState)"
    } else {
        [Console]::Error.WriteLine("The Rust daemon needs its bearer token, and this install has none ($tokenState): set PSEUDOLIFE_MCP_TOKEN in ops/.env, or rerun without -NoToken and -Transport http so the installer mints one; or rerun with -Engine python. The daemon was not started; the Rust image and models stay downloaded for the rerun.")
        exit 2
    }
}
# <<< engine token <<<

# -- 6. sidecar enable/disable via the compose override --------------------------
# >>> sidecar override >>>
function InstallerOwnsOverride {
    (Test-Path $overrideFile) -and
        ((Get-Content $overrideFile -TotalCount 1) -in $OverrideMarker, $LegacyOverrideMarker, $LegacyShimOverrideMarker)
}
if ($ClientOnly) {
    # No local daemon here, so its compose override is not this run's to change.
} elseif (Test-ExtractorDisablesSidecar) {
    if (-not (Test-Path $overrideFile) -or (InstallerOwnsOverride)) {
        $wantedOverride = (@(
            $OverrideMarker
            "# A profiled service is skipped by ``up`` entirely: the extractor image is"
            "# never built or pulled. Re-run ops\install.ps1 with a sidecar mode to remove."
            "services:"
            "  pseudolife-extractor:"
            "    profiles: [`"disabled`"]"
        ) -join "`n")
        $script:nativeHostChanging = $true
        $overrideStatus = $wantedOverride | Invoke-NativeHelper state write-file --path $overrideFile
        if ($LASTEXITCODE -ne 0) { throw "Could not write this install's override; fix the line above and rerun." }
        if ($overrideStatus -eq 'unchanged') { Step "Sidecar override unchanged (disabled)." }
        else { Step "Sidecar disabled via ops/docker-compose.override.yml" }
    } else {
        if ($script:installerRerun) {
            Step "Operator override unchanged (not installer-managed)."
        } else {
            Write-Host "NOTE: ops/docker-compose.override.yml exists and is not installer-managed."
            Write-Host "      Add this to it yourself to disable the sidecar:"
            Write-Host "        services:"
            Write-Host "          pseudolife-extractor:"
            Write-Host "            profiles: [`"disabled`"]"
        }
    }
    # Remove a leftover running extractor container (container only - it has
    # no volumes; the image is kept for an easy switch back).
    $extractorOwner = docker inspect -f '{{.Id}} {{index .Config.Labels "com.docker.compose.project"}}' pseudolife-mcp-extractor 2>$null
    if ($LASTEXITCODE -eq 0 -and "$extractorOwner" -match '^([a-f0-9]+) pseudolife-mcp$') {
        # Remove the verified ID, never a name another stack could acquire.
        $script:nativeHostChanging = $true
        docker rm -f $Matches[1] | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not remove this install's extractor; check docker info and rerun." }
        Step "Removed the running extractor container"
    }
} elseif (InstallerOwnsOverride) {
    $script:nativeHostChanging = $true
    Remove-Item $overrideFile
    Step "Removed installer-managed override (sidecar re-enabled)"
} else {
    Step "Sidecar override unchanged (enabled)."
}
# <<< sidecar override <<<

# -- 7. bring the stack up --------------------------------------------------------
if (-not $ClientOnly) {
    $compose = @("-p", "pseudolife-mcp", "--env-file", $envFile, "-f", $composeFile)
    if (Test-Path $overrideFile) { $compose += @("-f", $overrideFile) }
    # The engine overlays (stage 4b) follow, in the planner's checks too.
    $engineStateArgs = @()
    foreach ($engineFile in $engineFiles) {
        $compose += @("-f", $engineFile)
        $engineStateArgs += @("--overlay", $engineFile)
    }
    $composeSnapshotJson = Invoke-InstallerState plan --snapshot --repo $repo --env-file $envFile --override $overrideFile @engineStateArgs
    $composePlan = ($composeSnapshotJson | ConvertFrom-Json).action
    if ($composePlan -eq 'unchanged') {
        Step "Docker stack unchanged; no Compose up or build needed."
    } elseif ($composePlan -in 'start', 'build') {
        if ($composePlan -eq 'start') {
            Step "Starting Docker stack with existing images (no build)..."
            $script:nativeHostChanging = $true
            docker compose @compose up -d --no-build
        } else {
            Step "docker compose up -d --build (checkout or images changed)..."
            $script:nativeHostChanging = $true
            docker compose @compose up -d --build
        }
        if ($LASTEXITCODE -ne 0) { throw "compose up failed" }
    } else {
        throw "Cannot determine the Docker startup step; rerun ops/preflight-native.ps1."
    }
    $composeSnapshotJson = $composeSnapshotJson | Invoke-InstallerState started --expected-stdin --repo $repo --env-file $envFile --override $overrideFile @engineStateArgs
}

# -- 8b. Claude Code plugin (hooks + commands layer) ------------------------------
# From here on every mode changes client configuration (plugin, hooks,
# instructions, registrations).
$script:nativeHostChanging = $true
# >>> claude plugin >>>
# The plugin is the third install beside the daemon and the shim, and the
# only one that needed two commands typed inside Claude Code. Installing it
# here gives a fresh machine the session briefing and per-turn hooks from
# the plugin (section 9 then skips the settings.json hooks). Idempotent: an
# installed plugin is left alone - its cache moves through /plugin update,
# and the daemon's session briefing says when it is behind - except that a
# marketplace recorded with the old github source is moved to HTTPS.
$claudePluginId = "pseudolife-memory@pseudolife-mcp"
$claudePluginMarketplace = "pseudolife-mcp"
# The HTTPS git URL, not the owner/repo shorthand: Claude Code records the
# shorthand as a GitHub source and refreshes it over SSH, which fails on a
# host with no GitHub key (seen 2026-10-04). The repository is public.
$claudePluginMarketplaceSource = "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git"
# Claude Code's override points to the plugins root, which contains cache/.
$claudePluginDir = if ($env:CLAUDE_CODE_PLUGIN_CACHE_DIR) { $env:CLAUDE_CODE_PLUGIN_CACHE_DIR }
                   elseif ($env:CLAUDE_CONFIG_DIR) { Join-Path $env:CLAUDE_CONFIG_DIR "plugins" }
                   else { Join-Path $env:USERPROFILE ".claude\plugins" }
$script:pluginClaude = ""
$script:pluginClaudeRecovery = ""
function Get-ClaudePluginManual {
    "inside Claude Code run /plugin marketplace add $claudePluginMarketplaceSource then /plugin install $claudePluginId"
}
function Test-ClaudePluginRecorded {
    $file = Join-Path $claudePluginDir "installed_plugins.json"
    return (Test-Path $file) -and ((Get-Content $file -Raw) -match [regex]::Escape("`"$claudePluginId`""))
}
function Get-ClaudePluginInstalledVersion {
    $file = Join-Path $claudePluginDir "installed_plugins.json"
    try {
        $records = @((Get-Content $file -Raw | ConvertFrom-Json).plugins.$claudePluginId)
        if ($records.Count -gt 0) { return [string]$records[0].version }
    } catch {}
    return ""
}
function Move-ClaudeMarketplaceToHttps {
    # Up to 0.16.1 the marketplace was added by the owner/repo shorthand,
    # which Claude Code records as a github source and refreshes over SSH:
    # on a host with no GitHub SSH key the plugin stops updating (2026-10-04).
    # The native client's plugin-marketplace step moves it to the HTTPS URL
    # through Claude Code's own CLI (settings.json backed up and edited
    # first), prints one line, and prints nothing when no marketplace is
    # recorded.
    $out = [string](Invoke-NativeHelper plugin-marketplace | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Write-Warning $(if ($out) { $out } else { "the plugin marketplace could not be moved to HTTPS" })
    } elseif ($out) {
        Step $out
    }
}
function Install-ClaudePlugin {
    if ($clients -notcontains "claude") { return }
    $PSNativeCommandUseErrorActionPreference = $false
    if (($ClaudePlugin -ne "skip") -and (Get-Command claude -ErrorAction SilentlyContinue)) {
        Move-ClaudeMarketplaceToHttps
    }
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
    $marketplaces = Join-Path $claudePluginDir "known_marketplaces.json"
    $marketplaceKnown = (Test-Path $marketplaces) -and
        ((Get-Content $marketplaces -Raw) -match [regex]::Escape("`"$claudePluginMarketplace`""))
    if (-not $marketplaceKnown) {
        Step "Adding the $claudePluginMarketplace plugin marketplace to Claude Code..."
        claude plugin marketplace add $claudePluginMarketplaceSource
        if ($LASTEXITCODE -ne 0) {
            $script:pluginClaude = "failed"
            $script:pluginClaudeRecovery = "'claude plugin marketplace add $claudePluginMarketplaceSource' failed. Fix the reported error and rerun the installer."
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
        $script:pluginClaudeRecovery = "'claude plugin install $claudePluginId' failed. Fix the reported error and rerun the installer."
        Write-Warning $script:pluginClaudeRecovery
        return
    }
    if (Test-ClaudePluginRecorded) {
        $script:pluginClaude = "installed:$(Get-ClaudePluginInstalledVersion)"
    } else {
        $script:pluginClaude = "failed"
        $pluginRecord = Join-Path $claudePluginDir "installed_plugins.json"
        $script:pluginClaudeRecovery = "'claude plugin install' returned success but $pluginRecord does not list $claudePluginId. Run /plugin install $claudePluginId inside Claude Code."
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
    if (-not $interactive -or $script:installerRerun) { return $null }
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
# The stdio shim helpers come first: a client-only install runs the
# installed shim's connect (the client-only connect block) before the
# Codex credential setup here and the registrars in section 11.
#
# The shim install itself is client-agnostic; memoize one attempt so
# multi-provider runs install once.
$script:shimInstallResult = $null
$script:shimInstallPath = $null
$script:shimUpgradeHeld = $null
$script:shimManagerKind = $null
$script:shimPathNotes = @()
$script:shimPathState = $null
function Resolve-InstalledShimPath {
    # The stable launcher when it is the native client, else $null.
    return (Get-NativeInstalledLauncher)
}
function Install-ShimOnce {
    if ($null -ne $script:shimInstallResult) { return $script:shimInstallResult }
    # The native client (the native client block): a new runtime beside any
    # older one, so nothing a running session uses is replaced, and the
    # launcher every client registers starts the newest complete runtime.
    # Only its last value: a stray pipeline write must not become the path.
    $launcher = Install-NativeClient | Select-Object -Last 1
    if ($launcher) {
        $script:shimInstallPath = $launcher
        $script:shimManagerKind = "runtime"
        $script:shimInstallResult = $true
    } elseif ($script:nativeKeptPython) {
        Write-Warning "The native client was not installed (the reason is above); this machine keeps the Python client, and its client registrations are left as they are."
        $script:shimInstallResult = $false
    } else {
        Write-Warning "The native client could not be installed from $($script:nativeArchive) (see above); rerun the installer, which reuses the verified download."
        $script:shimInstallResult = $false
    }
    return $script:shimInstallResult
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
function Test-ShimRegistrationMigrates($Client, $RegisteredPath = $null) {
    # $true once the client's registration runs the native launcher (moved
    # now, or already there).
    # A pipx, pip --user or Python runtime registration moves (each changed
    # file is backed up first); editable, self-spawning and unrecognised
    # registrations stay as they are. $RegisteredPath is accepted for the
    # callers' shape; the native step finds the registration itself.
    if (-not (Install-ShimOnce)) { return $false }
    try {
        $output = @(& $script:shimInstallPath installer migrate-registrations --client $Client 2>&1)
    } catch { return $false }
    $script:shimMigrated = $null
    if ($output | Where-Object { "$_" -like "moved $Client to the native client*" }) { $script:shimMigrated = "moved" }
    elseif ($output | Where-Object { "$_" -like "$Client already runs the native client*" }) {
        $script:shimMigrated = "current"
        # Where the old Python client stays is said even when the
        # registration needed no edit (it named the stable launcher).
        foreach ($line in $output) {
            if ("$line" -match 'old Python client.*\)$') { Write-Host ("  " + ("$line" -replace '^[^(]*\(', '' -replace '\)$', '')) }
        }
        return $true
    }
    foreach ($line in $output) { Write-Host "  $line" }
    return [bool]$script:shimMigrated
}

# Two env pairs ride each shim registration: PSEUDOLIFE_WRITER_ID (the shim
# forwards it as the X-PL-Writer header — per-provider write attribution)
# and PSEUDOLIFE_MCP_NO_SPAWN=1 (Docker-tier no-spawn guard, 2026-08-29
# incident). CLI env-flag support is probed, never assumed: a missing flag
# fails closed before registration. HTTP transport cannot carry env, so there
# the daemon default (ops/.env) applies and no shim exists to spawn anything.

# >>> client-only connect >>>
# A client-only install re-points the registrations this machine already has
# (an earlier install's, naming another daemon) with the installed shim's
# `connect`, before the Codex credential setup below and the registrars in
# section 11: those create only what is still missing, and the Codex setup
# refuses a daemon URL its configured server does not name. The dry run sends
# no token and writes nothing; when it finds no registration (exit 3),
# connect is not run again. Keep the grouped plan on success, including
# its all-client schedule step. Isolate the providers after a failed plan.
# >>> client registration state >>>
$script:connectedClients = @()
$script:connectCheckedClients = @()
$script:currentClientTokenChecked = $false
$script:currentClientTokenFailure = $null
$script:clientSetupFailures = @{}
$script:httpCredentialWarnings = @()
function Set-ClientSetupFailure([string]$Provider, [string]$Reason) {
    $script:clientSetupFailures[$Provider] = $Reason
    if ($null -ne $mcpState) { $mcpState[$Provider] = "failed" }
}
function Write-ClientSetupFailure([string]$Provider, [string]$Reason) {
    Set-ClientSetupFailure $Provider $Reason
    Write-Warning $Reason
}
# <<< client registration state <<<
function Test-ConnectReport($report) {
    # connect's report, or nothing to trust: a JSON object with rows and exit.
    return ($report -is [pscustomobject]) -and
        ($report.PSObject.Properties.Name -contains "rows") -and
        ($report.PSObject.Properties.Name -contains "exit")
}
function Get-ConnectRows($report) {
    if (Test-ConnectReport $report) { return @(@($report.rows) | Where-Object { $_ }) }
    return @()
}
function Write-ConnectPlan($report, [switch]$ManualOnly) {
    foreach ($row in (Get-ConnectRows $report)) {
        # An absent client is for the registrars to create, not a connect action.
        if (($row.state -eq "absent") -or ($ManualOnly -and ($row.state -ne "manual"))) { continue }
        $where = (@($row.file, $(if ($row.key) { "[$($row.key)]" })) | Where-Object { $_ }) -join " "
        Write-Host ("    {0,-8} {1} {2}{3}" -f $row.state, $row.client, $row.place, $(if ($where) { ": $where" } else { "" }))
        if ($row.changes) {
            foreach ($change in $row.changes.PSObject.Properties) {
                $pair = @($change.Value) + @($null, $null)
                $old = if ($null -eq $pair[0]) { "(unset)" } else { $pair[0] }
                $new = if ($null -eq $pair[1]) { "(removed)" } else { $pair[1] }
                Write-Host "             $($change.Name): $old -> $new"
            }
        }
        if ($row.detail) { Write-Host "             $($row.detail)" }
    }
}
function Test-CurrentClientToken([string]$Name) {
    if (-not $script:currentClientTokenChecked) {
        $script:currentClientTokenChecked = $true
        Step "Verifying the current credential for $Name. If the daemon is unreachable, this can take up to 180 seconds."
        try {
            $raw = Invoke-NativeHelper credentials verify-token-file --daemon-url $DaemonUrl --token-file $TokenFile
            $status = $LASTEXITCODE
            $report = ($raw -join "`n") | ConvertFrom-Json
            if ($status -eq 0 -and $report.status -eq "ready") { return $true }
            $script:currentClientTokenFailure = [string]$report.recovery
        } catch { }
        if (-not $script:currentClientTokenFailure) {
            $script:currentClientTokenFailure = "The current registration's credential could not be verified. Check the daemon and rerun the installer."
        }
    }
    return -not $script:currentClientTokenFailure
}
function Connect-ExistingRegistration([string[]]$Provider, [string]$Name, [switch]$Grouped) {
    if (-not (Install-ShimOnce) -or -not $script:shimInstallPath) {
        Write-Warning "Existing registrations were not checked against $($DaemonUrl): the pseudolife-mcp shim is unavailable (see above). One that names another daemon stays as it is."
        return $true
    }
    $shim = $script:shimInstallPath
    $PSNativeCommandUseErrorActionPreference = $false
    try {
        $global:LASTEXITCODE = 0
        $planOutput = & $shim connect $DaemonUrl --token-file $TokenFile --client $Name --dry-run --json
        $planExit = $LASTEXITCODE
    } catch {
        Write-Warning "Existing registrations were not checked against $($DaemonUrl): $shim could not be run ($($_.Exception.Message)). One that names another daemon stays as it is."
        return $true
    }
    $planJson = @($planOutput) -join "`n"
    $report = try { $planJson | ConvertFrom-Json } catch { $null }
    # Only connect's own report is trusted. Anything else (a shim from before
    # connect, a held one) leaves the install as it was: the registrars run,
    # with their earlier advice for a mismatch.
    if (-not (Test-ConnectReport $report)) {
        $why = "$shim did not answer with connect's plan (exit $planExit; a shim from before connect has none)"
        if ($script:shimUpgradeHeld) { $why += ". It is the shim left in place: $($script:shimUpgradeHeld)" }
        Write-Warning "Existing registrations were not checked against $($DaemonUrl): $why. One that names another daemon stays as it is."
        return $true
    }
    $script:connectCheckedClients += $Provider
    if ($planExit -eq 3) {
        # No registration connect can write: the registrars create them.
        if (@(Get-ConnectRows $report | Where-Object { $_.state -eq "manual" }).Count -gt 0) {
            Write-Warning "pseudolife-mcp connect found entries it does not rewrite; change them by hand:"
            Write-ConnectPlan $report -ManualOnly
        }
        return $true
    }
    if ($planExit -ne 0) {
        if ($Grouped) { return $false }
        # Under --json the reason is only in the report.
        $reason = if ($report.PSObject.Properties.Name -contains "error" -and $report.error) { ": $($report.error)" } else { "" }
        Write-ClientSetupFailure $Provider[0] "pseudolife-mcp connect could not check the existing registration (exit $planExit$reason). Fix the reported problem and rerun the installer."
        return $false
    }
    if (($planJson | Invoke-NativeHelper connect-report current 2>$null) -ceq "current") {
        if (-not (Test-CurrentClientToken $Name)) {
            if (-not $Grouped) { Write-ClientSetupFailure $Provider[0] $script:currentClientTokenFailure }
            return $false
        }
        Step "Verified the current credential for $Name at $DaemonUrl. Nothing to write."
    } else {
        Step "Existing registrations: pseudolife-mcp connect points them at $($DaemonUrl):"
        Write-ConnectPlan $report
        try {
            $global:LASTEXITCODE = 0
            $applyOutput = @(& $shim connect $DaemonUrl --token-file $TokenFile --client $Name --yes 2>&1)
            $applyExit = $LASTEXITCODE
        } catch {
            $applyOutput = @($_.Exception.Message)
            $applyExit = 1
        }
        $applyOutput | Out-Host
        if ($applyExit -ne 0) {
            if ($Grouped) { return $false }
            $meaning = switch ($applyExit) {
                1 { "a write failed, and every file it wrote was rolled back" }
                4 { "verification failed, and nothing was written (see its reason above)" }
                5 { "the plan was applied, but the check after it failed; restore the backups it listed, or re-run connect" }
                default { "see its message above" }
            }
            $reported = $applyOutput | ForEach-Object { "$_" } | Where-Object { $_.StartsWith("connect: ") } | Select-Object -Last 1
            if ($reported) { $meaning = $reported.Substring("connect: ".Length) }
            Write-ClientSetupFailure $Provider[0] "pseudolife-mcp connect exited ${applyExit}: $meaning. Fix the reported problem and rerun the installer."
            return $false
        }
    }
    $script:connectedClients += @(Get-ConnectRows $report |
        Where-Object { ($_.place -eq "registration") -and ($_.state -in "current", "change") } |
        ForEach-Object { [string]$_.client })
    return $true
}
function Invoke-ClientOnlyConnect {
    $providers = @($clients | Where-Object {
        $_ -eq "claude-desktop" -or ($_ -in "claude", "codex", "gemini" -and (Get-Command $_ -ErrorAction SilentlyContinue))
    })
    $names = @($providers | ForEach-Object { if ($_ -eq "claude") { "claude-code" } else { $_ } })
    if ($providers.Count -gt 1) {
        if (Connect-ExistingRegistration $providers ($names -join ",") -Grouped) { return }
        Step "Checking existing registrations individually after the grouped connection failed."
    }
    for ($index = 0; $index -lt $providers.Count; $index++) {
        $null = Connect-ExistingRegistration $providers[$index] $names[$index]
    }
}
# <<< client-only connect <<<
# >>> client-only pair >>>
# A pairing code (-PairingCode, or one pasted where the token is asked for)
# is redeemed by the installed shim's `pair` before anything reads the token
# file: pair mints the token here, writes it owner-only to the token file
# (never over an existing one) and has the daemon accept it. The code goes
# to pair on stdin through the pipeline, never in an argument list or the
# output, and is forgotten once pair has it. The file is then checked as a
# given one is, and the install continues exactly as with a token file.
# Any failure stops the install: pair's exit 5 keeps the file, which may
# hold a token the daemon now accepts.
function Get-PairReportField($report, [string]$field) {
    # A text field of pair's --json report, or "".
    if (($report -is [pscustomobject]) -and ($report.PSObject.Properties.Name -contains $field) -and
        ($report.$field -is [string])) { return $report.$field }
    return ""
}
function Invoke-ClientOnlyPair {
    if (-not (Install-ShimOnce) -or -not $script:shimInstallPath) {
        $script:PairingCode = ""
        Write-Host "client-only install: pairing needs the pseudolife-mcp shim, and it is unavailable (see above). Fix its installation and re-run with the pairing code, or pass -TokenFile <path> with the daemon's token"
        exit 1
    }
    $shim = $script:shimInstallPath
    try {
        $global:LASTEXITCODE = 0
        $pairOutput = "$($script:PairingCode)" | & $shim pair $DaemonUrl --read-code --token-file $script:TokenFile --json
        $pairExit = $LASTEXITCODE
    } catch {
        Write-Host "  $($_.Exception.Message)"
        $pairOutput = $null
        $pairExit = 1
    }
    $script:PairingCode = ""
    $report = try { (@($pairOutput) -join "`n") | ConvertFrom-Json } catch { $null }
    if ($pairExit -ne 0) {
        $pairError = Get-PairReportField $report "error"
        $meaning = switch ($pairExit) {
            4 { "the daemon did not accept the code, and nothing was kept. Ask for a new code (pseudolife-mcp invite <machine> on the daemon's host), then re-run" }
            5 { "the outcome is unknown, or the check after it failed. The token file was kept at $($script:TokenFile) and may hold a token the daemon now accepts: re-run with -TokenFile $($script:TokenFile) to use it, or remove it and ask for a new code" }
            default { "see its message above" }
        }
        Write-Host "client-only install: pseudolife-mcp pair exited $pairExit$(if ($pairError) { " ($pairError)" }): $meaning"
        exit 1
    }
    $principal = Get-PairReportField $report "principal"
    Step "Paired with the daemon at $DaemonUrl$(if ($principal) { " as $principal" }): wrote the token file $($script:TokenFile) (owner-only; the token is not shown)."
    Assert-ClientTokenFile
}
# <<< client-only pair <<<
if ($PairingCode) { Invoke-ClientOnlyPair }
if ($ClientOnly) { Invoke-ClientOnlyConnect }

$installedPlugins = if ($claudePluginDir) { Join-Path $claudePluginDir "installed_plugins.json" }
                    else { Join-Path $env:USERPROFILE ".claude\plugins\installed_plugins.json" }
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
$codexCredentialFailure = $null
$codexRuntimeDefaults = $null
$codexRuntimeRecovery = $null
$briefingCommand = "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
foreach ($selectedClient in $clients) {
    if ($script:clientSetupFailures.ContainsKey($selectedClient)) { continue }
    if ($selectedClient -notin "claude", "codex") { continue }
    if (($selectedClient -eq "claude") -and $claudePluginInstalled) {
        $hookState["claude"] = "plugin"
        Invoke-ClaudeLegacyHookCleanup
        continue
    }
    if ($selectedClient -eq "codex") {
        $codexSetup = [pscustomobject]@{
            source = "skip"; status = "unavailable"; instructions = "skipped"
            recovery = $null
        }
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
            $credentialArgs = @("codex-credentials")
            if ($installerToken) {
                $credentialArgs += "--installer-token-stdin"
            }
            $credentialArgs += @("--installer-daemon-url", $installerUrl)
            $credentialOutput = & {
                # An installer helper can return a JSON report with exit 1.
                $PSNativeCommandUseErrorActionPreference = $false
                if ($installerToken) {
                    $installerToken | Invoke-NativeHelper @credentialArgs
                } else {
                    Invoke-NativeHelper @credentialArgs
                }
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
            } elseif (($credentialExit -ne 0) -and
                      ($credentialResult.status -eq "needs-configuration") -and
                      ($credentialResult.recovery -is [string]) -and $credentialResult.recovery) {
                $codexCredentialBootstrapFailed = $true
                $codexSetup.recovery = $credentialResult.recovery
            } else {
                throw "Invalid credential setup result"
            }
        } catch {
            $codexCredentialBootstrapFailed = $true
            $codexSetup.recovery = "Codex credential setup failed. The helper did not return a usable report. Rerun the installer to retry."
        }
        # pseudolife-mcp connect (the client-only connect block)
        # re-pointed an existing registration already; one still naming
        # another daemon is one it could not write, or connect did not
        # run (a shim from before it).
        if ($ClientOnly -and -not $codexCredentialBootstrapFailed -and
            ("$codexCredentialUrl".TrimEnd("/") -ne $DaemonUrl)) {
            $codexCredentialBootstrapFailed = $true
            $codexSetup.recovery = if ($script:connectCheckedClients -contains "codex") {
                "The existing Codex registration names the daemon at $(if ($codexCredentialUrl) { $codexCredentialUrl } else { '(none)' }), not the daemon at $DaemonUrl, and pseudolife-mcp connect did not re-point it (see its lines above for why). Set PSEUDOLIFE_MCP_DAEMON_URL=$DaemonUrl where that Codex configuration sets it, or run pseudolife-mcp connect $DaemonUrl --client codex --token-file $TokenFile, then re-run."
            } else {
                "The existing Codex registration names the daemon at $(if ($codexCredentialUrl) { $codexCredentialUrl } else { '(none)' }), not the daemon at $DaemonUrl. Edit it in place in the Codex config.toml: set PSEUDOLIFE_MCP_DAEMON_URL=$DaemonUrl in [mcp_servers.pseudolife-memory.env], then re-run."
            }
            [Environment]::SetEnvironmentVariable("PSEUDOLIFE_MCP_TOKEN", $savedCredentialToken, "Process")
            [Environment]::SetEnvironmentVariable("PSEUDOLIFE_MCP_TOKEN_FILE", $savedCredentialFile, "Process")
            [Environment]::SetEnvironmentVariable("PSEUDOLIFE_MCP_DAEMON_URL", $savedCredentialUrl, "Process")
        }
        if ($codexCredentialBootstrapFailed) {
            $codexCredentialFailure = $codexSetup.recovery
        } else {
            $setupArgs = @("codex-hooks", "--checkout", $repo, "--source", $CodexHooks,
                "--trust", $CodexHookTrust, "--instructions", $instructionChoice)
            if (-not $interactive -or $script:installerRerun) { $setupArgs += "--non-interactive" }
            $setupLaunchFailed = $false
            try {
                $setupOutput = Invoke-NativeHelper @setupArgs
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
                $codexSetup.recovery = "Codex setup did not return a valid result. Rerun this installer to retry."
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
    try {
        $global:LASTEXITCODE = 0
        & (Join-Path $PSScriptRoot "install-hook.ps1") -Client $selectedClient -Command $briefingCommand
        if ($LASTEXITCODE -ne 0) { throw "Hook script exited $LASTEXITCODE." }
        $hookState["claude"] = "hook"
    } catch {
        $hookState["claude"] = "failed"
        Write-Warning "The Claude Code session hook could not be written. Check the settings file's JSON and permissions, then rerun the installer."
    }
}

# -- 10. standing memory instructions (consent; never edited without it) ----------
# Codex instructions are resolved by the setup helper after hook verification.
# Other clients retain the existing auto/append/skip behavior and prompts.
$instrState = @{}
$resolvedInstructionChoices = @{}
$resolvedGenericTarget = $null
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
                $script:codexInstructionWarning = $true
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
    $choice = $instructionChoice
    if ($choice -eq 'auto' -and -not $PSBoundParameters.ContainsKey('Instructions') -and -not $PSBoundParameters.ContainsKey('ClaudeMd')) {
        if ($script:recordedInstructionChoices -and $script:recordedInstructionChoices.ContainsKey($selectedClient) -and
            ($selectedClient -ne 'generic' -or -not $PSBoundParameters.ContainsKey('AgentsFile'))) {
            $choice = $script:recordedInstructionChoices[$selectedClient]
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
        if ($selectedClient -in 'gemini', 'generic') { $resolvedInstructionChoices[$selectedClient] = if ($choice -eq 'auto') { 'append' } else { $choice } }
        if ($selectedClient -eq 'generic') { $resolvedGenericTarget = (Get-Item -LiteralPath $instructionPath).FullName }
        continue
    }
    if ($choice -eq "auto") {
        switch ($selectedClient) {
            "claude" {
                # Skipped by default. The settings.json SessionStart hook
                # serves the compact memory core and the live briefing, not
                # this block; the summary names the file to append it to.
                $choice = "skip"
            }
            "gemini" {
                if ($interactive -and -not $script:installerRerun) {
                    $yn = Read-Host "Gemini CLI has no hook system - append the standing memory block to $instructionPath? [Y/n]"
                    $choice = if ($yn -in "n", "N", "no", "NO") { "skip" } else { "append" }
                } else {
                    $choice = "append"
                    Write-Host "Unattended default: Gemini standing instructions=append ($instructionPath)."
                }
            }
            "generic" {
                if ($AgentsFile) {
                    $choice = "append"
                } elseif ($interactive -and -not $script:installerRerun) {
                    $default = Join-Path $env:USERPROFILE "AGENTS.md"
                    $answer = Read-Host "Append the standing memory block to which file? (Enter = $default, `"-`" to skip)"
                    if ($answer -eq "-") {
                        $choice = "skip"
                    } else {
                        $instructionPath = if ($answer) { $answer } else { $default }
                        $choice = "append"
                    }
                } else {
                    $instructionPath = Join-Path $env:USERPROFILE "AGENTS.md"
                    $choice = "append"
                    Write-Host "Unattended default: generic standing instructions=append ($instructionPath)."
                }
            }
        }
    }
    if (($choice -eq "append") -and ($selectedClient -eq "generic") -and -not $instructionPath) {
        Write-Host "NOTE: generic append needs a target - pass -AgentsFile <path>."
        $choice = "skip"
    }
    if ($selectedClient -in 'gemini', 'generic') { $resolvedInstructionChoices[$selectedClient] = $choice }
    if ($choice -eq "append") {
        try {
            Add-MemoryBlock $instructionPath $selectedClient
        } catch {
            Write-Warning "Could not write standing instructions to $instructionPath. $($_.Exception.Message) Check its permissions and rerun the installer."
            $instrState[$selectedClient] = "failed:$instructionPath"
        }
    } else {
        $hintPath = if ($instructionPath) { $instructionPath } else { "<your AGENTS.md>" }
        Step "Standing memory block not written for $selectedClient. To add it:"
        Write-Host "  Add-Content `"$hintPath`" (Get-Content `"$repo\examples\CLAUDE.memory.md`" -Raw)"
        $instrState[$selectedClient] = "skipped:$hintPath"
    }
    if ($selectedClient -eq 'generic' -and $instructionPath -and (Test-Path -LiteralPath $instructionPath)) {
        $resolvedGenericTarget = (Get-Item -LiteralPath $instructionPath).FullName
    }
}

# -- 11. wire into selected MCP clients ----------------------------------------------
# Runs even with the plugin installed: the plugin is the hooks/commands layer
# only, so the MCP transport (shim by default) always comes from here.
# Its shim helpers (Install-ShimOnce and the registration checks) are
# defined at the top of section 9, where a client-only install first needs
# them.
#
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
        Write-ClientSetupFailure $provider "MCP registration failed (exit $LASTEXITCODE). Fix the CLI error above and rerun the installer."
    }
}
function Set-CodexRuntimeDefaults {
    try {
        $runtimeOutput = Invoke-NativeHelper codex-runtime-defaults
        $runtimeExit = $LASTEXITCODE
        $runtimeResult = ($runtimeOutput -join "`n") | ConvertFrom-Json
        if (($runtimeExit -ne 0) -or ($runtimeResult.status -ne "ready") -or
            ($runtimeResult.runtime_defaults -notin "configured", "preserved")) {
            throw "Invalid runtime-default setup result"
        }
        $script:codexRuntimeDefaults = [string]$runtimeResult.runtime_defaults
        Step "Codex runtime defaults ready (startup 240s, tools 240s, required)."
    } catch {
        $script:codexRuntimeDefaults = "failed"
        $script:codexRuntimeRecovery = "Rerun this installer with -Client codex, then retry the Codex task."
        Write-Warning "Codex was registered, but its runtime defaults were not confirmed. $script:codexRuntimeRecovery"
    }
}
# >>> client registration helpers >>>
function Write-ClientHttpAuthResult([string]$Provider, [switch]$Existing) {
    if ($Existing) {
        $warning = "The $Provider HTTP registration was preserved. The daemon requires a bearer token. Verify the registration's bearer credential in the client."
        $script:httpCredentialWarnings += $warning
        Write-Warning $warning
    } elseif ($Transport -eq "shim") {
        Write-ClientSetupFailure $Provider "The native client could not be installed (see above), and the daemon requires a bearer token for this connection. Fix the native client install, then rerun the installer."
    } else {
        Write-ClientSetupFailure $Provider "The installer cannot add the daemon's required bearer token to an HTTP registration. Rerun the installer with -Transport shim."
    }
}
function Test-ClientTransport([string]$Provider, [string]$State) {
    if ($tokenState -notin "minted", "present", "remote") { return $true }
    if ($State -eq "http") { Write-ClientHttpAuthResult $Provider; return $false }
    $credentialFile = switch ($Provider) {
        "claude" { $claudeTokenFile }
        "codex" { $codexCredentialFile }
        "gemini" { $geminiTokenFile }
    }
    if ($credentialFile) { return $true }
    Write-ClientSetupFailure $Provider "The daemon requires a bearer token, and the client credential file could not be prepared. Repair its credential setup and rerun the installer."
    return $false
}
# <<< client registration helpers <<<
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
    $target = Join-Path $env:USERPROFILE ".pseudolife-mcp\$principal.token"
    # The values ride process-scoped env vars the helper reads by NAME -
    # never a command-line argument, never printed.
    $env:PSEUDOLIFE_INSTALLER_TOKEN = Get-DesktopTokenSource
    $env:PSEUDOLIFE_INSTALLER_TOKENS = Get-DesktopTokensSource
    try {
        $clientOutput = Invoke-NativeHelper credentials token-file --principal $principal --path $target
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
if ((($hookState["claude"] -eq "plugin") -or $ClientOnly) -and $claudeTokenFile -and
    -not $script:clientSetupFailures["claude"]) {
    $claudeSettingsHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE ".claude" }
    $settingsArgs = @("credentials", "claude-settings-env",
        "--settings", (Join-Path $claudeSettingsHome "settings.json"),
        "--token-file", $claudeTokenFile, "--daemon-url", $clientDaemonUrl)
    # A client-only install's hooks must reach the remote daemon with the
    # operator's file, whatever an earlier local install left there.
    if ($ClientOnly) { $settingsArgs += "--replace" }
    $settingsOutput = Invoke-NativeHelper @settingsArgs
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
        default {
            $script:clientHookSettingsWarning = $true
            Write-Warning "Could not give the Claude Code hooks the token file ($($settingsOutput -join ' ')). Set PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile and PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl in the env block of ~/.claude/settings.json."
        }
    }
}
# <<< client token files <<<
# >>> client registrations >>>
function Install-SelectedClient([string]$selectedClient) {
    $PSNativeCommandUseErrorActionPreference = $false
    if ($selectedClient -eq "claude-desktop") {
        # No `mcp add` CLI: the entry is merged into claude_desktop_config.json
        # by the native client's installer step (absolute shim path - Desktop's
        # sanitized PATH omits pipx/venv bin dirs; token FILE when gated). It is
        # named pseudolife-desktop so Code-tab sessions keep their own
        # per-session pseudolife-memory server.
        if ($Transport -ne "shim") {
            Write-Warning "Claude Desktop needs the stdio shim (its connector dialog rejects plain-http URLs) - ignoring -Transport http for it."
        }
        $shimReady = Install-ShimOnce
        if ((-not $shimReady) -or (-not $script:shimInstallPath)) {
            Write-ClientSetupFailure "claude-desktop" "pseudolife-mcp shim installation did not yield a usable executable - Claude Desktop not wired. Re-run once the native client installs."
            return
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
        try {
            Invoke-NativeHelper claude-desktop @desktopArgs 2>&1 | Out-Host
        } finally {
            $env:PSEUDOLIFE_DESKTOP_TOKEN_SOURCE = $null
            $env:PSEUDOLIFE_DESKTOP_TOKENS_SOURCE = $null
        }
        Register-Result "claude-desktop" "shim-env" "Wired into Claude Desktop as pseudolife-desktop via the pseudolife-mcp shim (claude_desktop_config.json) - fully quit and relaunch Desktop to load it."
        return
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
        return
    }
    if ($selectedClient -eq "codex") {
        if ($codexCredentialBootstrapFailed) {
            Set-ClientSetupFailure "codex" ([string]$codexCredentialFailure)
            Write-Warning "Codex MCP registration was skipped because credential setup failed."
            return
        }
        if ($ClientOnly -and -not $codexCredentialFile) {
            # Without it Codex would reach whatever daemon its defaults name.
            Write-ClientSetupFailure "codex" "Codex was not registered: credential setup for the daemon at $clientDaemonUrl did not produce a token file. Fix the error above and rerun the installer."
            return
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
                if ((-not $managedRegisteredShim) -and (Test-ShimRegistrationMigrates "codex")) {
                    if ($script:shimMigrated -eq "current") {
                        Step "Codex registration preserved; it already runs the native client."
                        $mcpState["codex"] = "present-current"
                    } else {
                        Step "Codex registration moved to the shim launcher; the native client is installed."
                        $mcpState["codex"] = "present-migrated"
                    }
                } elseif ($managedRegisteredShim) {
                    if (Install-ShimOnce) {
                        if ($script:shimUpgradeHeld) {
                            Write-ClientSetupFailure "codex" "The existing Codex registration was preserved, but its pseudolife-mcp shim was not upgraded - see the warning above."
                        } elseif (-not $bareRegisteredShim) {
                            # a pipx or pip launcher registration moves to the native launcher
                            if ($script:shimManagerKind -ne "runtime") {
                                Step "Codex registration preserved; it runs the native client installed from this release."
                                $mcpState["codex"] = "present-upgraded"
                            } elseif (Test-ShimRegistrationMigrates "codex" $registeredShim) {
                                if ($script:shimMigrated -eq "current") {
                                    Step "Codex registration preserved; it already runs the native client."
                                    $mcpState["codex"] = "present-current"
                                } else {
                                    Step "Codex registration moved to the shim launcher; the native client is installed."
                                    $mcpState["codex"] = "present-upgraded"
                                }
                            } else {
                                Write-ClientSetupFailure "codex" "The Codex registration still names $registeredShim, which nothing upgrades any more: the native client was installed but the registration could not be moved to it (see above). Move it by hand: & `"$($script:shimInstallPath)`" installer migrate-registrations --client codex"
                            }
                        } elseif (Test-ShimRegistrationMigrates "codex") {
                            if ($script:shimMigrated -eq "current") {
                                Step "Codex registration preserved; it already runs the native client."
                                $mcpState["codex"] = "present-current"
                            } else {
                                Step "Codex registration moved from bare pseudolife-mcp to the shim launcher; the native client is installed."
                                $mcpState["codex"] = "present-upgraded"
                            }
                        } else {
                        $resolvedShim = Get-Command pseudolife-mcp -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                        $resolvedShimPath = if ($resolvedShim) { (Resolve-Path -LiteralPath $resolvedShim.Source).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        if ($resolvedShimPath -and [string]::Equals($resolvedShimPath, $script:shimInstallPath, $pathComparison)) {
                            Step "Codex registration preserved; it runs the native client installed from this release."
                            $mcpState["codex"] = "present-upgraded"
                        } else {
                            Write-ClientSetupFailure "codex" "The existing Codex registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run."
                        }
                        }
                    } else {
                        Write-ClientSetupFailure "codex" "The existing Codex registration was preserved, but its pseudolife-mcp native client install failed - see the message above and re-run."
                    }
                } else {
                    Write-Warning "The existing Codex stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update."
                    $mcpState["codex"] = "present-custom"
                }
                if ($codexGuardUnverified) {
                    Set-ClientSetupFailure "codex" "The existing stdio registration has no verifiable no-spawn guard. Set PSEUDOLIFE_MCP_NO_SPAWN=1 in the existing entry, then rerun."
                }
            } else {
                Step "MCP server already wired into Codex - registration preserved."
                $mcpState["codex"] = "present"
            }
            if ($tokenState -in "minted", "present", "remote" -and ((Test-McpHttpRegistration $existingCodex) -or (Test-McpHttpRegistration $existingCodexGuard))) {
                Write-ClientHttpAuthResult "codex" -Existing
            }
            $script:codexRuntimeDefaults = "preserved"
        } elseif (($Transport -eq "shim") -and (Install-ShimOnce)) {
            $envFlag = Get-EnvFlag "codex"
            if ($envFlag) {
                if (-not (Test-ClientTransport "codex" "shim-env")) { return }
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
                Write-ClientSetupFailure "codex" "This Codex CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed."
            }
        } else {
            if ($codexCredentialFile -or $ClientOnly) {
                if ($Transport -eq "shim") {
                    Write-ClientSetupFailure "codex" "Codex authentication requires the stdio shim. HTTP fallback was not registered. Fix the native client install (see above), then rerun the installer."
                } else {
                    Write-ClientSetupFailure "codex" "Codex authentication requires the stdio shim. HTTP transport was not registered. Rerun the installer with -Transport shim."
                }
            } else {
                $codexHttpUrl = if ($codexCredentialUrl) {
                    "$codexCredentialUrl/mcp"
                } else {
                    "http://127.0.0.1:8765/mcp"
                }
                if (-not (Test-ClientTransport "codex" "http")) { return }
                if ($Transport -eq "shim") {
                    Write-Warning "Shim unavailable for Codex (see warnings above) - falling back to HTTP."
                    Write-Host "  Without the shim, a Codex session running beside a Claude Code session shares its episode identity."
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
            if (-not $geminiRegistration) {
                Write-ClientSetupFailure "gemini" "Gemini CLI did not return a usable Pseudolife registration. Check gemini mcp list, then rerun the installer."
                return
            }
            $geminiGuardUnverified = $false
            $geminiCredential = $null
            if (($Transport -eq "shim") -and -not (Test-McpHttpRegistration "$geminiRegistration")) {
                # The CLI list omits env. Inspect only the named registration
                # and distinguish "missing guard" from "cannot read settings".
                $geminiSettings = Join-Path $env:USERPROFILE ".gemini\settings.json"
                $guardOutput = Invoke-NativeHelper credentials gemini-guard --config $geminiSettings 2>$null
                $guardReport = try { $guardOutput -join "`n" | ConvertFrom-Json } catch { $null }
                $geminiCredential = $guardReport.credential
                switch ($guardReport.status) {
                    "enabled" { }
                    "absent" {
                        Write-UnverifiedNoSpawnGuard "Gemini CLI" "the pseudolife-memory entry in ~/.gemini/settings.json"
                        $geminiGuardUnverified = $true
                    }
                    default {
                        $script:geminiGuardWarning = $true
                        Write-Warning "The existing Gemini CLI stdio guard was not verified. Check the pseudolife-memory entry in ~/.gemini/settings.json."
                    }
                }
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
                if ((-not $managedRegisteredShim) -and (Test-ShimRegistrationMigrates "gemini")) {
                    if ($script:shimMigrated -eq "current") {
                        Step "Gemini CLI registration preserved; it already runs the native client."
                        $mcpState["gemini"] = "present-current"
                    } else {
                        Step "Gemini CLI registration moved to the shim launcher; the native client is installed."
                        $mcpState["gemini"] = "present-migrated"
                    }
                } elseif ($managedRegisteredShim) {
                    if (Install-ShimOnce) {
                        if ($script:shimUpgradeHeld) {
                            Write-ClientSetupFailure "gemini" "The existing Gemini CLI registration was preserved, but its pseudolife-mcp shim was not upgraded - see the warning above."
                        } elseif (-not $bareRegisteredShim) {
                            # a pipx or pip launcher registration moves to the native launcher
                            if ($script:shimManagerKind -ne "runtime") {
                                Step "Gemini CLI registration preserved; it runs the native client installed from this release."
                                $mcpState["gemini"] = "present-upgraded"
                            } elseif (Test-ShimRegistrationMigrates "gemini" $registeredShim) {
                                if ($script:shimMigrated -eq "current") {
                                    Step "Gemini CLI registration preserved; it already runs the native client."
                                    $mcpState["gemini"] = "present-current"
                                } else {
                                    Step "Gemini CLI registration moved to the shim launcher; the native client is installed."
                                    $mcpState["gemini"] = "present-upgraded"
                                }
                            } else {
                                Write-ClientSetupFailure "gemini" "The Gemini CLI registration still names $registeredShim, which nothing upgrades any more: the native client was installed but the registration could not be moved to it (see above). Move it by hand: & `"$($script:shimInstallPath)`" installer migrate-registrations --client gemini"
                            }
                        } elseif (Test-ShimRegistrationMigrates "gemini") {
                            if ($script:shimMigrated -eq "current") {
                                Step "Gemini CLI registration preserved; it already runs the native client."
                                $mcpState["gemini"] = "present-current"
                            } else {
                                Step "Gemini CLI registration moved from bare pseudolife-mcp to the shim launcher; the native client is installed."
                                $mcpState["gemini"] = "present-upgraded"
                            }
                        } else {
                        $resolvedShim = Get-Command pseudolife-mcp -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                        $resolvedShimPath = if ($resolvedShim) { (Resolve-Path -LiteralPath $resolvedShim.Source).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        if ($resolvedShimPath -and [string]::Equals($resolvedShimPath, $script:shimInstallPath, $pathComparison)) {
                            Step "Gemini CLI registration preserved; it runs the native client installed from this release."
                            $mcpState["gemini"] = "present-upgraded"
                        } else {
                            Write-ClientSetupFailure "gemini" "The existing Gemini CLI registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run."
                        }
                        }
                    } else {
                        Write-ClientSetupFailure "gemini" "The existing Gemini CLI registration was preserved, but its pseudolife-mcp native client install failed - see the message above and re-run."
                    }
                } else {
                    Write-Warning "The existing Gemini CLI stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update."
                    $mcpState["gemini"] = "present-custom"
                }
                if ($geminiGuardUnverified) {
                    Set-ClientSetupFailure "gemini" "The existing stdio registration has no verifiable no-spawn guard. Set PSEUDOLIFE_MCP_NO_SPAWN=1 in the existing entry, then rerun."
                }
            } else {
                Step "MCP server already wired into Gemini CLI - registration preserved."
                $mcpState["gemini"] = "present"
            }
            if ($tokenState -in "minted", "present", "remote" -and (Test-McpHttpRegistration "$geminiRegistration")) {
                Write-ClientHttpAuthResult "gemini" -Existing
                return
            }
            if ($tokenState -in "minted", "present", "remote" -and
                $script:connectedClients -notcontains "gemini" -and -not $script:clientSetupFailures["gemini"]) {
                if ($geminiCredential -eq "missing" -and -not ($env:PSEUDOLIFE_MCP_TOKEN -or $env:PSEUDOLIFE_MCP_TOKEN_FILE)) {
                    Write-ClientSetupFailure "gemini" "The daemon requires a bearer token, and the existing Gemini CLI stdio entry has no credential in its settings or this launch environment. Set PSEUDOLIFE_MCP_TOKEN_FILE in ~/.gemini/settings.json to a valid token file, then rerun."
                } elseif (-not $geminiCredential -and $geminiTokenFile) {
                    $script:geminiCredentialWarning = "The daemon requires a bearer token. Verify the existing Gemini CLI registration. Edit it in place in ~/.gemini/settings.json and set PSEUDOLIFE_MCP_TOKEN_FILE=$geminiTokenFile if it has no credential."
                    Write-Warning $script:geminiCredentialWarning
                }
            }
        } elseif (($Transport -eq "shim") -and (Install-ShimOnce)) {
            # Probe gemini's own spelling (`-e, --env`): the command below
            # emits the short form, so a help listing only `-e` must still
            # count as env support (Get-EnvFlag matches `--env` alone,
            # which is claude/codex's spelling).
            $geminiHelp = gemini mcp add --help 2>$null
            $envFlag = if ("$geminiHelp" -match '--env|-e,') { "-e" } else { $null }
            if ($envFlag) {
                if (-not (Test-ClientTransport "gemini" "shim-env")) { return }
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
                Write-ClientSetupFailure "gemini" "This Gemini CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed."
            }
        } elseif ($ClientOnly) {
            Write-ClientSetupFailure "gemini" "Shim unavailable for Gemini CLI (see warnings above), and a remote daemon needs it: an HTTP registration cannot carry the token file. Fix that and re-run."
        } else {
            if (-not (Test-ClientTransport "gemini" "http")) { return }
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
                if ((-not $managedRegisteredShim) -and (Test-ShimRegistrationMigrates "claude-code")) {
                    if ($script:shimMigrated -eq "current") {
                        Step "Claude Code registration preserved; it already runs the native client."
                        $mcpState["claude"] = "present-current"
                    } else {
                        Step "Claude Code registration moved to the shim launcher; the native client is installed."
                        $mcpState["claude"] = "present-migrated"
                    }
                } elseif ($managedRegisteredShim) {
                    if (Install-ShimOnce) {
                        if ($script:shimUpgradeHeld) {
                            Write-ClientSetupFailure "claude" "The existing Claude Code registration was preserved, but its pseudolife-mcp shim was not upgraded - see the warning above."
                        } elseif (-not $bareRegisteredShim) {
                            # a pipx or pip launcher registration moves to the native launcher
                            if ($script:shimManagerKind -ne "runtime") {
                                Step "Claude Code registration preserved; it runs the native client installed from this release."
                                $mcpState["claude"] = "present-upgraded"
                            } elseif (Test-ShimRegistrationMigrates "claude-code" $registeredShim) {
                                if ($script:shimMigrated -eq "current") {
                                    Step "Claude Code registration preserved; it already runs the native client."
                                    $mcpState["claude"] = "present-current"
                                } else {
                                    Step "Claude Code registration moved to the shim launcher; the native client is installed."
                                    $mcpState["claude"] = "present-upgraded"
                                }
                            } else {
                                Write-ClientSetupFailure "claude" "The Claude Code registration still names $registeredShim, which nothing upgrades any more: the native client was installed but the registration could not be moved to it (see above). Move it by hand: & `"$($script:shimInstallPath)`" installer migrate-registrations --client claude-code"
                            }
                        } elseif (Test-ShimRegistrationMigrates "claude-code") {
                            if ($script:shimMigrated -eq "current") {
                                Step "Claude Code registration preserved; it already runs the native client."
                                $mcpState["claude"] = "present-current"
                            } else {
                                Step "Claude Code registration moved from bare pseudolife-mcp to the shim launcher; the native client is installed."
                                $mcpState["claude"] = "present-upgraded"
                            }
                        } else {
                        $resolvedShim = Get-Command pseudolife-mcp -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                        $resolvedShimPath = if ($resolvedShim) { (Resolve-Path -LiteralPath $resolvedShim.Source).Path } else { $null }
                        $pathComparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
                        if ($resolvedShimPath -and [string]::Equals($resolvedShimPath, $script:shimInstallPath, $pathComparison)) {
                            Step "Claude Code registration preserved; it runs the native client installed from this release."
                            $mcpState["claude"] = "present-upgraded"
                        } else {
                            Write-ClientSetupFailure "claude" "The existing Claude Code registration was preserved and the checkout shim installed, but bare pseudolife-mcp still resolves to a different executable. Remove the earlier pseudolife-mcp from PATH or put the installed scripts directory first, then re-run."
                        }
                        }
                    } else {
                        Write-ClientSetupFailure "claude" "The existing Claude Code registration was preserved, but its pseudolife-mcp native client install failed - see the message above and re-run."
                    }
                } else {
                    Write-Warning "The existing Claude Code stdio registration uses a custom registered command or interpreter; it was preserved and may need a separate update."
                    $mcpState["claude"] = "present-custom"
                }
                if ($claudeGuardUnverified) {
                    Set-ClientSetupFailure "claude" "The existing stdio registration has no verifiable no-spawn guard. Set PSEUDOLIFE_MCP_NO_SPAWN=1 in the existing entry, then rerun."
                }
            } else {
                Step "MCP server already wired into Claude Code - registration preserved."
                $mcpState["claude"] = "present"
            }
            if ($tokenState -in "minted", "present", "remote" -and (Test-McpHttpRegistration $existingClaude)) {
                Write-ClientHttpAuthResult "claude" -Existing
                return
            }
            # An install from before the board token: the upgraded shim reads
            # a token file, so add it, the daemon URL and the state directory
            # to the registration in place (never a remove and re-add),
            # keeping any credential it already has. Anything else gets the
            # manual fix.
            $claudeCredentialOk = $false
            if (($mcpState["claude"] -in "present-upgraded", "present-migrated", "present-current") -and $claudeTokenFile) {
                $claudeEnvOutput = Invoke-NativeHelper credentials registration-env --config $claudeJson --set "PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile" --set "PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl" --set "PSEUDOLIFE_AGENT_STATE_DIR=$claudeAgentStateDir"
                switch ("$(Get-HelperStatus $claudeEnvOutput)") {
                    "updated" {
                        $claudeCredentialOk = $true
                        Step "Claude Code registration: added the board token file, daemon URL and state directory in place. Restart Claude Code sessions to load them."
                    }
                    "unchanged" { $claudeCredentialOk = $true }
                }
            }
            if ((-not $claudeCredentialOk) -and $claudeTokenFile -and ($existingClaude -notmatch 'PSEUDOLIFE_MCP_TOKEN')) {
                Write-ClientSetupFailure "claude" "The daemon requires a bearer token, but the existing Claude Code registration carries none. Set PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile in its stdio registration, then rerun."
            }
            # pseudolife-mcp connect (the client-only connect block)
            # re-pointed an existing registration already; one still naming
            # another daemon is one it could not write (a project scope, an
            # HTTP entry), or connect did not run (a shim from before it).
            if ($ClientOnly -and -not ([string](claude mcp get pseudolife-memory 2>$null | Out-String)).Contains($clientDaemonUrl)) {
                if ($script:connectCheckedClients -contains "claude") {
                    Write-ClientSetupFailure "claude" "The existing Claude Code registration does not name the daemon at $clientDaemonUrl, and pseudolife-mcp connect did not re-point it (see its lines above for why). Set PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl and PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile in the entry it names."
                } else {
                    Write-ClientSetupFailure "claude" "The existing Claude Code registration does not name the daemon at $clientDaemonUrl. Edit it in place and set PSEUDOLIFE_MCP_DAEMON_URL=$clientDaemonUrl and PSEUDOLIFE_MCP_TOKEN_FILE=$claudeTokenFile."
                }
            }
        } elseif ($Transport -eq "shim") {
            if (Install-ShimOnce) {
                $envFlag = Get-EnvFlag "claude"
                if ($envFlag) {
                    if (-not (Test-ClientTransport "claude" "shim-env")) { return }
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
                    Write-ClientSetupFailure "claude" "This Claude CLI has no env flag; the stdio registration was skipped because PSEUDOLIFE_MCP_NO_SPAWN=1 cannot be guaranteed."
                }
            } elseif ($ClientOnly) {
                Write-ClientSetupFailure "claude" "Could not install the pseudolife-mcp shim (see warnings above), and a remote daemon needs it: an HTTP registration cannot carry the token file. Fix that and re-run."
            } else {
                if (-not (Test-ClientTransport "claude" "http")) { return }
                Write-Warning "Could not install the pseudolife-mcp shim: the native client install failed (see warnings above)."
                Write-Host "  Without the shim, concurrent Claude Code sessions share one episode identity."
                Write-Host "  Re-run once that is fixed (the verified download is reused), or pass -Transport http to silence this."
                claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
                Register-Result "claude" "http" "Wired into Claude Code via HTTP (fallback - shim tooling not found or shim install failed)."
            }
        } else {
            if (-not (Test-ClientTransport "claude" "http")) { return }
            claude mcp add --transport http --scope user pseudolife-memory http://127.0.0.1:8765/mcp
            Register-Result "claude" "http" "Wired into Claude Code via HTTP (-Transport http)."
        }
    }
}

foreach ($selectedClient in $clients) {
    if ($script:clientSetupFailures.ContainsKey($selectedClient)) {
        $mcpState[$selectedClient] = "failed"
        continue
    }
    if ($selectedClient -in "claude", "codex", "gemini" -and -not (Get-Command $selectedClient -ErrorAction SilentlyContinue)) {
        Write-ClientSetupFailure $selectedClient "The $selectedClient CLI is not on PATH. Install it and rerun, or drop it from -Client."
        continue
    }
    try {
        Install-SelectedClient $selectedClient
    } catch {
        Write-ClientSetupFailure $selectedClient "MCP setup failed. $($_.Exception.Message) Fix the reported problem and rerun the installer."
    }
}

# <<< client registrations <<<
# -- 12. health -----------------------------------------------------------------------
# >>> claude profile scope >>>
if ($mcpState["claude"] -and $mcpState["claude"] -notin "skipped", "failed") {
    $profileHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { $env:USERPROFILE }
    Write-Host "Claude Code user-profile MCP destination: $(Join-Path $profileHome '.claude.json')"
    Write-Host "Project/local registrations may take precedence over this user-scope file."
    Write-Host "MCP registration is per-profile; shared plugins or settings do not share memory tools."
    Write-Host "To add another profile, rerun this installer with CLAUDE_CONFIG_DIR set to that profile's directory; remove Env:CLAUDE_CONFIG_DIR for the default profile."
}
# <<< claude profile scope <<<

# >>> endpoint container probe >>>
# A server named on this machine's loopback is written as host.docker.internal
# (the extractor env block). Docker Desktop routes that name to this
# machine's loopback; Linux Docker Engine routes it to the docker bridge,
# where a server bound only to 127.0.0.1 is not listening. So once the
# daemon is up, the endpoint is asked from inside its container, the only
# place the answer means anything.
$script:endpointContainer = ""
function Test-LinuxHost { return [bool]$IsLinux }
function Invoke-EndpointContainerProbe {
    if ($Extractor -notin "endpoint", "endpoint-fallback") { return }
    $probeUrl = ConvertTo-ExtractorContainerUrl $ExtractorUrl
    if ($probeUrl -eq $ExtractorUrl) { return }
    if (Test-LinuxHost) {
        Step "Note: on Linux, host.docker.internal is the docker bridge address, so a server listening only on 127.0.0.1 is out of the daemon container's reach."
    }
    # Any HTTP answer counts: a 401 or 404 still means the server is there.
    $probe = "import sys, urllib.error, urllib.request`ntry:`n    urllib.request.urlopen(sys.argv[1] + '/models', timeout=10)`nexcept urllib.error.HTTPError:`n    pass"
    $global:LASTEXITCODE = 0
    try {
        if ($composeEngine -and $composeEngine -ne 'python') {
            # The Rust image has curl and no Python; without -f any HTTP status is 0.
            docker exec pseudolife-mcp-daemon curl -s -o /dev/null --max-time 10 "$probeUrl/models" 2>&1 | Out-Null
        } else {
            docker exec pseudolife-mcp-daemon python -c $probe $probeUrl 2>&1 | Out-Null
        }
    } catch { $global:LASTEXITCODE = 1 }
    if ($LASTEXITCODE -eq 0) {
        $script:endpointContainer = "reachable"
        Step "The daemon's container reaches the extractor endpoint at $probeUrl."
    } else {
        $script:endpointContainer = "unreachable"
        $until = if ($Extractor -eq "endpoint-fallback") { "dreams fall back to the sidecar" } else { "dreams pause" }
        Write-Warning "The daemon's container cannot reach the extractor endpoint at $probeUrl/models. A server on this machine must listen where the container can reach it, not only on 127.0.0.1: OLLAMA_HOST=0.0.0.0 (or the docker bridge address) for Ollama, llama-server --host <bridge address>, LM Studio's `"Serve on Local Network`". Restart the server so; the daemon retries every sweep, and until then $until."
    }
}
function Show-EndpointContainer {
    switch ($script:endpointContainer) {
        "reachable" { Write-Host "  [x] Extractor endpoint   reachable from the daemon's container" }
        "unreachable" { Write-Host "  [!] Extractor endpoint   not reachable from the daemon's container - see the warning above" }
    }
}
# <<< endpoint container probe <<<

# >>> test login >>>
# The test suite's own Postgres login (`pseudolife-mcp test-login create`,
# run by the installed native client): a role that creates and drops its own
# databases and cannot connect to the bank, written to
# ~/.pseudolife-mcp/test-pg.env, which this checkout's tests read before
# ops/.env. So no checkout or agent session needs ops/.env, which holds the
# bank owner's password (2026-10-04). Only with -TestLogin: an end user's
# server has no use for a CREATEDB password login (review, 2026-10-04).
# Idempotent: a re-run re-applies the same password. It runs psql in the
# Postgres container as its own superuser; a failure leaves the install
# working.
function Invoke-TestLoginSetup {
    if (-not (Install-ShimOnce) -or -not $script:shimInstallPath) {
        Write-Warning "The native client is unavailable (see above), so the test suite's Postgres login was not set up. Later: pseudolife-mcp test-login create"
        return
    }
    Step "Setting up the test suite's Postgres login (it cannot open the bank)..."
    $code = 1
    try {
        & $script:shimInstallPath test-login create
        $code = $LASTEXITCODE
    } catch { $code = 1 }
    if ($code -ne 0) {
        Write-Warning "The test suite's Postgres login was not set up (see above); the install is unaffected. Re-run later: & `"$($script:shimInstallPath)`" test-login create"
    }
}
# <<< test login <<<

# -- 12a. CLI shim autostart (Claude / Codex modes) -------------------------------
# Placed here, at the top of stage 12, for three reasons: after stage 11's
# shim install (below); before the health wait, so a daemon that is not yet
# healthy still leaves the autostart registered, as when this was stage 8;
# and outside every stage range the installer tests extract (they run
# stages 9 through the '12. health' header), so no test ever reaches the
# real systemctl / Task Scheduler calls below.
# >>> shim autostart >>>
# Runs after stage 11 on purpose: the native client installed there runs
# the extractor shim (`pseudolife-mcp extractor-shim`), so no interpreter is
# involved. Its default prompt is evals\prompts\sonnet_extractor_v5.md
# (rust/shim/src/extractor_shim/config.rs), read from this checkout via --repo.
# A mode switch must tear down the OTHER family's autostart: an abandoned
# shim task keeps making real CLI calls at every /health refresh, forever,
# on a plan whose owner believes it is turned off. Best-effort like the
# registration below. A task this user owns is removed without elevation;
# one registered from an elevated shell (Administrators own it) is left
# alone, read-only, with the one elevated command that removes it.
. (Join-Path $PSScriptRoot "scheduled_task_lock.ps1")
function Remove-ShimTask($name, $family) {
    if (-not (Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue)) { return }
    # A failed owner read is a refusal, never a fall-through to the delete:
    # from an elevated shell that delete could take another account's task.
    try { $lock = Get-TaskLock $name }
    catch {
        Write-Warning "could not read the owner of autostart task '$name' ($($_.Exception.Message)); nothing was changed, and its $family shim keeps starting at logon"
        return
    }
    if ($lock) {
        $prefix = if ($lock.Unresolved) { "the $family shim task runs as an account this shell could not resolve, so it was left alone" }
                  elseif ($lock.OtherUser) { "the $family shim task is another account's, so it was left alone" }
                  else { "its $family shim keeps starting at logon until the task is removed" }
        Write-Warning "${prefix}: $(Format-TaskLock $lock -Removal)"
        return
    }
    Unregister-ScheduledTask -TaskPath '\' -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    if (Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue) {
        Write-Warning "could not remove autostart task '$name' (Task Scheduler refused; see the message above) - its $family shim keeps starting at logon until you remove it: Unregister-ScheduledTask -TaskName '$name' -Confirm:`$false"
    } else {
        Step "Removed autostart task '$name' (the $family shim): extractor mode $Extractor does not use it, and an abandoned shim keeps making real CLI calls at every health refresh (a running shim process, if any, persists until logoff)"
    }
}
# A client-only install leaves any local daemon's extractor shims alone.
if (-not $codexShimMode -and -not $ClientOnly) { Remove-ShimTask "Pseudolife Codex Shim" "Codex" }
if (-not $claudeShimMode -and -not $ClientOnly) {
    Remove-ShimTask "Pseudolife Claude Shim" "Claude"
    Remove-ShimTask "Pseudolife Sonnet Shim" "Claude"   # pre-rename installs
}
$shimAutostartFamily = if ($claudeShimMode) { "claude" } elseif ($codexShimMode) { "codex" } else { $null }
if ($shimAutostartFamily) {
    $familyLabel = if ($shimAutostartFamily -eq "claude") { "Claude" } else { "Codex" }
    Step "Registering the $familyLabel shim autostart (Task Scheduler, for this user; no elevation needed)..."
    $autostartArgs = @("extractor-shim", "install-autostart", $shimAutostartFamily, "--repo", $repo, "--port", "$ShimPort", "--model", $Model)
    $rerun = "pseudolife-mcp " + ($autostartArgs -join " ")
    if (-not (Install-ShimOnce) -or -not $script:shimInstallPath) {
        Write-Warning "Shim autostart was not registered: the native client is unavailable (see above)."
        $script:extractorAutostartFailure = "Shim autostart was not registered because the native client is unavailable. Fix the native client install and rerun the installer with the same extractor mode."
        Write-Host "  Re-run later in a normal (not elevated) pwsh: $rerun"
    } else {
        $autostartCode = 1
        # The stable launcher, not the numbered runtime it starts: the task
        # then runs whatever release a later update installs.
        $autostartArgs += @("--native-exe", $script:shimInstallPath)
        try {
            & $script:shimInstallPath @autostartArgs
            $autostartCode = $LASTEXITCODE
        } catch { $autostartCode = 1 }
        if ($autostartCode -eq 3) {
            # The native client has already printed the task and its repair.
            if ($extractorRequested -and -not $restoredExtractor) {
                $script:extractorAutostartFailure = "The existing task was left in place because it was registered from an elevated shell or by another account. Follow the task recovery above and rerun the installer with the same extractor mode."
            }
        } elseif ($autostartCode -ne 0) {
            Write-Warning "Shim autostart registration or start failed (see the message above)."
            $script:extractorAutostartFailure = "Shim autostart registration or start failed. Fix the reported error and rerun the installer with the same extractor mode."
            Write-Host "  Re-run later in a normal (not elevated) pwsh once the message above is dealt with: & `"$($script:shimInstallPath)`" $($autostartArgs -join ' ')"
            # Only on a free port: on Windows a second shim fails to bind while one listens.
            if (-not (Get-NetTCPConnection -LocalPort $ShimPort -State Listen -ErrorAction SilentlyContinue)) {
                Write-Host "  Or start it manually: & `"$($script:shimInstallPath)`" extractor-shim $shimAutostartFamily --repo `"$repo`" --port $ShimPort --model $Model"
            }
        }
    }
}
# <<< shim autostart <<<
# A client-only install checked the remote daemon at preflight, and nothing
# starts locally, so there is nothing to wait for.
if (-not $ClientOnly) {
    if ($composePlan -ne 'unchanged') { Step "Waiting for the daemon to report healthy..." }
    $h = $null
    # Rust verifies its provisioned models before it serves: allow it longer.
    $healthTries = if ($composeEngine -and $composeEngine -ne 'python') { 120 } else { 40 }
    for ($i = 0; $i -lt $healthTries; $i++) {
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 3
            # Rust answers "ok" while it still initializes: wait for ready.
            if ($h.status -eq "ok" -and ($composeEngine -eq 'python' -or -not $composeEngine -or $h.ready -eq $true)) { break }
        } catch { }
        Start-Sleep -Milliseconds 1500
        $h = $null
    }
    if (-not $h) {
        Write-Warning "Daemon not healthy yet. Logs: docker logs pseudolife-mcp-daemon"
        exit 1
    }
    Step "Healthy: http://127.0.0.1:8765/health (Console: http://127.0.0.1:8765/ui/)"
    # >>> engine record >>>
    if ($composeEngine -eq 'rust') {
        # Recorded only now, as update records its own after its health gate.
        $engineStatus = "$(Invoke-EngineHelper accept)".Trim()
        if ($LASTEXITCODE -ne 0) { exit 1 }
        if ($engineStatus -eq 'accepted') { Step "Rust daemon selection recorded; pseudolife-mcp update keeps it" }
    } elseif ($Engine -eq 'python') {
        # Kept by later updates too: the G2 default never moves a chosen host.
        Invoke-EngineHelper choose --engine python | Out-Null
        if ($LASTEXITCODE -ne 0) { exit 1 }
    }
    # <<< engine record <<<
    Invoke-EndpointContainerProbe
    if ($TestLogin) { Invoke-TestLoginSetup }
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
        try {
            $global:LASTEXITCODE = 0
            & (Join-Path $PSScriptRoot "install-hook.ps1") -Client claude -Command "$shimCommand briefing --hook-json"
            if ($LASTEXITCODE -ne 0) { throw "Hook script exited $LASTEXITCODE." }
            $hookState["claude"] = "hook"
        } catch {
            $hookState["claude"] = "failed"
            Write-Warning "The Claude Code session hook could not be written. Check the settings file's JSON and permissions, then rerun the installer."
        }
    } else {
        Write-Warning "The Claude Code session hook was not installed: it runs the pseudolife-mcp shim, which is unavailable (see above)."
        $hookState["claude"] = "failed"
        $script:claudeHookRecovery = "Hooks were not installed because the native client is unavailable. Fix the native client install and rerun the installer."
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
        "present-current" { "already wired (unchanged; it runs the native client)" }
        "present-upgraded" { "already wired; it runs the native client from this release" }
        "present-migrated" { "already wired; registration moved to the native launcher (old file backed up)" }
        "present-custom" { "already wired with custom command (unchanged)" }
        "failed" { "registration or shim upgrade FAILED - see the warning above and re-run" }
        default { "not wired" }
    }
}
function Get-McpMarker($state) {
    if ($state -in "shim-env", "shim", "http", "present", "present-current", "present-upgraded", "present-migrated", "present-custom") { "[x]" } else { "[!]" }
}
function Describe-Instr($state) {
    if (-not $state) { return "[-] Standing file        skipped" }
    $tail = ($state -split ":", 2)[-1]
    switch -Wildcard ($state) {
        "appended:*" { "[x] Standing file        $tail" }
        "failed:*" { "[!] Standing file        could not write $tail" }
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
                Write-Host "    [!] Session briefing     not installed"
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
            $codexMarker = if ($codexRuntimeDefaults -eq "failed") { "[!]" } else { Get-McpMarker $mcpState['codex'] }
            Write-Host "    $codexMarker MCP transport        $(Describe-Mcp $mcpState['codex'])"
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
# Whether `pseudolife-mcp` in a new terminal reaches the launcher (the user
# PATH step in Install-ShimOnce).
if ($script:shimPathNotes.Count -gt 0) {
    $pathMarker = if ($script:shimPathState -eq "failed") { "[!]" } else { "[x]" }
    Write-Host "  $pathMarker Launcher on PATH     $($script:shimPathNotes[0])"
    foreach ($note in @($script:shimPathNotes | Select-Object -Skip 1)) { Write-Host "      $note" }
    Write-Host ""
}
# One line for the agent board, as the daemon answers it for the first client
# token file (else the daemon's singular token): on, or off and why.
# >>> board line >>>
$boardHint = switch ($tokenState) {
    "opted-out" { " (installed with -NoToken; re-run without it to turn the board on)" }
    "http" { " (-Transport http registrations cannot carry a token file)" }
    default { "" }
}
$boardFile = @($claudeTokenFile, $geminiTokenFile, $codexCredentialFile, $TokenFile) | Where-Object { $_ } | Select-Object -First 1
$boardArgs = @("credentials", "board", "--daemon-url", $clientDaemonUrl)
if ($boardFile) {
    $boardArgs += @("--token-file", $boardFile)
} else {
    # The singular token rides a process-scoped env var, never an argument.
    $env:PSEUDOLIFE_INSTALLER_TOKEN = Get-DesktopTokenSource
}
try {
    $boardLine = (Invoke-NativeHelper @boardArgs) -join " "
    if (($LASTEXITCODE -ne 0) -or -not $boardLine) { $boardLine = "unknown" }
} catch {
    $boardLine = "unknown"
} finally {
    $env:PSEUDOLIFE_INSTALLER_TOKEN = $null
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
Show-EndpointContainer
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
if ($script:bankLocation -eq "shared") { Show-SharedBankNotes; Write-Host "" }
if ($script:bankLocation -eq "shared") { Invoke-SharedBankExposeOffer }
if ($script:shimUpgradeHeld) { Write-Warning $script:shimUpgradeHeld }
# >>> record successful deployment >>>
if (-not $ClientOnly) {
    $recordArgs = @('record', '--expected-stdin', '--repo', $repo, '--env-file', $envFile, '--override', $overrideFile)
    if ($resolvedInstructionChoices['gemini']) { $recordArgs += @('--instruction-gemini', $resolvedInstructionChoices['gemini']) }
    if ($resolvedInstructionChoices['generic']) { $recordArgs += @('--instruction-generic', $resolvedInstructionChoices['generic']) }
    if ($resolvedGenericTarget) { $recordArgs += @('--generic-target', $resolvedGenericTarget) }
    $recordArgs += $engineStateArgs
    $composeSnapshotJson | Invoke-InstallerState @recordArgs | Out-Null
}
# <<< record successful deployment <<<
# >>> engine switch >>>
# After the record: the switch recreates the daemon, which the record checks.
if ($switchEngine) {
    $switchVersion = $null
    try { $switchVersion = (Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 3).version } catch { }
    $switchArgs = @('update', '--engine', $switchEngine, '--daemon-only')
    # (A checkout-mode Rust host never gets here: stage 4b refuses it.)
    if ($switchVersion) { $switchArgs += @('--tag', "$switchVersion") }
    else {
        # Without --tag, update would move to the newest release instead.
        [Console]::Error.WriteLine("The installed daemon was kept: its version could not be read from http://127.0.0.1:8765/health, so the switch to $switchEngine did not run. Run: pseudolife-mcp update --engine $switchEngine --daemon-only --tag <the daemon's version>")
        exit 1
    }
    if (-not (Install-ShimOnce) -or -not $script:shimInstallPath) {
        [Console]::Error.WriteLine("The installed daemon was kept; switching it to $switchEngine needs the pseudolife-mcp shim, which is unavailable (see above). Run: pseudolife-mcp $($switchArgs -join ' ')")
        exit 1
    }
    Step "Switching the daemon to $switchEngine (backup, rollback tag and health gate)..."
    # Stdin closed: update's own questions take their defaults, never hang.
    $null | & $script:shimInstallPath @switchArgs
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("The switch to $switchEngine failed; its output above says what changed and how to roll back. Retry with: $($script:shimInstallPath) $($switchArgs -join ' ')")
        exit 1
    }
}
# <<< engine switch <<<
if (-not $ClientOnly) { Invoke-MaintainerSetupOffer }
if (-not $ClientOnly) { Invoke-ConsoleSignin }
# >>> update line >>>
# The installers do not put the shim launcher's directory on PATH, so a bare
# `pseudolife-mcp` finds an older pipx install, or nothing: name the shim this
# run installed by its path until PATH finds it.
function Get-UpdateCommand([string]$ShimPath) {
    # The command, and a PATH note while PATH does not find the shim this run installed.
    $updCmd = "pseudolife-mcp"
    $updPath = ""
    if ($ShimPath) {
        $found = Get-Command pseudolife-mcp -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        $foundPath = if ($found) { (Resolve-Path -LiteralPath $found.Source).Path } else { $null }
        $comparison = if ($env:OS -eq "Windows_NT") { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
        if (-not ($foundPath -and [string]::Equals($foundPath, $ShimPath, $comparison))) {
            $updCmd = "& `"$ShimPath`""
            $updPath = " (add $(Split-Path -Parent $ShimPath) to PATH to run it as plain pseudolife-mcp)"
        }
    }
    return @($updCmd, $updPath)
}
function Write-UpdateLine([string]$ShimPath, [switch]$ClientOnly, [string]$PairingHintUrl = "", [switch]$Scheduled) {
    $updCmd, $updPath = Get-UpdateCommand $ShimPath
    if ($ClientOnly) {
        $lead = if ($Scheduled) {
            "The daily client update job keeps this machine's clients on the daemon's release; to update them now, no checkout needed:"
        } else {
            "Update this machine's clients later with one command, no checkout needed:"
        }
        Write-Host "$lead $updCmd update --clients-only (installs the daemon's release as a new shim runtime and refreshes the plugin cache; the daemon is updated on its own host)$updPath."
        if ($PairingHintUrl) {
            Write-Host "If another machine uses this same token, rotating it breaks both and neither can be revoked alone: give each machine its own with pseudolife-mcp invite <name> on the daemon's host and $updCmd connect $PairingHintUrl --read-code here. A machine paired with invite, or given a token no other machine uses, already has its own: nothing to do."
        }
    } else {
        Write-Host "Update later with one command: $updCmd update (--check reports whether a newer release exists)$updPath; from this checkout, ops\update.ps1 -All does the same after a git pull."
        Write-Host "Keep this checkout: ops/.env is this install's configuration and credential source, and extractor autostart may reference its files."
    }
}
# <<< update line <<<
# >>> client update schedule offer >>>
# A client-only machine's shim and plugin move to the daemon's release only
# when something moves them: `pseudolife-mcp update --schedule` registers the
# daily job that does (pseudolife_memory/unattended_update.py). That is a
# persistent job the user did not choose by running the installer, so it is
# asked once, default no, and -ClientUpdateSchedule answers it. 04:30 is an
# hour after the 03:30 the docs give a daemon host's own job, so a daemon
# updated overnight is matched the same morning. --schedule registers a
# Windows task here (a systemd --user timer from install.sh).
$ClientScheduleDefault = "04:30"
$ClientScheduleTask = "Pseudolife Client Update"   # unattended_update.CLIENT_TASK_NAME
$ClientScheduleContainer = "pseudolife-mcp-daemon"  # update_cli.DAEMON_CONTAINER
$script:clientScheduleState = ""
function Get-ClientScheduleJob {
    # The task --schedule registered: whether it is enabled, and the daemon
    # URL and time it runs with; $null when there is none.
    $xml = ""
    $global:LASTEXITCODE = 1
    try { $xml = (& schtasks /Query /TN $ClientScheduleTask /XML 2>$null | Out-String) } catch { $global:LASTEXITCODE = 1 }
    if ($LASTEXITCODE -ne 0) { return $null }
    # --schedule leaves the default daemon URL off the command line.
    $url = if ($xml -match '--daemon-url[= ]+"?([^\s"<]+)') { $Matches[1] } else { "http://127.0.0.1:8765" }
    $time = if ($xml -match '<StartBoundary>[^<T]*T(\d\d:\d\d)') { $Matches[1] } else { $null }
    # A disabled task, or a disabled trigger, never runs.
    $enabled = $xml -notmatch '<Enabled>\s*false\s*</Enabled>'
    return [pscustomobject]@{ Url = $url; Time = $time; Enabled = $enabled }
}
function Test-ClientScheduleContainer {
    # Whether a daemon container exists here, running or stopped.
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    $global:LASTEXITCODE = 1
    try { & docker inspect -f '{{.Id}}' $ClientScheduleContainer *> $null } catch { $global:LASTEXITCODE = 1 }
    return ($LASTEXITCODE -eq 0)
}
function Invoke-ClientUpdateScheduleOffer {
    $mode = if ("$ClientUpdateSchedule") { "$ClientUpdateSchedule".ToLowerInvariant() } else { "ask" }
    $url = "$clientDaemonUrl".TrimEnd("/")
    $explicitTime = $mode -notin "ask", "yes", "no"
    $time = $ClientScheduleDefault
    if ($explicitTime) {
        $hour, $minute = $mode.Split(":")
        $time = "{0:D2}:{1}" -f [int]$hour, $minute
    }
    $haveShim = (Install-ShimOnce) -and $script:shimInstallPath
    $updCmd = (Get-UpdateCommand $script:shimInstallPath)[0]
    $command = "$updCmd update --schedule $time --daemon-url $url"
    if ($mode -eq "no") {
        $script:clientScheduleState = "declined"
        Write-Host "Automatic client updates: not set up (-ClientUpdateSchedule no); $command sets them up later."
        return
    }
    if ($env:OS -ne "Windows_NT") {
        if ($mode -ne "ask") {
            Write-Host "Automatic client updates: -ClientUpdateSchedule $mode does nothing here, since the daily job needs Task Scheduler (Windows) or systemd (Linux, from ops/install.sh); run $updCmd update --clients-only after the daemon upgrades."
        }
        return
    }
    if (-not $haveShim) {
        $script:clientScheduleState = "failed"
        Write-Warning "The pseudolife-mcp shim is unavailable (see above), so automatic client updates were not set up: run pseudolife-mcp update --schedule $time --daemon-url $url once it is installed."
        return
    }
    # --schedule takes the client path only from a shim runtime.
    if ($script:shimManagerKind -ne "runtime") {
        $script:clientScheduleState = "skipped"
        Write-Host "Automatic client updates: not offered, since the shim is not installed as a shim runtime here (see above for why), and pseudolife-mcp update --schedule installs the client job only from one; re-run the installer once the runtime installs to be offered them."
        return
    }
    # With a daemon container here, even a stopped one, --schedule installs
    # the daemon host's job, which this question never offered.
    if (Test-ClientScheduleContainer) {
        $script:clientScheduleState = "skipped"
        Write-Host "Automatic client updates: not offered, since this machine still has a local daemon container ($ClientScheduleContainer), so pseudolife-mcp update --schedule here would install the daemon host's job rather than the client's; once that container is removed (docker rm $ClientScheduleContainer keeps its volumes and bank), $command sets them up."
        return
    }
    $job = Get-ClientScheduleJob
    if ($job -and $job.Enabled) {
        $at = if ($job.Time) { $job.Time } else { "its set time" }
        $where = "the scheduled task '$ClientScheduleTask'"
        $move = ""
        $moveAt = $time
        if ("$($job.Url)".TrimEnd("/") -ne $url) {
            $script:clientScheduleState = "elsewhere"
            $move = ", following the daemon at $($job.Url) rather than $url"
            if (-not $explicitTime -and $job.Time) { $moveAt = $job.Time }
        } else {
            $script:clientScheduleState = "scheduled"
            if ($explicitTime -and $time -ne $job.Time) { $move = ", rather than at $time" }
        }
        if ($move) {
            Write-Host "Automatic client updates: already scheduled (daily at $at, $where)$move; nothing changed: $updCmd update --schedule $moveAt --daemon-url $url moves it."
        } else {
            Write-Host "Automatic client updates: already scheduled (daily at $at, $where); nothing changed."
        }
        return
    }
    if ($job) {
        Write-Host "Automatic client updates: the scheduled task '$ClientScheduleTask' exists but is disabled, so it never runs."
    }
    if ($mode -eq "ask") {
        if (-not $interactive -or $script:installerRerun) {
            $script:clientScheduleState = "skipped"
            Write-Host "Automatic client updates: not set up (no terminal to ask on): $command sets them up, or re-run with -ClientUpdateSchedule yes."
            return
        }
        $reply = Read-BankAnswer "A daily job at $time can keep this machine's Pseudolife clients on the daemon's release: it runs pseudolife-mcp update --unattended, which moves the shim and plugin to the release the daemon at $url runs, never past it. Set it up? [y/N]"
        if ((Get-BankReply $reply) -cnotin "y", "Y", "yes", "Yes", "YES") {
            $script:clientScheduleState = "declined"
            Write-Host "  Not now: $command sets them up later (any time of day as HH:MM)."
            return
        }
    }
    try {
        $global:LASTEXITCODE = 0
        & $script:shimInstallPath update --schedule $time --daemon-url $url
        $scheduleExit = $LASTEXITCODE
    } catch {
        Write-Host "  $($_.Exception.Message)"
        $scheduleExit = 1
    }
    if ($scheduleExit -ne 0) {
        $script:clientScheduleState = "failed"
        Write-Warning "pseudolife-mcp update --schedule exited $scheduleExit (see its message above), so nothing was scheduled. The install itself is complete: run $command once that is fixed."
        return
    }
    $script:clientScheduleState = "scheduled"
}
# <<< client update schedule offer <<<
if ($ClientOnly) { Invoke-ClientUpdateScheduleOffer }
Write-UpdateLine -ShimPath $script:shimInstallPath -ClientOnly:([bool]$ClientOnly) -PairingHintUrl "$script:pairingHintUrl" -Scheduled:($script:clientScheduleState -eq "scheduled")
Write-Host "Optional ChatGPT access: follow the native tunnel profile steps in docs/guide/tunnels.md."
Remove-NativeStage
# >>> client setup result >>>
function Get-CodexHookCompletionReason {
    switch ($hookState["codex"]) {
        "pending" {
            if ($codexSetup.recovery -clike "PseudoLife hooks await approval.*") {
                "Hooks await approval. Open /hooks in the Codex CLI or Settings > Hooks in the Codex app. Review and trust the Pseudolife handlers, then start a new session."
            } else {
                "Hooks are not installed. Rerun the installer with -CodexHookTrust yes."
            }
        }
        "unavailable" {
            if ($codexSetup.recovery -clike "Codex hooks are disabled in configuration (features.hooks = false);*") {
                "Hooks are disabled in Codex. Set features.hooks = true in the active Codex configuration and rerun the installer."
            } else {
                "Hooks could not be set up. Fix the hook problem reported above and rerun the installer."
            }
        }
    }
}
function Get-ClientSetupWarnings {
    $script:httpCredentialWarnings
    if ($script:legacyClaude -eq "error") { "Claude Code legacy hook cleanup failed. See the repair above." }
    if (-not $script:clientSetupFailures.ContainsKey("codex") -and
        ($hookState["codex"] -eq "pending" -or
         ($CodexHooks -eq "skip" -and $hookState["codex"] -eq "unavailable"))) {
        "Codex hook setup needs attention. $(Get-CodexHookCompletionReason)"
    }
    if ($script:codexRuntimeDefaults -eq "failed") { "Codex runtime defaults were not confirmed. $script:codexRuntimeRecovery" }
    if ($script:geminiGuardWarning) { "Gemini CLI stdio guard was not verified. Check the pseudolife-memory entry in ~/.gemini/settings.json." }
    if ($script:geminiCredentialWarning) { $script:geminiCredentialWarning }
    if ($script:codexInstructionWarning -or $codexSetup.instructions_error) { "Codex standing instructions could not be written. Check the Codex home and file permissions." }
    foreach ($state in $instrState.Values) {
        if ($state -like "failed:*") { "Standing instructions could not be written to $($state.Substring(7)). Check permissions and rerun." }
    }
    if ($script:extractorAutostartFailure -and -not $extractorRequested) {
        "Extractor setup incomplete ($Extractor). $script:extractorAutostartFailure"
    }
    if ($script:clientScheduleState -eq "failed") { "Automatic client updates were not scheduled. See the scheduling command above." }
    if ($script:consoleSigninWarning) { "Console sign-in did not finish. Run pseudolife-mcp console once the reported problem is fixed." }
}
function Get-ClientCompletionReasons([string]$Provider, [string]$ConnectionFailure) {
    switch ($Provider) {
        "claude" {
            switch ($script:pluginClaude) {
                "failed" { "Plugin was not installed. $($script:pluginClaudeRecovery)" }
                "no-cli" { "Plugin was not installed because the claude CLI is not on PATH. Put the Claude Code CLI on PATH and rerun the installer." }
            }
            if ($hookState["claude"] -eq "failed") {
                if ($script:claudeHookRecovery) { $script:claudeHookRecovery }
                else { "Hooks were not installed. Fix the hook error above and rerun the installer." }
            }
            if ($script:clientHookSettingsWarning) {
                "Hook credentials were not written. Set the daemon URL and token file in the settings file's env block as shown above."
            }
        }
        "codex" {
            if (-not $ConnectionFailure -and $CodexHooks -ne "skip" -and $hookState["codex"] -eq "unavailable") {
                Get-CodexHookCompletionReason
            }
        }
    }
    if ($ConnectionFailure) { $ConnectionFailure }
}
$clientWarnings = @(Get-ClientSetupWarnings)
$failedRequests = @()
foreach ($provider in $clients) {
    $label = switch ($provider) {
        "claude" { "Claude Code" }
        "claude-desktop" { "Claude Desktop" }
        "codex" { "Codex" }
        "gemini" { "Gemini CLI" }
    }
    foreach ($reason in Get-ClientCompletionReasons $provider $script:clientSetupFailures[$provider]) {
        $reason = $reason -replace '[\r\n]+', ' '
        if ($requestedClients -contains $provider) {
            $failedRequests += "Requested client setup incomplete ($label). $reason"
        } else {
            $clientWarnings += "Detected client setup incomplete ($label). $reason"
        }
    }
}
if ($extractorRequested -and $script:extractorAutostartFailure) {
    $failedRequests += "Requested extractor setup incomplete ($Extractor). $script:extractorAutostartFailure"
}
if ($clientWarnings.Count) {
    [Console]::Error.WriteLine("Warnings")
    foreach ($warning in $clientWarnings) { [Console]::Error.WriteLine($warning) }
}
if ($failedRequests.Count) {
    $script:nativeRunDone = $true
    foreach ($failure in $failedRequests) { [Console]::Error.WriteLine($failure) }
    exit 5
}
$script:nativeRunDone = $true
Write-Host "Done. First session: tell your coding agent to remember something."
# <<< client setup result <<<
} finally {
    Remove-NativeStage
    # An exit after the run's first change to the host (an exit, an error or
    # Ctrl-C) says the run stopped part way; nothing is undone.
    if ($script:nativeHostChanging -and -not $script:nativeRunDone -and -not $script:nativePartWaySaid) {
        $part = "The install stopped part way: what the lines marked ==> above report stays as it is"
        if ($script:nativeInstalledAt) { $part += ", the native client among it (installed at $($script:nativeInstalledAt))" }
        [Console]::Error.WriteLine("$part. Rerun the installer to finish, once the cause this run reports is fixed.")
    }
}
