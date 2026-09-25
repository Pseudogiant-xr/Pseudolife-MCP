#Requires -Version 7
# Safely update ONLY the Pseudolife-MCP daemon to the current checkout code.
# (v7 guard: Windows PowerShell 5.1 turns benign native stderr — e.g. docker
# inspecting a not-yet-built image tag — into a terminating NativeCommandError.)
#
#   ops\update.ps1                 # backup -> tag rollback -> daemon-only rebuild -> health
#   ops\update.ps1 -Tag pre-x      # name the rollback image tag suffix
#   ops\update.ps1 -NoBackup       # skip the pg_dump (NOT recommended)
#   ops\update.ps1 -KeepRollbacks 5  # rollback tags to retain (default 2)
#   ops\update.ps1 -KeepCacheHours 24 # build cache to retain, hours (default 168)
#   ops\update.ps1 -NoCachePrune     # skip build-cache retention entirely
#   ops\update.ps1 -HealthRetries 30 -HealthDelayMs 1500  # health-wait budget
#   ops\update.ps1 -ForceRollbackTag # tag the rollback even when the version
#                                    # tag is not the running daemon's image
#   ops\update.ps1 -All              # after the daemon: shim, plugin cache,
#                                    # Codex hooks (ops/update_clients.py)
#   ops\update.ps1 -AllowDirty       # deploy a tree with uncommitted or
#                                    # untracked files (stamped dirty=true),
#                                    # or one git cannot describe (unknown)
#
# Rebuilds + recreates ONLY the daemon container (`--no-deps`), so Postgres and
# the extractor are never touched. The bank lives in EXTERNAL volumes; this never
# runs `down -v`. Run after `git pull` to deploy daemon changes; local edits must
# be committed first (or deployed with -AllowDirty).
param(
    [string]$Tag = "",
    [switch]$NoBackup,
    # Override for the "a build already ran without a completed deploy" guard
    # in step 2 — see the comment there before reaching for it.
    [switch]$ForceRollbackTag,
    [int]$KeepRollbacks = 2,
    [int]$KeepCacheHours = 168,
    [switch]$NoCachePrune,
    # Health-wait budget (step 4). The defaults reproduce the previously
    # hard-coded loop exactly — 30 attempts, 1.5s apart, so ~45s before a
    # deploy is called unhealthy. Exposed so the unhealthy branch can be
    # driven in a test without spending 45 seconds per scenario; there is no
    # reason to lower them on a real deploy.
    [int]$HealthRetries = 30,
    [int]$HealthDelayMs = 1500,
    # Also move the client side once the daemon is healthy: the shim behind
    # each registration, the Claude Code plugin cache (compared by bytes
    # against the marketplace clone) and Codex's hook copy — ops/update_clients.py.
    [switch]$All,
    # Override for the clean-tree guard in step 0 — see the comment there.
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

# Colored step lines when interactive (NO_COLOR suppresses; the literal `==>`
# prefix survives either way for log greps). Escapes are generated
# ([char]27), never raw ESC bytes.
$Esc = [char]27
$stepColor = [Environment]::UserInteractive -and
    -not [Console]::IsOutputRedirected -and -not $env:NO_COLOR
function Step($msg) {
    if ($stepColor) { Write-Host "${Esc}[1;36m==>${Esc}[0m $msg" }
    else { Write-Host "==> $msg" }
}

$repo = Split-Path -Parent $PSScriptRoot

# 0. Build stamp + clean-tree guard, before anything has side effects. The
#    image records the commit it was built from (labels + /health `build`),
#    which is only true if the tree IS that commit. The maintainer's main
#    checkout often carries another session's uncommitted work (2026-09-23
#    review), and a deploy from it would ship that work under a commit that
#    does not contain it. A tree whose state git cannot report is refused
#    the same way. Step 3 probes again just before the build.
function Get-TreeState {
    # HEAD and `git status`, or git's own reason it could not report them
    # (a missing repository, or safe.directory refusing a foreign owner).
    # Only stdout is data: on success git can still write warnings or
    # GIT_TRACE lines to stderr, which must not read as dirty paths. Its
    # stderr is collected only once a command has failed.
    $state = @{ Sha = "unknown"; Dirty = "unknown"; Lines = @(); Error = "git is not on PATH" }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $state }
    $head = @(git -C $repo rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0) {
        $state.Error = ((git -C $repo rev-parse HEAD 2>&1 | ForEach-Object { "$_" }) -join " ").Trim()
        return $state
    }
    $lines = @(git -C $repo status --porcelain --untracked-files=normal 2>$null)
    if ($LASTEXITCODE -ne 0) {
        $state.Error = ((git -C $repo status --porcelain 2>&1 | ForEach-Object { "$_" }) -join " ").Trim()
        return $state
    }
    $state.Sha = $head[0].Trim()
    $state.Lines = $lines
    $state.Dirty = if ($lines.Count -gt 0) { "true" } else { "false" }
    $state.Error = $null
    return $state
}
function Write-TreeLines($lines) {
    $lines | Select-Object -First 20 | ForEach-Object { Write-Warning "    $_" }
    if ($lines.Count -gt 20) { Write-Warning "    ... and $($lines.Count - 20) more" }
}
$tree = Get-TreeState
if ($tree.Dirty -ne "false") {
    $why = if ($tree.Dirty -eq "true") {
        "$repo has $($tree.Lines.Count) uncommitted or untracked path(s):"
    } else {
        "cannot tell whether $repo is a clean git checkout: $($tree.Error)"
    }
    if (-not $AllowDirty) {
        Write-Warning "REFUSING to deploy: $why"
        Write-TreeLines $tree.Lines
        Write-Warning "Commit, stash or remove the listed paths, or re-run with -AllowDirty to deploy this tree as it is (stamped dirty, or unknown when git cannot describe it)."
        exit 1
    }
    Write-Warning "-AllowDirty: deploying anyway. $why"
    Write-TreeLines $tree.Lines
}
$buildSha = $tree.Sha
$buildDirty = $tree.Dirty
# InvariantCulture: a custom format's ':' is the culture's time separator
# otherwise ('.' under fi-FI), which is not an RFC 3339 timestamp.
$buildStamp = [ordered]@{
    PSEUDOLIFE_BUILD_GIT_SHA = $buildSha
    PSEUDOLIFE_BUILD_DIRTY = $buildDirty
    PSEUDOLIFE_BUILD_TIME = [DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'",
        [Globalization.CultureInfo]::InvariantCulture)
}

