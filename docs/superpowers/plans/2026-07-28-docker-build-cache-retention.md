# Docker Build-Cache Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `docker_data.vhdx` ballooning by giving the BuildKit build cache an automatic retention policy, delivered through both the deploy path and a weekly scheduled task.

**Architecture:** One primitive script (`ops/prune-build-cache.ps1` + `.sh`) runs an age pass, then a size-ceiling pass only if still over cap, then a Windows-only `fstrim`. Two triggers call it: a non-fatal hook in `ops/update.ps1|.sh` placed *after* the health check, and a weekly Windows Scheduled Task. The offline vhdx compact stays manual and is shipped as a documented, de-identified ops script.

**Tech Stack:** PowerShell 7, bash, Docker CLI 29.6.2 (BuildKit), Windows Task Scheduler, pytest driving real scripts with a stubbed `docker`.

**Spec:** `docs/superpowers/specs/2026-07-28-docker-build-cache-retention-design.md`

## Global Constraints

- **Never** emit `docker system prune`, `docker rmi`, `docker image rm`, or any `docker volume` command from any script in this plan. The only prune verb permitted is `docker builder prune`.
- **Never** touch the `ops_pseudolife_data` or `ops_pseudolife_pgdata` volumes.
- `--keep-storage` does not exist on Docker 29.6.2. Use `--max-used-space` (bytes).
- The build-cache prune runs **after** the build, never before — pruning before `docker compose up --build` deletes the cache that build reuses.
- PowerShell scripts start with `#Requires -Version 7` (Windows PowerShell 5.1 turns benign native stderr into terminating errors).
- No PII in tracked files: no `C:\Users\<realname>`, no emails, no hostnames, no LAN IPs. Guard: `tests/test_release_ux.py::test_tracked_tree_carries_no_maintainer_identifiers`.
- Docker humanizes sizes with **SI (1000-based)** units: `0B`, `8.192kB`, `12.45GB`. Lowercase `k`.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Test runner: `HF_HUB_OFFLINE=1` with the repo venv at `<repo>/.venv/Scripts/python.exe`.

## Verified CLI contracts (do not re-derive; measured 2026-07-28 on Docker 29.6.2)

```
docker system df --format '{{.Type}}|{{.Size}}'
  Images|44.22GB
  Containers|1.532MB
  Local Volumes|283.6MB
  Build Cache|12.45GB

docker builder du --format '{{.CreatedAt}}|{{.Size}}'
  2026-07-28 05:31:09.884805417 +0000 UTC|250B
  2026-07-28 05:31:09.786369487 +0000 UTC|8.192kB

docker builder prune --help  ->  --all --filter --force --max-used-space --reserved-space
  (NO --keep-storage)

wsl -d docker-desktop -e true    ->  exit 0
wsl -d no-such-distro -e true    ->  exit 255
  (Use the exit code. `wsl -l -q` returns UTF-16LE with interleaved nulls
   and is unsafe to parse from bash.)
```

## File Structure

| File | Responsibility |
|---|---|
| `ops/prune-build-cache.ps1` | **Create.** The retention primitive (Windows). |
| `ops/prune-build-cache.sh` | **Create.** Bash port, same CLI contract. |
| `ops/update.ps1` | **Modify.** Add `-KeepCacheHours` / `-NoCachePrune`; call primitive after health check. |
| `ops/update.sh` | **Modify.** Mirror with `--keep-cache-hours` / `--no-cache-prune`. |
| `ops/install-cache-retention.ps1` | **Create.** Register/unregister the weekly Scheduled Task. |
| `ops/compact-docker-vhdx.ps1` | **Create.** De-identified offline compact. Manual only. |
| `docs/runbooks/docker-disk-retention.md` | **Create.** What is automated, what is not, and why. |
| `tests/test_ops_prune_build_cache.py` | **Create.** Stubbed-`docker` harness, parametrised ps1/sh. |
| `README.md` | **Modify.** Replace the manual `docker builder prune` advice at ~L305. |
| `CHANGELOG.md` | **Modify.** `[Unreleased]` entry. |

---

### Task 1: The retention primitive (PowerShell) + test harness

**Files:**
- Create: `ops/prune-build-cache.ps1`
- Create: `tests/test_ops_prune_build_cache.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ops/prune-build-cache.ps1` with parameters `-MaxAgeHours <int> = 168`, `-MaxUsedSpaceGB <int> = 20`, `-DryRun <switch>`, `-NoTrim <switch>`. Task 3 calls it as `& (Join-Path $PSScriptRoot "prune-build-cache.ps1") -MaxAgeHours <n>`. Task 4 invokes it from a scheduled task. Task 2 adds the `sh` parametrisation to the same test file.

- [ ] **Step 1: Write the failing test file**

Create `tests/test_ops_prune_build_cache.py`:

```python
"""Build-cache retention: ``ops/prune-build-cache.ps1|.sh`` + update wiring.

Why this exists: ``update.ps1`` rebuilds the daemon image on every deploy and
nothing ever removed the BuildKit cache those rebuilds create — 51.87GB across
169 entries by 2026-07-28, and a single deploy measured at 12.45GB/17 entries.

Build cache is pure derived data, so over-pruning only costs rebuild time. The
load-bearing assertion here is therefore NEGATIVE: the scripts must be
incapable of touching images, containers, or volumes. The stub fails the test
on any such call.

Same harness as ``test_ops_prune_rollbacks.py``: drive the REAL script with
``docker`` (and ``wsl``) stubbed as shell functions, so each script's exact CLI
contract is pinned without a Docker daemon.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PS1_SCRIPT = REPO / "ops" / "prune-build-cache.ps1"
SH_SCRIPT = REPO / "ops" / "prune-build-cache.sh"
UPDATE_PS1 = REPO / "ops" / "update.ps1"
UPDATE_SH = REPO / "ops" / "update.sh"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _find_bash() -> str | None:
    # Prefer Git Bash on Windows — System32 bash.exe launches WSL, where the
    # C:-style script paths don't resolve.
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).exists():
            return cand
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


BASH = _find_bash()

# Forbidden verbs. Any of these reaching the stub fails the run outright.
FORBIDDEN = ("system prune", "rmi", "image rm", "volume")


def _fixture(size="12.45GB", entries=None, distro=True):
    """Default state: a fresh post-deploy cache, every entry minutes old."""
    if entries is None:
        entries = [
            {"created": "2026-07-28 05:31:09.884805417 +0000 UTC", "size": "250B"},
            {"created": "2026-07-28 05:31:09.786369487 +0000 UTC", "size": "8.192kB"},
        ]
    return {
        "df": [("Images", "44.22GB"), ("Containers", "1.532MB"),
               ("Local Volumes", "283.6MB"), ("Build Cache", size)],
        "du": entries,
        "distro": distro,
    }


def _run_ps1(tmp_path: Path, fixture: dict, *args: str):
    fx_path = tmp_path / "fixture.json"
    fx_path.write_text(json.dumps(fixture), encoding="utf-8")
    calls_log = tmp_path / "calls.log"
    calls_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        f'''
$fx = Get-Content -Raw "{fx_path}" | ConvertFrom-Json
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content "{calls_log}" ($a -join ' ')
    if ($a[0] -eq "system" -and $a[1] -eq "df") {{
        return @($fx.df | ForEach-Object {{ "$($_[0])|$($_[1])" }})
    }}
    if ($a[0] -eq "builder" -and $a[1] -eq "du") {{
        return @($fx.du | ForEach-Object {{ "$($_.created)|$($_.size)" }})
    }}
    if ($a[0] -eq "builder" -and $a[1] -eq "prune") {{ return "Total: 0B" }}
    throw "unexpected docker call: $($a -join ' ')"
}}
function global:wsl {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content "{calls_log}" ("wsl " + ($a -join ' '))
    if (-not $fx.distro) {{ $global:LASTEXITCODE = 255 }}
    return
}}
& "{PS1_SCRIPT}" {" ".join(args)}
''',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=120,
    )
    calls = [ln for ln in calls_log.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return proc, calls


@pytest.fixture(params=["ps1"])
def prune(request, tmp_path):
    """Run the retention script variant under test.
    Call as ``prune(fixture, "-DryRun")``; returns (proc, docker_calls)."""
    if request.param == "ps1":
        if PWSH is None:
            pytest.skip("PowerShell not on PATH")

        def run(fixture, *args):
            return _run_ps1(tmp_path, fixture, *args)
    else:
        if BASH is None:
            pytest.skip("bash not available")

        def run(fixture, *args):
            return _run_sh(tmp_path, fixture, *args)
    return run


def _prunes(calls):
    return [c for c in calls if c.startswith("builder prune")]


def test_never_issues_a_forbidden_docker_verb(prune):
    """THE guard. Build-cache retention must be incapable of removing images,
    containers or volumes — those are owned by prune-rollbacks.ps1 and by
    nothing at all, respectively."""
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    for call in calls:
        for verb in FORBIDDEN:
            assert verb not in call, f"forbidden docker verb in: {call}"


def test_age_pass_always_runs_with_the_default_window(prune):
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    assert any("builder prune --force --filter until=168h" in c
               for c in _prunes(calls)), calls


def test_age_pass_honours_the_max_age_parameter(prune):
    proc, calls = prune(_fixture(), "-MaxAgeHours", "24")
    assert proc.returncode == 0, proc.stderr
    assert any("until=24h" in c for c in _prunes(calls)), calls


def test_ceiling_pass_is_skipped_when_under_the_cap(prune):
    # 12.45GB of cache, 20GB ceiling -> the backstop must not fire.
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    assert not any("--max-used-space" in c for c in _prunes(calls)), calls


def test_ceiling_pass_fires_when_still_over_the_cap_after_the_age_pass(prune):
    fx = _fixture(size="30GB")
    proc, calls = prune(fx, "-MaxUsedSpaceGB", "8")
    assert proc.returncode == 0, proc.stderr
    assert any("--max-used-space 8000000000" in c for c in _prunes(calls)), calls


def test_dry_run_mutates_nothing(prune):
    proc, calls = prune(_fixture(), "-DryRun")
    assert proc.returncode == 0, proc.stderr
    assert _prunes(calls) == [], "dry run must issue no prune"
    assert not any(c.startswith("wsl") and "fstrim" in c for c in calls), \
        "dry run must not fstrim — it discards blocks"
    # Must still have produced a report, else this passes vacuously against
    # a missing script.
    assert "12.45GB" in proc.stdout
    assert "until=168h" in proc.stdout


def test_no_trim_switch_skips_fstrim(prune):
    proc, calls = prune(_fixture(), "-NoTrim")
    assert proc.returncode == 0, proc.stderr
    assert not any("fstrim" in c for c in calls)


def test_fstrim_runs_by_default_on_the_docker_desktop_distro(prune):
    proc, calls = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    assert any("fstrim -v /mnt/docker-desktop-disk" in c for c in calls), calls


def test_absent_distro_skips_fstrim_without_failing(prune):
    proc, calls = prune(_fixture(distro=False))
    assert proc.returncode == 0, proc.stderr
    assert not any("fstrim" in c for c in calls)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_ops_prune_build_cache.py -v
```

