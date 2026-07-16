#Requires -Version 7
# Preflight / doctor: CHECK-ONLY prerequisite audit for a fresh install
# (issue #13 — converts "mysterious failure" into "run this line"). Verifies
# each prerequisite and prints the exact remediation for anything missing;
# never installs or changes anything. Exit 0 = ready to install.
#
#   ops\preflight.ps1

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

# -- ports 8765 (daemon) / 5433 (postgres): free, or held by our own stack -----
# Warn-only: a taken port turns into a cryptic "port is already allocated" at
# compose up; held-by-us means an existing install (idempotent re-run is fine).
$running = @(docker ps --format "{{.Names}}" 2>$null)
foreach ($p in @(@{Port=8765; Svc="daemon"; Cont="pseudolife-mcp-daemon"},
                 @{Port=5433; Svc="postgres"; Cont="pseudolife-mcp-postgres"})) {
    if ($running -contains $p.Cont) {
        Ok "port $($p.Port) held by $($p.Cont) (existing install)"
    } elseif (Get-NetTCPConnection -LocalPort $p.Port -State Listen -ErrorAction SilentlyContinue) {
        Warn "port $($p.Port) is already in use (needed for the $($p.Svc))" `
             "free the port (e.g. a native Postgres on 5433), then re-run - compose up will otherwise fail with 'port is already allocated'"
    } else {
        Ok "port $($p.Port) free ($($p.Svc))"
    }
}

# -- git ----------------------------------------------------------------------
if (Get-Command git -ErrorAction SilentlyContinue) { Ok "git" }
else { Fail "git is not installed" "https://git-scm.com/downloads or: winget install Git.Git" }

# -- python 3 (only needed for the optional Sonnet shim + eval tooling) --------
if ((Get-Command python -ErrorAction SilentlyContinue) -or (Get-Command python3 -ErrorAction SilentlyContinue)) {
    Ok "python 3 (optional Sonnet shim)"
} else {
    Fail "python 3 not found (optional - needed only for the Sonnet shim)" `
         "https://www.python.org/downloads/ or: winget install Python.Python.3.12"
}

# -- claude CLI ----------------------------------------------------------------
if (Get-Command claude -ErrorAction SilentlyContinue) { Ok "claude CLI" }
else {
    Fail "claude CLI not found" `
         "npm install -g @anthropic-ai/claude-code   (needs Node; see https://docs.anthropic.com/en/docs/claude-code)"
}

Write-Host ""
if ($script:fails -eq 0) {
    Write-Host "All checks passed - follow the README Quickstart."
} else {
    Write-Host "$($script:fails) check(s) failed - run the fix line(s) above, then re-run ops\preflight.ps1."
    exit 1
}