$composeFile = Join-Path $repo "ops\docker-compose.yml"
$envFile = Join-Path $repo "ops\.env"
$overrideFile = Join-Path $repo "ops\docker-compose.override.yml"
$compose = @("-f", $composeFile)
# Scaffold the (gitignored) machine-local env from the example so its knobs
# are discoverable — every line ships commented, so this changes nothing.
$exampleFile = Join-Path $repo "ops\.env.example"
if (-not (Test-Path $envFile) -and (Test-Path $exampleFile)) {
    Copy-Item $exampleFile $envFile
    Step "Scaffolded ops/.env from ops/.env.example (all values commented)."
}
# Machine-local overrides (e.g. a fine-tuned GGUF mount) live in the gitignored
# override file; explicit -f disables compose's auto-merge, so add it here.
if (Test-Path $overrideFile) { $compose += @("-f", $overrideFile) }
if (Test-Path $envFile) { $compose = @("--env-file", $envFile) + $compose }

# 1. Backup the bank (pg_dump inside the container) — the always-first rule.
if (-not $NoBackup) {
    Step "Backing up the bank (pg_dump)..."
    & (Join-Path $PSScriptRoot "backup.ps1")
} else {
    Write-Warning "Skipping backup (-NoBackup)."
}

# 2. Tag the current daemon image so a bad build can be rolled back. The tag
#    is read from the compose file so this script never drifts from it.
$imageTag = (Select-String -Path $composeFile -Pattern 'image:\s*(pseudolife-daemon:\S+)').Matches[0].Groups[1].Value
if (-not $imageTag) { throw "could not find the pseudolife-daemon image tag in $composeFile" }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $Tag) { $Tag = "pre-update-$stamp" }
$rollback = "$imageTag-$Tag"
$daemonContainer = "pseudolife-mcp-daemon"
# The rollback tag is only worth anything if it points at the LAST-GOOD
# image, and the version tag alone is not proof of that: `docker compose up
# --build` below builds FIRST and the deploy is validated AFTER, so a run
# that aborts in between leaves the version tag on a freshly built, never
# validated image. Re-running update.ps1 — the obvious next move — then
# tagged THAT as the rollback and destroyed the only pointer to the last-good
# image (this happened live on 2026-08-13).
#
# The running daemon container still holds the image that was actually
# deployed, so the two IDs disagreeing IS that situation. Known blind spot:
# a build that recreated the container and then failed its health check
# leaves both IDs on the new image — that path exits with the rollback
# instructions already printed, and is meant to be acted on then.
$tagImageId = docker image inspect -f '{{.Id}}' $imageTag 2>$null
$imagePresent = (($LASTEXITCODE -eq 0) -and $tagImageId)
# `inspect` answers for a STOPPED container too — it reports the image that
# container was created from, which is exactly the deployed image — so
# deploying from a stopped stack is still guarded. Only a container that does
# not exist at all (fresh install, or it was removed) leaves nothing to
# compare, and that case keeps the pre-guard behavior.
$runningImageId = docker inspect -f '{{.Image}}' $daemonContainer 2>$null
if ($LASTEXITCODE -ne 0) { $runningImageId = "" }

