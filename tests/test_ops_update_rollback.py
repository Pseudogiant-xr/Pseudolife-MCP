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

import re
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


def _compose_daemon_version() -> str:
    """The daemon image tag compose declares — the single source of truth the
    update scripts read.

    Derived, not hard-coded: this assertion used to pin a literal version and
    so went red on every release cut, making the version bump look like a
    regression in the rollback path. That is a sixth file coupled to the
    "five-file version cut", and the one nobody lists.
    """
    text = (Path(__file__).resolve().parents[1]
            / "ops" / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r"^\s*image:\s*pseudolife-daemon:(\S+)\s*$",
                  text, re.MULTILINE)
    assert m, "no `image: pseudolife-daemon:<tag>` line in ops/docker-compose.yml"
    return m.group(1)


def test_rollback_image_present_still_prints_the_tag_command(tmp_path):
    """The working case must keep working."""
    out = _run_update(tmp_path, image_exists=True)

    version = _compose_daemon_version()
    assert f"docker tag pseudolife-daemon:{version}-unittest" in out, out


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

    # ``rollback_state`` succeeded ``rollback_ready`` when the tag-move guard
    # landed (#183); either name means the script tracks the outcome rather
    # than assuming it.
    assert "rollback_state" in text or "rollback_ready" in text, (
        "update.sh does not track whether the rollback tag was created")
    assert "no rollback image" in text.lower(), (
        "update.sh has no honest no-rollback message")


# --- A re-run after a failed deploy must not destroy the rollback (#183) ---
#
# The rollback tag was moved onto whatever the version tag pointed at, at the
# START of every run — but the compose step BUILDS first and validates after,
# so a run that aborts between the build and a completed deploy leaves the
# version tag on the freshly built (unvalidated) image. The natural next move
# — re-run update.ps1 — then tagged THAT image as the rollback, destroying the
# only pointer to the last-good image. Happened live on 2026-08-13.
#
# The tell is cheap: the RUNNING daemon container still holds the last-good
# image, so its image ID and the version tag's image ID disagree exactly when
# a build has run without a completed deploy.


def _run_update_ids(tmp_path: Path, *, tag_id: str | None, running_id: str | None,
                    extra: str = ""):
    """Run update.ps1 with the two image IDs stubbed, logging docker calls.

    ``tag_id`` is what ``docker image inspect <version-tag>`` resolves to
    (None = no such image); ``running_id`` is what ``docker inspect`` reports
    for the running daemon container (None = not running / unresolvable).
    Returns ``(proc, docker_calls)``.
    """
    calls_log = tmp_path / "docker_calls.log"
    calls_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver_ids.ps1"
    tag_body = (f'$global:LASTEXITCODE = 0; return "{tag_id}"' if tag_id
                else "$global:LASTEXITCODE = 1; return")
    run_body = (f'$global:LASTEXITCODE = 0; return "{running_id}"' if running_id
                else "$global:LASTEXITCODE = 1; return")
    driver.write_text(
        f"""
function global:docker {{
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content "{calls_log}" ($a -join ' ')
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") {{ {tag_body} }}
    if ($a[0] -eq "inspect") {{ {run_body} }}
    $global:LASTEXITCODE = 0
    return
}}
function global:Invoke-RestMethod {{ @{{ status = 'ok'; schema = 22; persist_errors = 0 }} }}
& "{UPDATE_PS1}" -NoBackup -Tag unittest {extra}
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


def _tag_calls(calls: list[str]) -> list[str]:
    """`docker tag ...` calls only — `docker image inspect` must not count."""
    return [c for c in calls if c.split()[:1] == ["tag"]]


def test_rollback_tag_is_not_moved_onto_an_unvalidated_build(tmp_path):
    """The 2026-08-13 defect: the version tag already points at a freshly
    built image (a previous run aborted after its build), so moving the
    rollback tag now would destroy the last-good pointer."""
    proc, calls = _run_update_ids(
        tmp_path, tag_id="sha256:newbuild", running_id="sha256:lastgood")
    out = proc.stdout + proc.stderr
    assert _tag_calls(calls) == [], (
        f"the rollback tag was moved onto an unvalidated build: {calls}\n{out}")
    assert "rollback" in out.lower(), out


def test_refusal_explains_itself_and_offers_a_way_forward(tmp_path):
    """Silently not tagging would be its own trap — the operator has to learn
    that the running image and the version tag disagree, and how to override."""
    proc, _ = _run_update_ids(
        tmp_path, tag_id="sha256:newbuild", running_id="sha256:lastgood")
    out = (proc.stdout + proc.stderr).lower()
    assert "forcerollbacktag" in out, (
        "the refusal does not name the override flag:\n" + out)


def test_force_flag_moves_the_tag_anyway(tmp_path):
    """The escape hatch must actually work — some mismatches are legitimate
    (an image rebuilt by hand, a deliberately re-pointed tag).

    The declaration is asserted separately because the execution half cannot
    fail on its own: a PowerShell *script* (no [CmdletBinding()]) absorbs an
    unrecognized ``-Foo`` into ``$args`` instead of erroring, so a script
    without the switch would happily tag and pass.
    """
    assert re.search(r"\[switch\]\$ForceRollbackTag", UPDATE_PS1.read_text(
        encoding="utf-8")), "update.ps1 does not declare -ForceRollbackTag"

    proc, calls = _run_update_ids(
        tmp_path, tag_id="sha256:newbuild", running_id="sha256:lastgood",
        extra="-ForceRollbackTag")
    out = proc.stdout + proc.stderr
    assert _tag_calls(calls), f"-ForceRollbackTag did not tag: {calls}\n{out}"


def test_matching_ids_tag_exactly_as_before(tmp_path):
    """The happy path — running daemon and version tag are the same image —
    must be untouched."""
    proc, calls = _run_update_ids(
        tmp_path, tag_id="sha256:same", running_id="sha256:same")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert _tag_calls(calls), f"the happy path stopped tagging: {calls}\n{out}"


def test_absent_daemon_container_still_tags(tmp_path):
    """No daemon container at all (fresh install, or it was removed) leaves
    nothing to compare; refusing there would break the first deploy. Note a
    STOPPED container still answers `docker inspect`, so it stays guarded."""
    proc, calls = _run_update_ids(
        tmp_path, tag_id="sha256:present", running_id=None)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert _tag_calls(calls), (
        f"a stopped daemon blocked the rollback tag: {calls}\n{out}")


def test_update_sh_carries_the_same_guard():
    """The Linux/macOS port must not keep the destructive behavior."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "force-rollback-tag" in text, (
        "update.sh has no --force-rollback-tag override")
    assert "{{.Image}}" in text, (
        "update.sh never resolves the running daemon container's image ID, so "
        "it cannot tell a built-but-undeployed image from the deployed one")


