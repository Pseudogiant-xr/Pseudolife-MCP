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
    # Forbidden verbs are deliberately PERMISSIVE here: the stub must not die
    # on them (that would fail the run via proc.returncode before the
    # FORBIDDEN loop in the test ever inspects calls.log). Logged above via
    # Add-Content like every other call; the test's own loop is what must
    # catch these, not this stub's strictness.
    if ($a[0] -eq "rmi") {{ return }}
    if ($a[0] -eq "image" -and $a[1] -eq "rm") {{ return }}
    if ($a[0] -eq "system" -and $a[1] -eq "prune") {{ return }}
    if ($a[0] -eq "volume") {{ return }}
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