$rollbackState = "none"
if (-not $imagePresent) {
    Write-Warning "No current $imageTag image to tag (first build, or the version was bumped before this image was ever built)."
    Write-Warning "This deploy has NO rollback image. Rolling back means rebuilding the previous code."
} elseif ($runningImageId -and ($runningImageId -ne $tagImageId) -and (-not $ForceRollbackTag)) {
    $rollbackState = "kept"
    Write-Warning "REFUSING to move the rollback tag: $imageTag is NOT the image the running daemon deployed ($runningImageId vs $tagImageId)."
    Write-Warning "That means a build already ran without a completed deploy, so tagging it now would overwrite the last-good rollback with an unvalidated image."
    Write-Warning "Existing rollback tags are untouched. Re-run with -ForceRollbackTag once you are sure $imageTag IS the image you would want to roll back to."
} else {
    docker tag $imageTag $rollback
    $rollbackState = "tagged"
    Step "Tagged rollback image: $rollback"
    if ($ForceRollbackTag -and $runningImageId -and ($runningImageId -ne $tagImageId)) {
        Write-Warning "-ForceRollbackTag: tagged $imageTag even though the running daemon deployed a different image."
    }
}

# Rollback instructions are derived from whether the tag actually exists.
# They used to be printed unconditionally, so a skipped tag produced a
# command that fails — worst of all on the unhealthy path below, where the
# operator reaches for it precisely because the deploy just broke.
$rollbackLines = switch ($rollbackState) {
    "tagged" {
        @("      docker tag $rollback $imageTag",
          "      docker compose -f `"$composeFile`" up -d --no-deps pseudolife-daemon")
    }
    "kept" {
        @("      (the rollback tag was NOT moved this run - see the warning above)",
          "      Pick the newest surviving rollback tag and redeploy it:",
          "      docker image ls $(($imageTag -split ':')[0])",
          "      docker tag <that tag> $imageTag",
          "      docker compose -f `"$composeFile`" up -d --no-deps pseudolife-daemon")
    }
    default {
        @("      (no rollback image exists for this deploy - nothing was tagged)",
          "      Rebuild the last-good code instead, e.g.:",
          "      git checkout master; ops\update.ps1")
    }
}

# 2b. Retention: drop stale pre-* rollback tags beyond the newest N — one is
#     minted per deploy and they otherwise pile up without bound (~60 tags in
#     a 177GB docker_data.vhdx by 2026-07-14). The script never touches the
#     deployed tag or an image a running container uses; a retention hiccup
#     must not abort the deploy.
try {
    & (Join-Path $PSScriptRoot "prune-rollbacks.ps1") -Keep $KeepRollbacks -Repository ($imageTag -split ':')[0]
} catch {
    Write-Warning "Rollback-tag retention failed (deploy continues): $_"
}

# 3. Rebuild + recreate ONLY the daemon. `--no-deps` is what keeps Postgres and
#    the extractor untouched (without it, `up --build <svc>` recreates all three).
Step "Rebuilding the daemon only (Postgres + extractor untouched)..."
# Explicit machine-local authentication must not be shadowed by credentials
# inherited from a client process. Restore that process environment even if
# Compose fails; only this deployment invocation uses the file's authority.
$savedAuthEnvironment = @{}
# The step-0 build stamp reaches the build through compose's
# ${PSEUDOLIFE_BUILD_*} args, scoped to this invocation like the credentials.
$savedBuildEnvironment = @{}
try {
    foreach ($name in $buildStamp.Keys) {
        $savedBuildEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $buildStamp[$name], 'Process')
    }
    if (Test-Path -LiteralPath $envFile) {
        $authLines = [IO.File]::ReadAllLines($envFile)
        if ($authLines -match '^\s*(?:export\s+)?PSEUDOLIFE_MCP_TOKENS?\s*=') {
            foreach ($name in @('PSEUDOLIFE_MCP_TOKEN', 'PSEUDOLIFE_MCP_TOKENS')) {
                $originalVariable = Get-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
                $savedAuthEnvironment[$name] = @{
                    Present = $null -ne $originalVariable
                    Name = if ($null -ne $originalVariable) { $originalVariable.Name } else { $name }
                    Value = [Environment]::GetEnvironmentVariable($name, 'Process')
                }
                # Passing $null to SetEnvironmentVariable can leave an empty
                # native-child value, which still overrides Compose's env file.
                Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
            }
        }
    }
    # The backup and the rollback tag took a while; in a checkout that other
    # sessions share, HEAD or the tree may have moved since step 0, and the
    # stamp would then describe a different tree than the one being built.
    $now = Get-TreeState
    if ($now.Sha -ne $tree.Sha -or $now.Dirty -ne $tree.Dirty) {
        throw "the checkout changed during this deploy (HEAD $($tree.Sha) -> $($now.Sha), dirty $($tree.Dirty) -> $($now.Dirty)); nothing was built. Re-run the deploy."
    }
    Step "Build stamp: commit $buildSha, dirty=$buildDirty"
    docker compose @compose up -d --no-deps --build pseudolife-daemon
    if ($LASTEXITCODE -ne 0) { throw "daemon rebuild failed" }
} finally {
    foreach ($name in $savedBuildEnvironment.Keys) {
        if ($null -eq $savedBuildEnvironment[$name]) {
            Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $savedBuildEnvironment[$name], 'Process')
        }
    }
    foreach ($name in $savedAuthEnvironment.Keys) {
        $original = $savedAuthEnvironment[$name]
        if ($original.Present) {
            [Environment]::SetEnvironmentVariable($original.Name, $original.Value, 'Process')
        } else {
            Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
        }
    }
}