# --- Execution-based reachability: the build-cache retention hook ---------
#
# test_ops_prune_build_cache.py::test_rollback_retention_still_runs_before_the_build
# pins only prune-rollbacks' placement with ``.index()`` on the raw file
# text. Textual ordering cannot prove the build-cache hook's placement: a
# regression that moved the retention call inside the unhealthy
# ``else { ...; exit 1 }`` branch (pruning the cache after a FAILED deploy —
# exactly what the design doc warns against) would still sit textually after
# both the health-check and build anchors.
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


# --- The non-fatal wrapper itself: does a PRUNE FAILURE actually stay ------
# --- non-fatal on an otherwise-healthy deploy? -----------------------------
#
# Spec Sec6 claims this is pinned; it wasn't. The tests above only prove
# retention is *skipped* on the unhealthy path (a reachability property).
# Neither they nor test_ops_prune_build_cache.py ever drive prune-build-
# cache to actually FAIL while the deploy is otherwise healthy — which is
# the one scenario the try/catch (ps1) / `if !` (sh) wrapper exists for.
#
# `docker system df` is prune-build-cache's own first call (Get-
# BuildCacheBytes / build_cache_bytes) and is not used by anything else on
# the healthy path (prune-rollbacks never issues it — see the comment
# above), so failing only that call isolates the prune's own failure
# without disturbing the rest of a healthy deploy.


def test_build_cache_prune_failure_does_not_fail_a_healthy_deploy(tmp_path):
    """A build-cache-retention failure must not fail a deploy that already
    reported healthy. Stub `docker system df` to fail (LASTEXITCODE 1),
    keep the health probe reporting 'ok', and assert update.ps1 still exits
    0 with the non-fatal warning emitted — the try/catch's one job."""
    driver = tmp_path / "driver_cachefail.ps1"
    driver.write_text(
        f"""
function global:docker {{
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") {{
        $global:LASTEXITCODE = 0
        return "[]"
    }}
    if ($a[0] -eq "system" -and $a[1] -eq "df") {{
        $global:LASTEXITCODE = 1
        return
    }}
    $global:LASTEXITCODE = 0
    return
}}
function global:Invoke-RestMethod {{ @{{ status = 'ok'; schema = 22; persist_errors = 0 }} }}
& "{UPDATE_PS1}" -NoBackup -Tag unittest 2>&1 | ForEach-Object {{ "$_" }}
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "a build-cache-retention failure must not fail an otherwise-"
        f"healthy deploy:\n{out}")
    assert "build-cache retention failed" in out.lower(), (
        f"expected the non-fatal warning to be emitted:\n{out}")


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_update_sh_build_cache_prune_failure_does_not_fail_a_healthy_deploy(tmp_path):
    """bash port: same proof, driving the real update.sh with `docker
    system df` stubbed to fail and `curl` stubbed to report healthy."""
    driver = tmp_path / "driver_cachefail.sh"
    driver.write_text(
        f'''#!/usr/bin/env bash
set -u
docker() {{
    if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
        return 0
    fi
    if [ "$1" = "system" ] && [ "$2" = "df" ]; then
        return 1
    fi
    return 0
}}
curl() {{
    echo '{{"status":"ok","schema":22,"persist_errors":0}}'
    return 0
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
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "a build-cache-retention failure must not fail an otherwise-"
        f"healthy deploy:\n{out}")
    assert "build-cache retention failed" in out.lower(), (
        f"expected the non-fatal warning to be emitted:\n{out}")