Expected: every test FAILS. The driver invokes a script that does not exist, so pwsh exits non-zero and the `proc.returncode == 0` assertions fail.

- [ ] **Step 3: Write `ops/prune-build-cache.ps1`**

```powershell
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
# "reclaimable" — reclaimable means "not pinned by a running build", NOT
# "not worth keeping". A policy keyed on reclaimability deletes hot cache
# and cold-starts the next build.
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
    if ($Bytes -ge 1e9) { return ("{0:N2}GB" -f ($Bytes / 1e9)) }
    if ($Bytes -ge 1e6) { return ("{0:N2}MB" -f ($Bytes / 1e6)) }
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
        Write-Host "    trim        : wsl -d docker-desktop -e fstrim -v /mnt/docker-desktop-disk"
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
# Windows/WSL only, and never fatal.
if ($NoTrim) { return }
if (-not $IsWindows) { return }
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
    Write-Host "==> Build-cache retention: no wsl on PATH; skipping fstrim."
    return
}
wsl -d docker-desktop -e true 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Build-cache retention: no docker-desktop WSL distro; skipping fstrim."
    return
}
wsl -d docker-desktop -e fstrim -v /mnt/docker-desktop-disk
if ($LASTEXITCODE -ne 0) {
    Write-Warning "fstrim failed (retention otherwise succeeded)."
}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_ops_prune_build_cache.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Mutation-check the guard (per project CLAUDE.md)**

Temporarily add `docker rmi some-image | Out-Null` immediately after the age pass in `ops/prune-build-cache.ps1`. Run:

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_ops_prune_build_cache.py -k forbidden -v
```

Expected: `test_never_issues_a_forbidden_docker_verb` FAILS with "forbidden docker verb in: rmi some-image". **Then revert the temporary line.** A guard that never fires red is decoration.

- [ ] **Step 6: Commit**

```bash
git add ops/prune-build-cache.ps1 tests/test_ops_prune_build_cache.py
git commit -m "feat(ops): build-cache retention primitive (PowerShell)"
```

---

### Task 2: Bash port

**Files:**
- Create: `ops/prune-build-cache.sh`
- Modify: `tests/test_ops_prune_build_cache.py` (add `_run_sh`, add `"sh"` to the fixture params)

**Interfaces:**
- Consumes: the CLI contract from Task 1.
- Produces: `ops/prune-build-cache.sh` accepting `--max-age-hours <int>` (default 168), `--max-used-space-gb <int>` (default 20), `--dry-run`, `--no-trim`. Task 3 calls it as `"$(dirname "$0")/prune-build-cache.sh" --max-age-hours "$KEEP_CACHE_HOURS"`.

- [ ] **Step 1: Add the bash driver and enable the `sh` parametrisation (failing test)**

In `tests/test_ops_prune_build_cache.py`, insert `_run_sh` directly above the `prune` fixture:

```python
def _run_sh(tmp_path: Path, fixture: dict, *args: str):
    fx_dir = tmp_path / "fx"
    fx_dir.mkdir(exist_ok=True)
    (fx_dir / "df.txt").write_text(
        "".join(f"{t}|{s}\n" for t, s in fixture["df"]),
        encoding="utf-8", newline="\n")
    (fx_dir / "du.txt").write_text(
        "".join(f"{e['created']}|{e['size']}\n" for e in fixture["du"]),
        encoding="utf-8", newline="\n")
    calls_log = tmp_path / "calls.log"
    calls_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f'''#!/usr/bin/env bash
set -u
export FX="{fx_dir.as_posix()}"
export CALLS="{calls_log.as_posix()}"
export DISTRO_OK={"1" if fixture["distro"] else "0"}
docker() {{
    echo "$*" >> "$CALLS"
    if [ "$1" = "system" ] && [ "$2" = "df" ]; then
        cat "$FX/df.txt"
    elif [ "$1" = "builder" ] && [ "$2" = "du" ]; then
        cat "$FX/du.txt"
    elif [ "$1" = "builder" ] && [ "$2" = "prune" ]; then
        echo "Total: 0B"
    else
        echo "unexpected docker call: $*" >&2
        return 1
    fi
}}
wsl() {{
    echo "wsl $*" >> "$CALLS"
    [ "$DISTRO_OK" = "1" ] || return 255
    return 0
}}
export -f docker
export -f wsl
bash "{SH_SCRIPT.as_posix()}" "$@"
''',
        encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [BASH, str(driver), *args],
        capture_output=True, text=True, timeout=120,
    )
    calls = [ln for ln in calls_log.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return proc, calls
```

Change the fixture decorator and add flag translation so one test body drives both variants:

```python
# PowerShell flag -> bash flag. One test body, two CLIs.
_SH_FLAGS = {
    "-MaxAgeHours": "--max-age-hours",
    "-MaxUsedSpaceGB": "--max-used-space-gb",
    "-DryRun": "--dry-run",
    "-NoTrim": "--no-trim",
}


@pytest.fixture(params=["ps1", "sh"])
def prune(request, tmp_path):
    """Run the retention script variant under test.
    Call as ``prune(fixture, "-DryRun")``; returns (proc, docker_calls)."""
    if request.param == "ps1":
        if PWSH is None:
            pytest.skip("PowerShell not on PATH")

        def run(fixture, *args):
            return _run_ps1(tmp_path, fixture, *args)
    else:
        if BASH is None:
            pytest.skip("bash not available")

        def run(fixture, *args):
            translated = [_SH_FLAGS.get(a, a) for a in args]
            return _run_sh(tmp_path, fixture, *translated)
    return run
```

