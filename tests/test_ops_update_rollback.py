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

Two runtime notes:

* the three scenarios that drive the UNHEALTHY path used to spend the
  script's full production health budget — 30 attempts 1.5s apart, ~46s
  each, 137s of the suite. They pass ``-HealthRetries``/``-HealthDelayMs``
  (``HEALTH_RETRIES``/``HEALTH_DELAY_MS`` for the bash port), whose
  defaults are still 30/1500, so the gate is exercised in full, twice,
  in a tenth of a second;
* every scenario runs inside one interpreter per shell
  (``tests/ops_harness.py``), each in its own sandbox with its own docker
  call log and its own exit code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ops_harness import (
    BASH,
    EXIT_FROM_LASTEXITCODE,
    PWSH,
    Scenario,
    run_ps1_batch,
    run_sh_batch,
    scenario_dir,
)

REPO = Path(__file__).resolve().parents[1]
UPDATE_PS1 = REPO / "ops" / "update.ps1"
UPDATE_SH = REPO / "ops" / "update.sh"

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not available")

# Health-wait budget for the stubbed runs. Two attempts is still a loop the
# unhealthy branch has to exhaust — the retries-exhausted path is what these
# tests assert — and 50ms keeps the whole file under a second.
_FAST_HEALTH_PS = "-HealthRetries 2 -HealthDelayMs 50"
_FAST_HEALTH_SH = "export HEALTH_RETRIES=2\nexport HEALTH_DELAY_MS=50"

_HEALTHY = "@{ status = 'ok'; schema = 22; persist_errors = 0 }"
_UNHEALTHY = "throw 'connection refused'"


def _health_stub(healthy: bool) -> str:
    body = _HEALTHY if healthy else _UNHEALTHY
    return f"function global:Invoke-RestMethod {{ {body} }}\n"


def _image_stub(*, image_exists: bool, system_df_fails: bool = False) -> str:
    """``docker image inspect <version tag>`` resolves (or not); every other
    call succeeds quietly. Optionally fail ``docker system df``, which is
    prune-build-cache's own first call and nothing else's."""
    inspect_rc = "0" if image_exists else "1"
    df_branch = ('''    if ($a[0] -eq "system" -and $a[1] -eq "df") {
        $global:LASTEXITCODE = 1
        return
    }
''' if system_df_fails else "")
    return f'''
function global:docker {{
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content $global:CallsLog ($a -join ' ')
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") {{
        $global:LASTEXITCODE = {inspect_rc}
        if ({inspect_rc} -ne 0) {{ return }}
        return "[]"
    }}
{df_branch}    # tag / compose / anything else: succeed quietly
    $global:LASTEXITCODE = 0
    return
}}
'''


def _ids_stub(tag_id: str | None, running_id: str | None) -> str:
    """``tag_id`` is what ``docker image inspect <version-tag>`` resolves to
    (None = no such image); ``running_id`` is what ``docker inspect`` reports
    for the running daemon container (None = not running / unresolvable)."""
    tag_body = (f'$global:LASTEXITCODE = 0; return "{tag_id}"' if tag_id
                else "$global:LASTEXITCODE = 1; return")
    run_body = (f'$global:LASTEXITCODE = 0; return "{running_id}"' if running_id
                else "$global:LASTEXITCODE = 1; return")
    return f'''
function global:docker {{
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content $global:CallsLog ($a -join ' ')
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") {{ {tag_body} }}
    if ($a[0] -eq "inspect") {{ {run_body} }}
    $global:LASTEXITCODE = 0
    return
}}
'''


# name -> (docker stub, health stub, extra update.ps1 args, exit-code model).
#
# The exit-code model is per scenario because the old per-scenario drivers
# disagreed: the ``_run_update_ids`` family ended in ``exit $LASTEXITCODE``
# (update.ps1's unhealthy path exits 1, and that is the assertion), while the
# others let pwsh's own ``-File`` rule stand — a completed script exits 0 even
# when a stubbed docker call left $LASTEXITCODE non-zero. That distinction is
# load-bearing for test_build_cache_prune_failure_does_not_fail_a_healthy_
# deploy, where the deploy succeeds while the retention primitive fails.
_PS_SCENARIOS: dict[str, tuple[str, str, str, str | None]] = {
    "no_rollback_image_healthy": (
        _image_stub(image_exists=False), _health_stub(True), "", None),
    "rollback_image_present": (
        _image_stub(image_exists=True), _health_stub(True), "", None),
    "no_rollback_image_unhealthy": (
        _image_stub(image_exists=False), _health_stub(False), "", None),
    "unhealthy_with_docker_log": (
        _image_stub(image_exists=True), _health_stub(False), "",
        EXIT_FROM_LASTEXITCODE),
    "cache_prune_failure": (
        _image_stub(image_exists=True, system_df_fails=True),
        _health_stub(True), "", None),
    "ids_mismatch": (
        _ids_stub("sha256:newbuild", "sha256:lastgood"), _health_stub(True),
        "", EXIT_FROM_LASTEXITCODE),
    "ids_mismatch_forced": (
        _ids_stub("sha256:newbuild", "sha256:lastgood"), _health_stub(True),
        "-ForceRollbackTag", EXIT_FROM_LASTEXITCODE),
    "ids_match": (
        _ids_stub("sha256:same", "sha256:same"), _health_stub(True), "",
        EXIT_FROM_LASTEXITCODE),
    "no_daemon_container": (
        _ids_stub("sha256:present", None), _health_stub(True), "",
        EXIT_FROM_LASTEXITCODE),
}


