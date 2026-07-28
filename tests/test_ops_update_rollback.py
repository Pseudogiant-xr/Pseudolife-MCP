"""``ops/update.ps1|.sh`` must not promise a rollback it did not create.

Both scripts compute ``$rollback`` unconditionally but only ``docker tag``
it when a current image exists. When it doesn't — a first build, or a
version bump whose image was never built — the tag is skipped with a
warning, and then BOTH exit paths print
``docker tag <rollback> <image_tag>`` anyway.

Seen live on 2026-07-26: the deploy warned "No current
pseudolife-daemon:0.10.0 image to tag" and then printed a rollback
command referencing a tag that does not exist. The dangerous path is the
unhealthy one — the operator is handed a command that fails at exactly
the moment the deploy just broke.

Drives the REAL script with ``docker`` and ``Invoke-RestMethod`` stubbed
as PowerShell functions (functions shadow both an exe and a cmdlet on
command lookup), so the script's own branching is what's under test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
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

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not available")


def _run_update(tmp_path: Path, *, image_exists: bool, healthy: bool = True):
    """Run update.ps1 with docker + the health probe stubbed."""
    driver = tmp_path / "driver.ps1"
    inspect_rc = "0" if image_exists else "1"
    health = ("@{ status = 'ok'; schema = 22; persist_errors = 0 }"
              if healthy else "throw 'connection refused'")
    driver.write_text(
        f"""
function global:docker {{
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") {{
        $global:LASTEXITCODE = {inspect_rc}
        if ({inspect_rc} -ne 0) {{ return }}
        return "[]"
    }}
    # tag / compose / anything else: succeed quietly
    $global:LASTEXITCODE = 0
    return
}}
function global:Invoke-RestMethod {{ {health} }}
& "{UPDATE_PS1}" -NoBackup -Tag unittest 2>&1 | ForEach-Object {{ "$_" }}
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=180,
    )
    return proc.stdout + proc.stderr


def test_no_rollback_image_means_no_rollback_promise(tmp_path):
    """The reported defect: a tag command for a tag that was never made."""
    out = _run_update(tmp_path, image_exists=False)

    assert "docker tag pseudolife-daemon" not in out, (
        "promised a rollback tag that was never created:\n" + out)


def test_no_rollback_image_says_so_and_offers_a_real_path(tmp_path):
    """Silence would be worse than the wrong command — the operator still
    needs to know how to get back."""
    out = _run_update(tmp_path, image_exists=False)

    assert "no rollback image" in out.lower(), out
    assert "ops" in out and "update" in out, (
        "no rebuild-from-git fallback offered:\n" + out)


def test_rollback_image_present_still_prints_the_tag_command(tmp_path):
    """The working case must keep working."""
    out = _run_update(tmp_path, image_exists=True)

    assert "docker tag pseudolife-daemon:0.10.0-unittest" in out, out


def test_unhealthy_deploy_without_a_rollback_image_is_honest(tmp_path):
    """The path that matters most: the deploy just failed, so a rollback
    instruction that cannot work is actively harmful."""
    out = _run_update(tmp_path, image_exists=False, healthy=False)

    assert "docker tag pseudolife-daemon" not in out, (
        "handed the operator a failing rollback mid-incident:\n" + out)
    assert "no rollback image" in out.lower(), out


def test_update_sh_guards_the_same_way():
    """The Linux/macOS port carries the identical bug and must carry the
    identical guard."""
    text = UPDATE_SH.read_text(encoding="utf-8")

    assert "rollback_ready" in text, (
        "update.sh does not track whether the rollback tag was created")
    assert "no rollback image" in text.lower(), (
        "update.sh has no honest no-rollback message")


# --- Execution-based reachability: the build-cache retention hook ---------
#
# test_ops_prune_build_cache.py::
# test_cache_prune_runs_after_the_health_check_not_beside_prune_rollbacks
# asserts ordering with ``.index()`` on the raw file text — it proves
# prune-build-cache.ps1/.sh appear textually after the health-check and
# build anchors, not that the unhealthy branch can't reach them. A
# regression that moved the retention call inside the unhealthy
# ``else { ...; exit 1 }`` branch (pruning the cache after a FAILED deploy —
# exactly what the design doc warns against) would still satisfy every
# ``.index()`` assertion there, because that branch's body is textually
# after both anchors too.
#
# These tests instead run the real unhealthy path and prove the cache-prune
# primitive's own signature docker call — ``docker system df``, issued only
# by prune-build-cache's Get-BuildCacheBytes/build_cache_bytes, never by
# prune-rollbacks (which legitimately runs before the health check on every
# deploy, healthy or not) — never fires.