- [ ] **Step 2: Run the tests to verify the `sh` half fails**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_ops_prune_build_cache.py -v -k sh
```

Expected: 9 FAIL (`prune-build-cache.sh` does not exist), 9 `ps1` tests still pass.

- [ ] **Step 3: Write `ops/prune-build-cache.sh`**

```bash
#!/usr/bin/env bash
# Retention for the BuildKit build cache. Bash port of ops/prune-build-cache.ps1.
#
#   ops/prune-build-cache.sh                     # age 168h, ceiling 20GB
#   ops/prune-build-cache.sh --dry-run           # report only; mutate nothing
#   ops/prune-build-cache.sh --max-age-hours 24  # tighter age policy
#
# Build cache is pure derived data: over-pruning costs rebuild time and
# nothing else. This script ONLY ever calls `docker builder prune`. It never
# removes images (ops/prune-rollbacks.sh owns rollback-tag retention), never
# touches containers, and never runs `docker system prune` or any volume
# command — the bank lives in the external ops_pseudolife_* volumes.
#
# The fstrim step is Windows/WSL-only; on Linux there is no vhdx and the
# prune alone is the whole job.
set -euo pipefail

MAX_AGE_HOURS=168
MAX_USED_SPACE_GB=20
DRY_RUN=0
NO_TRIM=0

while [ $# -gt 0 ]; do
    case "$1" in
        --max-age-hours)      MAX_AGE_HOURS="$2"; shift 2 ;;
        --max-used-space-gb)  MAX_USED_SPACE_GB="$2"; shift 2 ;;
        --dry-run)            DRY_RUN=1; shift ;;
        --no-trim)            NO_TRIM=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
for v in "$MAX_AGE_HOURS" "$MAX_USED_SPACE_GB"; do
    case "$v" in
        ''|*[!0-9]*) echo "--max-age-hours/--max-used-space-gb must be non-negative integers" >&2; exit 2 ;;
    esac
done

# docker humanizes with SI (1000-based) units and a lowercase k: "8.192kB".
docker_size_to_bytes() {
    printf '%s' "$1" | awk '
        {
            if (match($0, /^[ ]*[0-9.]+/) == 0) { exit 1 }
            num = substr($0, RSTART, RLENGTH) + 0
            unit = toupper(substr($0, RSTART + RLENGTH))
            gsub(/[ ]/, "", unit)
            m = 1
            if (unit == "KB") m = 1000
            else if (unit == "MB") m = 1000000
            else if (unit == "GB") m = 1000000000
            else if (unit == "TB") m = 1000000000000
            else if (unit == "PB") m = 1000000000000000
            else if (unit != "B") { exit 1 }
            printf "%d", num * m
        }'
}

format_bytes() {
    awk -v b="$1" 'BEGIN {
        if (b >= 1000000000) printf "%.2fGB", b / 1000000000
        else if (b >= 1000000) printf "%.2fMB", b / 1000000
        else printf "%dB", b
    }'
}

build_cache_bytes() {
    local row type size
    while IFS= read -r row; do
        type="${row%%|*}"
        size="${row#*|}"
        if [ "$type" = "Build Cache" ]; then
            docker_size_to_bytes "$size"
            return 0
        fi
    done < <(docker system df --format '{{.Type}}|{{.Size}}')
    echo "docker system df reported no Build Cache row" >&2
    return 1
}

# Dry-run only. BuildKit has no --dry-run, so this sums entries whose
# CreatedAt precedes the cutoff. It is an ESTIMATE: BuildKit's own until=
# filter considers last-used, and shared entries may not free fully.
stale_estimate_bytes() {
    local cutoff row created size total=0 when
    cutoff="$(date -u -d "-${MAX_AGE_HOURS} hours" +%s)"
    while IFS= read -r row; do
        [ -n "$row" ] || continue
        created="${row%%|*}"
        size="${row#*|}"
        # "2026-07-28 05:31:09.884 +0000 UTC" -> strip fractional secs + zone
        # name, which GNU date will not parse together.
        created="$(printf '%s' "$created" | sed -E 's/\.[0-9]+//; s/ UTC$//')"
        when="$(date -u -d "$created" +%s 2>/dev/null || echo 0)"
        if [ "$when" -ne 0 ] && [ "$when" -lt "$cutoff" ]; then
            total=$(( total + $(docker_size_to_bytes "$size") ))
        fi
    done < <(docker builder du --format '{{.CreatedAt}}|{{.Size}}')
    printf '%d' "$total"
}

cap_bytes=$(( MAX_USED_SPACE_GB * 1000000000 ))
before="$(build_cache_bytes)"
echo "==> Build-cache retention: $(format_bytes "$before") in cache (age policy ${MAX_AGE_HOURS}h, ceiling $(format_bytes "$cap_bytes"))."