def _ps1_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (docker, health, extra, exit_code) in _PS_SCENARIOS.items():
        sdir = scenario_dir(root, name)
        calls_log = sdir / "calls.log"
        calls_log.write_text("", encoding="utf-8")
        setup = f'$global:CallsLog = "{calls_log}"\n{docker}{health}'
        invoke = (f'& "{UPDATE_PS1}" -NoBackup -Tag unittest '
                  f'{_FAST_HEALTH_PS} {extra}')
        scenarios.append(Scenario(name, setup, invoke, exit_code=exit_code))
    return scenarios


def _sh_scenarios(root: Path) -> list[Scenario]:
    """The bash port's two execution scenarios.

    ``curl`` failing (exit 7, connection refused) is the unhealthy path;
    ``docker system df`` failing isolates prune-build-cache's own failure on
    an otherwise-healthy deploy. Neither touches real infrastructure.
    """
    specs = {
        "sh_unhealthy_with_docker_log": ("return 7", "return 0"),
        "sh_cache_prune_failure": (
            '''echo '{"status":"ok","schema":22,"persist_errors":0}'
    return 0''', "return 1"),
    }
    scenarios = []
    for name, (curl_body, df_body) in specs.items():
        sdir = scenario_dir(root, name)
        calls_log = sdir / "calls.log"
        calls_log.write_text("", encoding="utf-8")
        setup = f'''
{_FAST_HEALTH_SH}
export CALLS="{calls_log.as_posix()}"
docker() {{
    echo "$*" >> "$CALLS"
    if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
        return 0
    fi
    if [ "$1" = "system" ] && [ "$2" = "df" ]; then
        {df_body}
    fi
    return 0
}}
curl() {{
    {curl_body}
}}
export -f docker
export -f curl
'''
        invoke = f'bash "{UPDATE_SH.as_posix()}" --no-backup --tag unittest'
        scenarios.append(Scenario(name, setup, invoke))
    return scenarios


@pytest.fixture(scope="module")
def deploys(tmp_path_factory):
    root = tmp_path_factory.mktemp("update_rollback_ps1")
    return run_ps1_batch(root, _ps1_scenarios(root))