def _run_update_ps1_with_docker_log(tmp_path: Path, *, image_exists: bool, healthy: bool):
    """Like ``_run_update``, but also logs every ``docker`` invocation made
    by update.ps1 and everything it calls (prune-rollbacks.ps1, and — if a
    regression moved it there — prune-build-cache.ps1), so a test can assert
    on which docker commands actually ran rather than trusting the script's
    own exit path. Returns ``(proc, docker_calls)``."""
    calls_log = tmp_path / "docker_calls.log"
    calls_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver_reach.ps1"
    inspect_rc = "0" if image_exists else "1"
    health = ("@{ status = 'ok'; schema = 22; persist_errors = 0 }"
              if healthy else "throw 'connection refused'")
    driver.write_text(
        f"""
function global:docker {{
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content "{calls_log}" ($a -join ' ')
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") {{
        $global:LASTEXITCODE = {inspect_rc}
        if ({inspect_rc} -ne 0) {{ return }}
        return "[]"
    }}
    $global:LASTEXITCODE = 0
    return
}}
function global:Invoke-RestMethod {{ {health} }}
& "{UPDATE_PS1}" -NoBackup -Tag unittest
exit $LASTEXITCODE
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=180,
    )
    calls = [ln for ln in calls_log.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return proc, calls


def test_unhealthy_deploy_never_runs_build_cache_retention(tmp_path):
    """Execution proof for the 2026-07-28 build-cache-retention ordering:
    drive the real unhealthy path (health check never reports 'ok') and
    assert prune-build-cache.ps1's own docker probe never fires. A
    regression that moved the retention call inside the unhealthy branch
    would make this go red even though the textual .index() test in
    test_ops_prune_build_cache.py would stay green."""
    proc, calls = _run_update_ps1_with_docker_log(
        tmp_path, image_exists=True, healthy=False)
    assert proc.returncode != 0, (
        "an unhealthy deploy must exit non-zero:\n" + proc.stdout + proc.stderr)
    assert not any("system df" in c for c in calls), (
        "prune-build-cache.ps1 ran on the unhealthy path (its 'docker "
        f"system df' probe fired) — calls: {calls}\n"
        f"output:\n{proc.stdout}{proc.stderr}"
    )


def _run_update_sh_with_docker_log(tmp_path: Path, *, image_exists: bool):
    """bash counterpart of ``_run_update_ps1_with_docker_log``. ``curl`` is
    stubbed to always fail (connection refused) so the health loop can never
    succeed — the same unhealthy path the ps1 test drives — without ever
    touching real infrastructure. Returns ``(proc, docker_calls)``."""
    calls_log = tmp_path / "docker_calls.log"
    calls_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver_reach.sh"
    inspect_rc = "0" if image_exists else "1"
    driver.write_text(
        f'''#!/usr/bin/env bash
set -u
export CALLS="{calls_log.as_posix()}"
docker() {{
    echo "$*" >> "$CALLS"
    if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
        return {inspect_rc}
    fi
    return 0
}}
curl() {{
    return 7
}}
export -f docker
export -f curl
bash "{UPDATE_SH.as_posix()}" --no-backup --tag unittest
rc=$?
exit $rc
''',
        encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [BASH, str(driver)],
        capture_output=True, text=True, timeout=180,
    )
    calls = [ln for ln in calls_log.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return proc, calls


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_update_sh_unhealthy_deploy_never_runs_build_cache_retention(tmp_path):
    """bash port of test_unhealthy_deploy_never_runs_build_cache_retention:
    same execution-based reachability proof, driving the real update.sh with
    docker and curl stubbed so the health loop never succeeds."""
    proc, calls = _run_update_sh_with_docker_log(tmp_path, image_exists=True)
    assert proc.returncode != 0, (
        "an unhealthy deploy must exit non-zero:\n" + proc.stdout + proc.stderr)
    assert not any("system df" in c for c in calls), (
        "prune-build-cache.sh ran on the unhealthy path (its 'docker "
        f"system df' probe fired) — calls: {calls}\n"
        f"output:\n{proc.stdout}{proc.stderr}"
    )