if [ "$DRY_RUN" = "1" ]; then
    stale="$(stale_estimate_bytes)"
    echo "==> DRY RUN: nothing below is executed."
    echo "    age pass    : docker builder prune --force --filter until=${MAX_AGE_HOURS}h"
    echo "                  estimated reclaim $(format_bytes "$stale")."
    echo "                  (Estimate: sums CreatedAt; BuildKit's until= uses last-used,"
    echo "                   and shared entries may not free fully.)"
    if [ $(( before - stale )) -gt "$cap_bytes" ]; then
        echo "    ceiling pass: docker builder prune --force --max-used-space $cap_bytes"
    else
        echo "    ceiling pass: skipped (post-age size would be within the ceiling)."
    fi
    if [ "$NO_TRIM" != "1" ]; then
        echo "    trim        : wsl -d docker-desktop -e fstrim -v /mnt/docker-desktop-disk"
    fi
    echo "==> DRY RUN complete: nothing was changed."
    exit 0
fi

# Age pass — the normal policy, always runs.
docker builder prune --force --filter "until=${MAX_AGE_HOURS}h" > /dev/null

# Ceiling pass — backstop only.
after_age="$(build_cache_bytes)"
if [ "$after_age" -gt "$cap_bytes" ]; then
    echo "==> Build-cache retention: $(format_bytes "$after_age") still over the $(format_bytes "$cap_bytes") ceiling; enforcing."
    docker builder prune --force --max-used-space "$cap_bytes" > /dev/null
fi

after="$(build_cache_bytes)"
echo "==> Build-cache retention: $(format_bytes "$before") -> $(format_bytes "$after") (reclaimed $(format_bytes $(( before - after ))))."

# fstrim returns freed blocks to the vhdx free list; it does NOT shrink the
# host file. Windows/WSL only, never fatal.
[ "$NO_TRIM" = "1" ] && exit 0
if ! command -v wsl > /dev/null 2>&1; then
    echo "==> Build-cache retention: no wsl on PATH; skipping fstrim."
    exit 0
fi
if ! wsl -d docker-desktop -e true 2>/dev/null; then
    echo "==> Build-cache retention: no docker-desktop WSL distro; skipping fstrim."
    exit 0
fi
if ! wsl -d docker-desktop -e fstrim -v /mnt/docker-desktop-disk; then
    echo "WARNING: fstrim failed (retention otherwise succeeded)." >&2
fi
```

- [ ] **Step 4: Run the full test file**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_ops_prune_build_cache.py -v
```

Expected: 18 passed (9 tests × 2 variants).

- [ ] **Step 5: Commit**

```bash
git add ops/prune-build-cache.sh tests/test_ops_prune_build_cache.py
git commit -m "feat(ops): bash port of the build-cache retention primitive"
```

---

### Task 3: Wire into the deploy path

**Files:**
- Modify: `ops/update.ps1` (params block L14-18; new step after the health block L103-112)
- Modify: `ops/update.sh` (arg parsing; new step after the health block)
- Modify: `tests/test_ops_prune_build_cache.py` (append wiring tests)

**Interfaces:**
- Consumes: `ops/prune-build-cache.ps1 -MaxAgeHours <int>` and `ops/prune-build-cache.sh --max-age-hours <int>` from Tasks 1-2.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/test_ops_prune_build_cache.py`:

```python
def test_update_ps1_wires_cache_retention_in():
    """update.ps1 must expose -KeepCacheHours / -NoCachePrune and call the
    primitive. Retention must not abort a deploy that otherwise succeeded."""
    text = UPDATE_PS1.read_text(encoding="utf-8")
    assert "KeepCacheHours" in text
    assert "NoCachePrune" in text
    assert "prune-build-cache.ps1" in text
    assert "$KeepCacheHours = 168" in text


def test_update_sh_wires_cache_retention_in():
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "--keep-cache-hours" in text
    assert "--no-cache-prune" in text
    assert "prune-build-cache.sh" in text
    assert "KEEP_CACHE_HOURS=168" in text


def test_cache_prune_runs_after_the_health_check_not_beside_prune_rollbacks():
    """Placement is load-bearing, in both directions:

    * Before the build it would delete the cache that build reuses, making
      every deploy a cold build.
    * On the unhealthy path it would strip the cache an operator's rollback
      rebuild needs — exactly when a slow build hurts most. The unhealthy
      branch exits, so being after the health block skips it for free.
    """
    ps1 = UPDATE_PS1.read_text(encoding="utf-8")
    assert ps1.index("prune-build-cache.ps1") > ps1.index("Waiting for the daemon"), \
        "cache prune must come after the health check"
    assert ps1.index("prune-build-cache.ps1") > ps1.index("--build pseudolife-daemon"), \
        "cache prune must come after the build"
    assert ps1.index("prune-rollbacks.ps1") < ps1.index("--build pseudolife-daemon"), \
        "rollback retention still belongs before the build"

    sh = UPDATE_SH.read_text(encoding="utf-8")
    assert sh.index("prune-build-cache.sh") > sh.index("Waiting for the daemon")
    assert sh.index("prune-build-cache.sh") > sh.index("--build pseudolife-daemon")
```

- [ ] **Step 2: Run to verify they fail**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_ops_prune_build_cache.py -k update_ps1_wires or update_sh_wires or after_the_health -v
```

Expected: 3 FAIL — `KeepCacheHours` is not in `update.ps1`, and `.index()` raises `ValueError` for the missing substring.

- [ ] **Step 3: Modify `ops/update.ps1`**

Replace the header usage comment block line `#   ops\update.ps1 -KeepRollbacks 5  # rollback tags to retain (default 2)` by appending two lines after it:

```powershell
#   ops\update.ps1 -KeepRollbacks 5  # rollback tags to retain (default 2)
#   ops\update.ps1 -KeepCacheHours 24 # build cache to retain, hours (default 168)
#   ops\update.ps1 -NoCachePrune     # skip build-cache retention entirely
```

Replace the `param(...)` block (currently lines 14-18) with:

```powershell
param(
    [string]$Tag = "",
    [switch]$NoBackup,
    [int]$KeepRollbacks = 2,
    [int]$KeepCacheHours = 168,
    [switch]$NoCachePrune
)
```

Then, at the very end of the file, **after** the closing `}` of the `if ($h) { ... } else { ... exit 1 }` health block, append:

```powershell

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
```

- [ ] **Step 4: Modify `ops/update.sh`**

Add to the defaults near the other `KEEP_*` variable:

```bash
KEEP_CACHE_HOURS=168
NO_CACHE_PRUNE=0
```

Add to the argument-parsing `case` statement, alongside `--keep-rollbacks`:

```bash
        --keep-cache-hours) KEEP_CACHE_HOURS="$2"; shift 2 ;;
        --no-cache-prune)   NO_CACHE_PRUNE=1; shift ;;
```

Append at the very end of the file, after the health-check block:

```bash

# 5. Build-cache retention. Deliberately LAST, for two reasons: before the
#    build it would delete the cache the build reuses (cold-starting every
#    deploy), and on the unhealthy path above it would strip the cache an
#    operator's rollback rebuild wants — that branch exits, so this is
#    skipped for free. A retention hiccup must never fail a good deploy.
if [ "$NO_CACHE_PRUNE" != "1" ]; then
    if ! "$(dirname "$0")/prune-build-cache.sh" --max-age-hours "$KEEP_CACHE_HOURS"; then
        echo "WARNING: build-cache retention failed (deploy already succeeded)." >&2
    fi
fi
```

- [ ] **Step 5: Run the full file to verify all pass**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_ops_prune_build_cache.py -v
```

Expected: 21 passed.

- [ ] **Step 6: Confirm the existing rollback tests still pass**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_ops_prune_rollbacks.py tests/test_ops_update_rollback.py -v
```

Expected: all pass. These read `update.ps1`/`update.sh` too; the param block changed.

- [ ] **Step 7: Commit**

```bash
git add ops/update.ps1 ops/update.sh tests/test_ops_prune_build_cache.py
git commit -m "feat(ops): run build-cache retention after a healthy deploy"
```

---

### Task 4: Weekly Scheduled Task installer

**Files:**
- Create: `ops/install-cache-retention.ps1`

**Interfaces:**
- Consumes: `ops/prune-build-cache.ps1` from Task 1.
- Produces: a Scheduled Task named `Pseudolife-MCP Docker cache retention`.

- [ ] **Step 1: Write `ops/install-cache-retention.ps1`**

```powershell
#Requires -Version 7
# Register a weekly Docker build-cache retention task (Windows Task Scheduler).
#
#   ops\install-cache-retention.ps1                       # Sunday 03:00, defaults
#   ops\install-cache-retention.ps1 -DayOfWeek Wednesday -At 21:00
#   ops\install-cache-retention.ps1 -Unregister           # remove it
#
# The deploy path (ops\update.ps1) already prunes after every healthy deploy;
# this covers the other gap — stretches with no deploys at all, which is how
# 51.87GB accumulated by 2026-07-28.
param(
    [int]$MaxAgeHours = 168,
    [int]$MaxUsedSpaceGB = 20,
    [ValidateSet("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]
    [string]$DayOfWeek = "Sunday",
    [string]$At = "03:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$taskName = "Pseudolife-MCP Docker cache retention"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Unregistered '$taskName'."
    return
}

$script = Join-Path $PSScriptRoot "prune-build-cache.ps1"
if (-not (Test-Path $script)) { throw "not found: $script" }

# Base64 -EncodedCommand, as ops\install-autostart.ps1 does: it survives the
# quoting round-trip through Task Scheduler's single argument string.
$inner = "& '$script' -MaxAgeHours $MaxAgeHours -MaxUsedSpaceGB $MaxUsedSpaceGB"
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))

$action = New-ScheduledTaskAction -Execute "pwsh.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $At
# StartWhenAvailable is the point of this task: a desktop is often off at
# 03:00 Sunday, and a retention run that silently never happens is the
# failure mode being fixed.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "Prune Docker build cache older than $MaxAgeHours h (ceiling ${MaxUsedSpaceGB}GB), then fstrim the WSL disk." | Out-Null

Write-Host "Registered '$taskName' ($DayOfWeek $At) -> $script"
Write-Host "Run now with:  Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Remove with :  ops\install-cache-retention.ps1 -Unregister"
```

- [ ] **Step 2: Syntax-check without registering anything**

```bash
pwsh -NoProfile -Command "\$ErrorActionPreference='Stop'; \$null = [System.Management.Automation.Language.Parser]::ParseFile('ops/install-cache-retention.ps1', [ref]\$null, [ref]\$e); if (\$e) { \$e; exit 1 } else { 'parse ok' }"
```

Expected: `parse ok`.

- [ ] **Step 3: Register it for real and confirm**

```bash
pwsh -NoProfile -File ops/install-cache-retention.ps1
```

Expected: `Registered 'Pseudolife-MCP Docker cache retention' (Sunday 03:00) -> ...`

```bash
pwsh -NoProfile -Command "Get-ScheduledTask -TaskName 'Pseudolife-MCP Docker cache retention' | Select-Object TaskName, State"
```

