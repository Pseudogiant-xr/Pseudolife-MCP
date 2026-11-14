#Requires -Version 7
# Preflight / doctor: CHECK-ONLY prerequisite audit for a fresh install
# (issue #13 — converts "mysterious failure" into "run this line"). Verifies
# each prerequisite and prints the exact remediation for anything missing;
# never installs or changes anything. Exit 0 = ready to install.
#
#   ops\preflight.ps1 -Client claude|claude-desktop|codex|gemini|generic
#   (comma/space list; aliases: both = claude,codex — all = claude,codex,gemini)

param(
    # Comma/space-separated provider list — validated below, not by
    # ValidateSet, which cannot express a list (install.ps1 passes e.g.
    # "claude,gemini").
    [string]$Client = "claude"
)

$checks = @()
foreach ($tok in ($Client -split '[,\s]+' | Where-Object { $_ })) {
    switch ($tok) {
        "both" { $checks += @("claude", "codex") }
        "all" { $checks += @("claude", "codex", "gemini") }
        { $_ -in "claude", "codex", "gemini" } { $checks += $_ }
        "generic" { }  # no CLI to probe — its MCP config is pasted by hand
        "claude-desktop" { }  # no CLI to probe: the native client writes its config
        default {
            Write-Host "invalid -Client '$tok' (claude|claude-desktop|codex|gemini|generic|both|all)"
            exit 2
        }
    }
}

$script:fails = 0

function Ok($msg)   { Write-Host "  OK   $msg" -ForegroundColor Green }
function Warn($msg, $fix) {
    Write-Host "  WARN $msg" -ForegroundColor Yellow
    Write-Host "        fix: $fix"
}
function Fail($msg, $fix) {
    Write-Host "  FAIL $msg" -ForegroundColor Red
    Write-Host "        fix: $fix"
    $script:fails++
}

Write-Host "Pseudolife-MCP preflight (checks only - nothing is installed or changed)"

# -- docker: installed + daemon reachable ------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "docker is not installed" `
         "install Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/"
} else {
    docker info *> $null
    if ($LASTEXITCODE -eq 0) {
        Ok "docker installed, daemon reachable"
    } else {
        Fail "docker daemon is not running" "start Docker Desktop and wait for the whale icon to settle"
    }
}

# -- docker compose v2 --------------------------------------------------------
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker compose version *> $null
    if ($LASTEXITCODE -eq 0) { Ok "docker compose v2" }
    else { Fail "docker compose v2 plugin missing" "Docker Desktop bundles it - update Docker Desktop" }
}

# -- docker buildx (BuildKit, which the image build needs) ---------------------
# The Dockerfiles use RUN --mount caches, which only BuildKit runs; without
# the buildx plugin compose falls back to the classic builder and the build
# dies with "the --mount option requires BuildKit".
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker buildx version *> $null
    if ($LASTEXITCODE -eq 0) { Ok "docker buildx (BuildKit image builds)" }
    else { Fail "docker buildx plugin missing - the image build needs BuildKit for its RUN --mount caches" "Docker Desktop bundles it - update Docker Desktop" }
}

# -- ports, volumes and Compose ownership: fail before any installer writes ----
# The native client's check (`installer preflight-check`): the binary named by
# PSEUDOLIFE_NATIVE_CLI (the installer passes its verified copy), else
# PSEUDOLIFE_SHIM_LAUNCHER, else the stable launcher, the order ops\update.ps1
# uses.
$nativeCli = $env:PSEUDOLIFE_NATIVE_CLI
if (-not $nativeCli) { $nativeCli = $env:PSEUDOLIFE_SHIM_LAUNCHER }
if (-not $nativeCli) {
    if ($IsWindows -or ($null -eq $IsWindows)) {
        $localData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME 'AppData/Local' }
        $nativeCli = Join-Path $localData 'pseudolife-mcp/bin/pseudolife-mcp.exe'
    } else {
        $dataHome = if ($env:XDG_DATA_HOME) { $env:XDG_DATA_HOME } else { Join-Path $HOME '.local/share' }
        $nativeCli = Join-Path $dataHome 'pseudolife-mcp/bin/pseudolife-mcp'
    }
}
if (-not (Test-Path -LiteralPath $nativeCli -PathType Leaf)) {
    # Run by hand before an install: the installer verifies the client, then
    # runs this check before it changes anything.
    Warn "the Docker ownership check needs the native pseudolife-mcp client, which is not installed yet ($nativeCli)" "ops\install.ps1 downloads and verifies it, then runs this check before anything changes (or set PSEUDOLIFE_NATIVE_CLI to a client)"
} else {
    & $nativeCli installer preflight-check --repo (Split-Path -Parent $PSScriptRoot) --env-file (Join-Path $PSScriptRoot ".env")
    if ($LASTEXITCODE -eq 2) {
        Fail "the native client at $nativeCli is older than this checkout and has no Docker ownership check" "run ops\install.ps1, which installs this checkout's client first"
        exit 1
    }
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

# -- git ----------------------------------------------------------------------
if (Get-Command git -ErrorAction SilentlyContinue) { Ok "git" }
else { Fail "git is not installed" "https://git-scm.com/downloads or: winget install Git.Git" }

# -- selected MCP client CLI(s) -------------------------------------------------
if ($checks -contains "claude") {
    if (Get-Command claude -ErrorAction SilentlyContinue) { Ok "claude CLI" }
    else {
        Fail "claude CLI not found" `
             "npm install -g @anthropic-ai/claude-code   (needs Node; see https://docs.anthropic.com/en/docs/claude-code)"
    }
}
if ($checks -contains "codex") {
    # The official Windows installer keeps codex.exe in a rotating
    # %LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\ dir and NOT on PATH (verified
    # live 2026-08-31) — Get-Command alone would fail a working install.
    if ((Get-Command codex -ErrorAction SilentlyContinue) -or
        (Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin\*\codex.exe" -ErrorAction SilentlyContinue)) {
        Ok "codex CLI"
    } else {
        Fail "codex CLI not found" `
             "install Codex: https://developers.openai.com/codex/cli/"
    }
}
if ($checks -contains "gemini") {
    if (Get-Command gemini -ErrorAction SilentlyContinue) { Ok "gemini CLI" }
    else {
        Fail "gemini CLI not found" `
             "npm install -g @google/gemini-cli   (needs Node; see https://geminicli.com/docs/)"
    }
}

Write-Host ""
if ($script:fails -eq 0) {
    Write-Host "All checks passed - follow the README Quickstart."
    # Explicit: `&` on a .ps1 only refreshes the caller's $LASTEXITCODE when
    # the script exits by `exit` — falling off the end would leave whatever a
    # native probe above set.
    exit 0
} else {
    Write-Host "$($script:fails) check(s) failed - run the fix line(s) above, then re-run ops\preflight.ps1."
    exit 1
}