# 4. Wait for health.
Step "Waiting for the daemon to report healthy..."
$h = $null
for ($i = 0; $i -lt $HealthRetries; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 3
        if ($h.status -eq "ok") { break }
    } catch { Start-Sleep -Milliseconds $HealthDelayMs }
    $h = $null
}
if ($h) {
    Step "Healthy. schema=$($h.schema) persist_errors=$($h.persist_errors)"
    Write-Host "    Rolled-back deploy if ever needed:"
    $rollbackLines | ForEach-Object { Write-Host $_ }
} else {
    Write-Warning "Daemon did not report healthy. Logs: docker logs pseudolife-mcp-daemon"
    Write-Warning "To roll back:"
    $rollbackLines | ForEach-Object { Write-Warning $_ }
    exit 1
}

# 4b. -All: the client side. The daemon is deployed either way; a client
#     step that fails is reported, never a failed deploy.
if ($All) {
    Step "Updating the client side (shim, plugin cache, Codex hooks)..."
    $python = $null
    foreach ($candidate in @("python", "python3", "py")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { $python = $candidate; break }
    }
    if (-not $python) {
        Write-Warning "No python on PATH; run it yourself: python ops/update_clients.py"
    } else {
        $pyArgs = @()
        if ($python -eq "py") { $pyArgs = @("-3") }
        & $python @pyArgs (Join-Path $PSScriptRoot "update_clients.py") --repo $repo
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "A client-side step needs attention (see the ladder above); the daemon deploy itself succeeded."
        }
    }
}

# 5. Build-cache retention. Deliberately LAST, for two reasons: before the
#    build it would delete the cache the build reuses (cold-starting every
#    deploy), and on the unhealthy path above it would strip the cache an
#    operator's rollback rebuild wants — that branch exits, so this is
#    skipped for free. A retention hiccup must never fail a good deploy.
if (-not $NoCachePrune) {
    try {
        & (Join-Path $PSScriptRoot "prune-build-cache.ps1") -MaxAgeHours $KeepCacheHours
    } catch {
        Write-Warning "Build-cache retention failed (deploy already succeeded): $_"
    }
}