Expected: `State: Ready`.

- [ ] **Step 4: Commit**

```bash
git add ops/install-cache-retention.ps1
git commit -m "feat(ops): weekly scheduled task for build-cache retention"
```

---

### Task 5: De-identified offline compact script

**Files:**
- Create: `ops/compact-docker-vhdx.ps1`

**Interfaces:**
- Consumes: nothing.
- Produces: `ops/compact-docker-vhdx.ps1 [-Path <vhdx>]`, referenced by the runbook in Task 6.

- [ ] **Step 1: Write `ops/compact-docker-vhdx.ps1`**

```powershell
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
```

- [ ] **Step 2: Verify no identifiers leaked in**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_release_ux.py -k identifier -v
```

Expected: PASS. (The original scratchpad version hardcoded a home path and would fail this.)

- [ ] **Step 3: Parse-check (do not run — it stops Docker)**

```bash
pwsh -NoProfile -Command "\$null = [System.Management.Automation.Language.Parser]::ParseFile('ops/compact-docker-vhdx.ps1', [ref]\$null, [ref]\$e); if (\$e) { \$e; exit 1 } else { 'parse ok' }"
```

Expected: `parse ok`.

- [ ] **Step 4: Commit**

```bash
git add ops/compact-docker-vhdx.ps1
git commit -m "feat(ops): de-identified offline vhdx compact script"
```

---

### Task 6: Documentation

**Files:**
- Create: `docs/runbooks/docker-disk-retention.md`
- Modify: `README.md` (the manual-prune advice at ~L305-307)
- Modify: `CHANGELOG.md` (`[Unreleased]`)

**Interfaces:**
- Consumes: every script from Tasks 1-5.
- Produces: nothing.

- [ ] **Step 1: Create `docs/runbooks/docker-disk-retention.md`**

```markdown
# Runbook — Docker disk retention

Two separate problems, only one of which is automated.

## 1. Build cache grows without bound (AUTOMATED)

Every daemon rebuild adds BuildKit cache entries. Nothing removed them before
2026-07-28: 51.87GB across 169 entries had accumulated, every one inactive,
some 5-6 weeks old. A single deploy accounts for ~12.45GB.

Build cache is **pure derived data** — the only cost of pruning it is rebuild
time — so retention is safe to automate.

**What runs, and when:**

| Trigger | Where | Policy |
|---|---|---|
| After every healthy deploy | `ops/update.ps1` / `.sh`, final step | age 168h |
| Weekly | Scheduled Task, if registered | age 168h, 20GB ceiling |

Register the weekly task (Windows, once):

```powershell
.\ops\install-cache-retention.ps1
```

Run retention by hand at any time:

```powershell
.\ops\prune-build-cache.ps1 -DryRun    # report only, changes nothing
.\ops\prune-build-cache.ps1            # age pass, ceiling pass, fstrim
```

**Why age and not size or "reclaimable".** Minutes after a deploy, Docker
reports the whole fresh cache as reclaimable — reclaimable means "not pinned
by a running build", not "not worth keeping". Pruning on that signal deletes
hot cache and cold-starts the next build. The 168h window keeps the week of
cache that actually gets reused; the size ceiling is only a backstop for an
unusually heavy build week.

**What these scripts will never do:** remove images (that is
`ops/prune-rollbacks.ps1`), touch containers, or run `docker system prune` or
any volume command. Never run `docker system prune --volumes` on this host —
the bank lives in the external `ops_pseudolife_data` and
`ops_pseudolife_pgdata` volumes.

## 2. The .vhdx never shrinks (MANUAL — needs elevation)

Pruning frees space *inside* the VM. `fstrim` (run automatically as the last
retention step) returns those blocks to the disk's free list. Neither shrinks
the host file: sparse mode was deliberately declined as risky, so the `.vhdx`
only gets smaller under an offline `Optimize-VHD -Mode Full`.

That requires an Administrator prompt and stops Docker Desktop plus every WSL
distro, so it stays an operator decision.

**From an elevated PowerShell prompt:**

```powershell
.\ops\compact-docker-vhdx.ps1
```

It stops Docker, shuts down WSL, verifies nothing still holds the file, then
compacts and reports before/after.

**Measured 2026-07-28.** Prune + fstrim took internal usage 87GB -> 49.3GB
while the file stayed at 94.74GB. The offline compact then took the file to
47.31GB. Both halves are needed: the prune reclaims, the compact returns it.
```

- [ ] **Step 2: Update the README manual-prune advice**

Replace the sentence at `README.md` ~L305-307:

```
`pip install -e .` is editable.) Reclaim accumulated build cache now and
then with `docker builder prune` (safe — it only touches build layers);
never `docker system prune --volumes`, which deletes volumes.
```

with:

```
`pip install -e .` is editable.) Build cache is pruned automatically after
every healthy deploy; see
[Docker disk retention](docs/runbooks/docker-disk-retention.md) for the
weekly task and the manual vhdx compact. Never run
`docker system prune --volumes`, which deletes volumes.
```

- [ ] **Step 3: Add the CHANGELOG entry**

Insert directly beneath the `## [Unreleased]` line:

```markdown
### Added (2026-07-28 — Docker build-cache retention)
- **`ops/prune-build-cache.ps1` / `.sh`** give the BuildKit cache a retention
  policy: an age pass (`--filter until=168h`), then a size-ceiling pass
  (`--max-used-space`, default 20GB) only if still over cap, then a
  Windows-only `fstrim` of the WSL disk. `-DryRun` reports and changes
  nothing. The sibling of the 2026-07-14 rollback-tag retention: one deploy
  produces ~12.45GB of cache, and 51.87GB across 169 entries — all inactive,
  some 5-6 weeks old — had accumulated by 2026-07-28.
- **`ops/update.ps1` / `.sh` prune after a healthy deploy**, via
  `-KeepCacheHours` (default 168) and `-NoCachePrune`. Placement is
  load-bearing: before the build it would delete the cache that build reuses,
  and on the unhealthy path it would strip the cache a rollback rebuild needs.
  Non-fatal — retention never fails a deploy that succeeded.
- **`ops/install-cache-retention.ps1`** registers a weekly Scheduled Task, for
  stretches with no deploys.
- **`ops/compact-docker-vhdx.ps1` + `docs/runbooks/docker-disk-retention.md`.**
  The vhdx never shrinks on its own, and the offline `Optimize-VHD -Mode Full`
  needs elevation plus Docker downtime, so it stays manual and documented.
- Scope guard: these scripts only ever call `docker builder prune`. Never
  images, containers, `docker system prune`, or any volume command —
  enforced by `tests/test_ops_prune_build_cache.py`, not by convention.
```

- [ ] **Step 4: Verify the docs guards pass**

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/test_release_ux.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add docs/runbooks/docker-disk-retention.md README.md CHANGELOG.md
git commit -m "docs(ops): document Docker disk retention, automated and manual"
```

---

### Task 7: Full suite + live verification

**Files:** none modified.

**Interfaces:** consumes everything above.

- [ ] **Step 1: Run the full suite**

Requires the bench Postgres up at `127.0.0.1:5433` — PG-backed tests skip silently without it, which is not a pass.

```bash
HF_HUB_OFFLINE=1 python -m pytest tests/
```

Expected: all pass, no new failures against the pre-change baseline.

- [ ] **Step 2: Record the before reading**

```bash
docker system df
```

Record the Build Cache row. Expected ~12.45GB / 17 entries (from the 2026-07-28 deploy).

- [ ] **Step 3: Dry run at defaults — proves the size read and the no-op**

```bash
pwsh -NoProfile -File ops/prune-build-cache.ps1 -DryRun
```

Expected: reports the current size, `age pass ... estimated reclaim 0B` (nothing is older than 168h), `ceiling pass: skipped`, and `nothing was changed`. Re-run `docker system df` to confirm the cache is untouched.

- [ ] **Step 4: Dry run at zero age — proves the filter is really plumbed through**

```bash
pwsh -NoProfile -File ops/prune-build-cache.ps1 -DryRun -MaxAgeHours 0
```

Expected: `until=0h` in the planned command and an estimated reclaim close to the full cache size. Still mutates nothing — confirm with `docker system df`.

- [ ] **Step 5: One real run at a deliberately low ceiling — proves reclamation**

This is the only mutating step. `-MaxUsedSpaceGB 8` against ~12.45GB of cache forces the ceiling pass to reclaim roughly 4.5GB while leaving ~8GB of hot cache, so the next build stays warm.

```bash
pwsh -NoProfile -File ops/prune-build-cache.ps1 -MaxUsedSpaceGB 8
```

Expected: the age pass reclaims nothing, the ceiling pass fires, the summary reports a non-zero reclaim, and `fstrim` runs against `/mnt/docker-desktop-disk`.

- [ ] **Step 6: Record the after reading**

```bash
docker system df
```

Expected: Build Cache at or below 8GB. Record before/after for the commit message.

**Deliberately not run:** a default-age real run (a no-op today — every entry is minutes old) and anything `-af`-equivalent (would cold-start the next deploy to prove nothing the stubbed tests do not already pin). The first default-policy reclaim lands on its own once these entries pass 168h.

- [ ] **Step 7: Commit the verification record**

```bash
git commit --allow-empty -m "test(ops): live verification of build-cache retention"
```

Put the actual before/after numbers from Steps 2 and 6 in the commit body.

---

## Self-Review

**Spec coverage:** §5.1 primitive → Tasks 1-2. §5.2 deploy hook → Task 3. §5.3 scheduled task → Task 4. §5.4 compact script → Task 5. §5.5 runbook → Task 6. §6 testing incl. the mutation-check → Task 1 Step 5. §7 four-step verification → Task 7 Steps 2-6. §8 shipping checklist: CHANGELOG → Task 6, full suite → Task 7 Step 1, no schema change (correctly no task), no deploy needed (noted). §3 non-goals → the `FORBIDDEN` guard test in Task 1.

**Type consistency:** `-MaxAgeHours` / `--max-age-hours`, `-MaxUsedSpaceGB` / `--max-used-space-gb`, `-DryRun` / `--dry-run`, `-NoTrim` / `--no-trim` are used identically in Tasks 1, 2, 3, 4 and 7 and bridged by `_SH_FLAGS`. `-KeepCacheHours` / `KEEP_CACHE_HOURS` appear only in Task 3 and its tests. `ConvertFrom-DockerSize` / `docker_size_to_bytes` and `Get-BuildCacheBytes` / `build_cache_bytes` are each defined once and called only within their own script.

**Known gap, accepted:** Task 4 (`install-cache-retention.ps1`) has no automated test. Registering a real Scheduled Task is a machine-level side effect the stubbed-`docker` harness cannot model, and mocking `Register-ScheduledTask` would test the mock. It is covered by a parse check plus a live register-and-query in Steps 2-3.