@pytest.fixture(scope="module")
def sh_deploys(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("update_rollback_sh")
    return run_sh_batch(root, _sh_scenarios(root))


def _out(res) -> str:
    return res.stdout + res.stderr


def _tag_calls(res) -> list[str]:
    """`docker tag ...` calls only — `docker image inspect` must not count."""
    return [c for c in res.lines("calls.log") if c.split()[:1] == ["tag"]]


def test_no_rollback_image_means_no_rollback_promise(deploys):
    """The reported defect: a tag command for a tag that was never made."""
    out = _out(deploys["no_rollback_image_healthy"])

    assert "docker tag pseudolife-daemon" not in out, (
        "promised a rollback tag that was never created:\n" + out)


def test_no_rollback_image_says_so_and_offers_a_real_path(deploys):
    """Silence would be worse than the wrong command — the operator still
    needs to know how to get back."""
    out = _out(deploys["no_rollback_image_healthy"])

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


def test_rollback_image_present_still_prints_the_tag_command(deploys):
    """The working case must keep working."""
    out = _out(deploys["rollback_image_present"])

    version = _compose_daemon_version()
    assert f"docker tag pseudolife-daemon:{version}-unittest" in out, out


def test_unhealthy_deploy_without_a_rollback_image_is_honest(deploys):
    """The path that matters most: the deploy just failed, so a rollback
    instruction that cannot work is actively harmful."""
    out = _out(deploys["no_rollback_image_unhealthy"])

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


def test_rollback_tag_is_not_moved_onto_an_unvalidated_build(deploys):
    """The 2026-08-13 defect: the version tag already points at a freshly
    built image (a previous run aborted after its build), so moving the
    rollback tag now would destroy the last-good pointer."""
    res = deploys["ids_mismatch"]
    assert _tag_calls(res) == [], (
        "the rollback tag was moved onto an unvalidated build" + res.detail())
    assert "rollback" in _out(res).lower(), res.detail()


def test_refusal_explains_itself_and_offers_a_way_forward(deploys):
    """Silently not tagging would be its own trap — the operator has to learn
    that the running image and the version tag disagree, and how to override."""
    out = _out(deploys["ids_mismatch"]).lower()
    assert "forcerollbacktag" in out, (
        "the refusal does not name the override flag:\n" + out)


def test_force_flag_moves_the_tag_anyway(deploys):
    """The escape hatch must actually work — some mismatches are legitimate
    (an image rebuilt by hand, a deliberately re-pointed tag).

    The declaration is asserted separately because the execution half cannot
    fail on its own: a PowerShell *script* (no [CmdletBinding()]) absorbs an
    unrecognized ``-Foo`` into ``$args`` instead of erroring, so a script
    without the switch would happily tag and pass.
    """
    assert re.search(r"\[switch\]\$ForceRollbackTag", UPDATE_PS1.read_text(
        encoding="utf-8")), "update.ps1 does not declare -ForceRollbackTag"

    res = deploys["ids_mismatch_forced"]
    assert _tag_calls(res), "-ForceRollbackTag did not tag" + res.detail()


def test_matching_ids_tag_exactly_as_before(deploys):
    """The happy path — running daemon and version tag are the same image —
    must be untouched."""
    res = deploys["ids_match"]
    assert res.returncode == 0, res.detail()
    assert _tag_calls(res), "the happy path stopped tagging" + res.detail()


def test_absent_daemon_container_still_tags(deploys):
    """No daemon container at all (fresh install, or it was removed) leaves
    nothing to compare; refusing there would break the first deploy. Note a
    STOPPED container still answers `docker inspect`, so it stays guarded."""
    res = deploys["no_daemon_container"]
    assert res.returncode == 0, res.detail()
    assert _tag_calls(res), (
        "a stopped daemon blocked the rollback tag" + res.detail())


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


def test_unhealthy_deploy_never_runs_build_cache_retention(deploys):
    """Execution proof for the 2026-07-28 build-cache-retention ordering:
    drive the real unhealthy path (health check never reports 'ok') and
    assert prune-build-cache.ps1's own docker probe never fires. A
    regression that moved the retention call inside the unhealthy branch
    would make this go red even though the textual .index() test in
    test_ops_prune_build_cache.py would stay green."""
    res = deploys["unhealthy_with_docker_log"]
    assert res.returncode != 0, (
        "an unhealthy deploy must exit non-zero:" + res.detail())
    assert not any("system df" in c for c in res.lines("calls.log")), (
        "prune-build-cache.ps1 ran on the unhealthy path (its 'docker "
        "system df' probe fired)" + res.detail())


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_update_sh_unhealthy_deploy_never_runs_build_cache_retention(sh_deploys):
    """bash port of test_unhealthy_deploy_never_runs_build_cache_retention:
    same execution-based reachability proof, driving the real update.sh with
    docker and curl stubbed so the health loop never succeeds."""
    res = sh_deploys["sh_unhealthy_with_docker_log"]
    assert res.returncode != 0, (
        "an unhealthy deploy must exit non-zero:" + res.detail())
    assert not any("system df" in c for c in res.lines("calls.log")), (
        "prune-build-cache.sh ran on the unhealthy path (its 'docker "
        "system df' probe fired)" + res.detail())


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


def test_build_cache_prune_failure_does_not_fail_a_healthy_deploy(deploys):
    """A build-cache-retention failure must not fail a deploy that already
    reported healthy. Stub `docker system df` to fail (LASTEXITCODE 1),
    keep the health probe reporting 'ok', and assert update.ps1 still exits
    0 with the non-fatal warning emitted — the try/catch's one job."""
    res = deploys["cache_prune_failure"]
    out = _out(res)
    assert res.returncode == 0, (
        "a build-cache-retention failure must not fail an otherwise-"
        f"healthy deploy:\n{out}")
    assert "build-cache retention failed" in out.lower(), (
        f"expected the non-fatal warning to be emitted:\n{out}")


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_update_sh_build_cache_prune_failure_does_not_fail_a_healthy_deploy(
        sh_deploys):
    """bash port: same proof, driving the real update.sh with `docker
    system df` stubbed to fail and `curl` stubbed to report healthy."""
    res = sh_deploys["sh_cache_prune_failure"]
    out = _out(res)
    assert res.returncode == 0, (
        "a build-cache-retention failure must not fail an otherwise-"
        f"healthy deploy:\n{out}")
    assert "build-cache retention failed" in out.lower(), (
        f"expected the non-fatal warning to be emitted:\n{out}")
