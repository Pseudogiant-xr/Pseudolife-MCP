"""``ops/update.ps1|.sh`` stamp the build and refuse a dirty tree.

Two findings of the 2026-09-23 fresh-eyes review:

* ``/health`` could not name the deployed commit, and the main checkout sat
  two merges behind the deployed image, so "what is live?" had no answer.
  The scripts now pass the checkout's HEAD, a dirty flag and the build time
  to the image build (``PSEUDOLIFE_BUILD_*`` -> compose build args ->
  image labels + ``/health``'s ``build`` block).
* The maintainer's main checkout routinely carries another session's
  uncommitted work. Deploying from it would ship that work under a commit
  that does not contain it, so the scripts refuse a tree with uncommitted
  or untracked files, or one whose state git cannot report, unless given
  ``-AllowDirty`` / ``--allow-dirty``, which deploys it stamped dirty.

Drives the REAL scripts with ``git``, ``docker`` and the health probe
stubbed as shell functions (``tests/ops_harness.py``), like the rollback
tests beside this file.
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

_SHA = "0123456789abcdef0123456789abcdef01234567"
_DIRTY = [" M pseudolife_memory/daemon.py", "?? notes-from-another-session.md"]
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _ps_git(*, repo: bool, dirty: list[str]) -> str:
    rev = (f'$global:LASTEXITCODE = 0; return "{_SHA}"' if repo
           else "$global:LASTEXITCODE = 128; return")
    lines = ", ".join(f"'{line}'" for line in dirty) or ""
    return f'''
function global:git {{
    $a = @($args | ForEach-Object {{ "$_" }})
    Add-Content $global:CallsLog ("git " + ($a -join ' '))
    if ($a -contains "rev-parse") {{ {rev} }}
    if ($a -contains "status") {{ $global:LASTEXITCODE = 0; return @({lines}) }}
    $global:LASTEXITCODE = 0
}}
'''


# `docker compose` records the stamp it was handed; every other call succeeds
# quietly (no version-tag image, so no rollback tag, which is irrelevant here).
_PS_DOCKER = '''
function global:docker {
    $a = @($args | ForEach-Object { "$_" })
    Add-Content $global:CallsLog ("docker " + ($a -join ' '))
    if ($a[0] -eq "compose") {
        Set-Content -LiteralPath (Join-Path $ScenarioDir "stamp.txt") -Value @(
            "sha=$env:PSEUDOLIFE_BUILD_GIT_SHA",
            "dirty=$env:PSEUDOLIFE_BUILD_DIRTY",
            "time=$env:PSEUDOLIFE_BUILD_TIME")
    }
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") { $global:LASTEXITCODE = 1; return }
    $global:LASTEXITCODE = 0
}
function global:Invoke-RestMethod { @{ status = 'ok'; schema = 42; persist_errors = 0 } }
'''

# After the run, record whether the caller's own values survived: one was
# set before the deploy, one was absent.
_PS_AFTER = '''
Set-Content -LiteralPath (Join-Path $ScenarioDir "after.txt") -Value @(
    "sha=$([Environment]::GetEnvironmentVariable('PSEUDOLIFE_BUILD_GIT_SHA', 'Process'))",
    "dirty_present=$(Test-Path Env:\\PSEUDOLIFE_BUILD_DIRTY)")
'''

_PS_SCENARIOS = {
    "clean": (_ps_git(repo=True, dirty=[]), ""),
    "dirty_refused": (_ps_git(repo=True, dirty=_DIRTY), ""),
    "dirty_allowed": (_ps_git(repo=True, dirty=_DIRTY), "-AllowDirty"),
    "not_a_repo_refused": (_ps_git(repo=False, dirty=[]), ""),
    "not_a_repo_allowed": (_ps_git(repo=False, dirty=[]), "-AllowDirty"),
}


@pytest.fixture(scope="module")
def deploys(tmp_path_factory):
    if PWSH is None:
        pytest.skip("pwsh not available")
    root = tmp_path_factory.mktemp("update_build_stamp_ps1")
    scenarios = []
    for name, (git, extra) in _PS_SCENARIOS.items():
        sdir = scenario_dir(root, name)
        (sdir / "calls.log").write_text("", encoding="utf-8")
        setup = (f'$global:CallsLog = "{sdir / "calls.log"}"\n'
                 "$env:PSEUDOLIFE_BUILD_GIT_SHA = 'caller-value'\n"
                 "Remove-Item Env:\\PSEUDOLIFE_BUILD_DIRTY -ErrorAction SilentlyContinue\n"
                 f"{git}{_PS_DOCKER}")
        invoke = (f'& {{ & "{UPDATE_PS1}" -NoBackup -NoCachePrune -Tag unittest '
                  f'-HealthRetries 2 -HealthDelayMs 50 {extra}; {_PS_AFTER} }}')
        scenarios.append(Scenario(name, setup, invoke,
                                  exit_code=EXIT_FROM_LASTEXITCODE))
    return run_ps1_batch(root, scenarios)


def _stamp(res) -> dict[str, str]:
    return dict(line.split("=", 1) for line in res.lines("stamp.txt"))


def _docker_calls(res) -> list[str]:
    return [c for c in res.lines("calls.log") if c.startswith("docker ")]


def test_a_clean_tree_is_deployed_with_its_commit(deploys):
    res = deploys["clean"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert stamp["sha"] == _SHA, res.detail()
    assert stamp["dirty"] == "false", res.detail()
    assert _RFC3339.match(stamp["time"]), res.detail()


def test_a_dirty_tree_is_refused_before_anything_runs(deploys):
    res = deploys["dirty_refused"]
    out = res.stdout + res.stderr
    assert res.returncode == 1, res.detail()
    assert _docker_calls(res) == [], (
        "the refusal came after docker had already been called" + res.detail())
    assert "REFUSING to deploy" in out, res.detail()
    assert "-AllowDirty" in out, "the refusal does not name the override" + res.detail()
    for line in _DIRTY:
        assert line.strip().split()[-1] in out, (
            "the refusal does not show what is dirty" + res.detail())


def test_allow_dirty_deploys_and_stamps_the_image_dirty(deploys):
    # PowerShell scripts absorb an undeclared -Foo into $args, so a script
    # without the switch would silently refuse; pin the declaration too.
    assert re.search(r"\[switch\]\$AllowDirty", UPDATE_PS1.read_text(encoding="utf-8"))
    res = deploys["dirty_allowed"]
    assert res.returncode == 0, res.detail()
    assert _stamp(res)["dirty"] == "true", res.detail()
    assert "-AllowDirty: deploying anyway" in res.stdout + res.stderr, res.detail()


def test_a_tree_git_cannot_describe_is_refused_too(deploys):
    res = deploys["not_a_repo_refused"]
    assert res.returncode == 1, res.detail()
    assert _docker_calls(res) == [], res.detail()
    assert "cannot tell whether" in res.stdout + res.stderr, res.detail()


def test_allow_dirty_on_a_non_repository_stamps_unknown(deploys):
    res = deploys["not_a_repo_allowed"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert (stamp["sha"], stamp["dirty"]) == ("unknown", "unknown"), res.detail()


def test_the_callers_environment_is_left_as_it_was(deploys):
    for name in ("clean", "dirty_allowed"):
        after = dict(line.split("=", 1) for line in deploys[name].lines("after.txt"))
        assert after == {"sha": "caller-value", "dirty_present": "False"}, (
            deploys[name].detail())


def test_the_guard_runs_before_the_backup():
    # Execution tests run with -NoBackup (a real backup must never run from
    # a test), so the ordering against step 1 is pinned on the text.
    for path, guard, backup in (
            (UPDATE_PS1, "REFUSING to deploy", 'Step "Backing up the bank'),
            (UPDATE_SH, "REFUSING to deploy", 'step "Backing up the bank')):
        text = path.read_text(encoding="utf-8")
        assert text.index(guard) < text.index(backup), path.name


# --- bash port -------------------------------------------------------------

def _sh_git(dirty: list[str]) -> str:
    body = "".join(f"echo '{line}'; " for line in dirty) or ":"
    return f'''
git() {{
    echo "git $*" >> "$CALLS"
    case "$*" in
        *rev-parse*) echo {_SHA} ;;
        *status*) {body} ;;
    esac
}}
'''


_SH_DOCKER = '''
docker() {
    echo "docker $*" >> "$CALLS"
    if [ "$1" = compose ]; then
        printf 'sha=%s\\ndirty=%s\\ntime=%s\\n' "${PSEUDOLIFE_BUILD_GIT_SHA:-}" \\
            "${PSEUDOLIFE_BUILD_DIRTY:-}" "${PSEUDOLIFE_BUILD_TIME:-}" > "$SCENARIO_DIR/stamp.txt"
    fi
    return 0
}
curl() { echo '{"status":"ok","schema":42,"persist_errors":0}'; }
export -f git docker curl
'''


@pytest.fixture(scope="module")
def sh_deploys(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("update_build_stamp_sh")
    specs = {"sh_clean": ([], ""), "sh_dirty_refused": (_DIRTY, ""),
             "sh_dirty_allowed": (_DIRTY, "--allow-dirty")}
    scenarios = []
    for name, (dirty, extra) in specs.items():
        sdir = scenario_dir(root, name)
        (sdir / "calls.log").write_text("", encoding="utf-8")
        setup = (f'export CALLS="{(sdir / "calls.log").as_posix()}"\n'
                 "export HEALTH_RETRIES=2\nexport HEALTH_DELAY_MS=50\n"
                 f"export SCENARIO_DIR\n{_sh_git(dirty)}{_SH_DOCKER}")
        invoke = (f'bash "{UPDATE_SH.as_posix()}" --no-backup --no-cache-prune '
                  f"--tag unittest {extra}")
        scenarios.append(Scenario(name, setup, invoke))
    return run_sh_batch(root, scenarios)


def test_update_sh_stamps_a_clean_tree(sh_deploys):
    res = sh_deploys["sh_clean"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert (stamp["sha"], stamp["dirty"]) == (_SHA, "false"), res.detail()
    assert _RFC3339.match(stamp["time"]), res.detail()


def test_update_sh_refuses_a_dirty_tree(sh_deploys):
    res = sh_deploys["sh_dirty_refused"]
    assert res.returncode == 1, res.detail()
    assert _docker_calls(res) == [], res.detail()
    assert "--allow-dirty" in res.stderr, res.detail()


def test_update_sh_allow_dirty_stamps_dirty(sh_deploys):
    res = sh_deploys["sh_dirty_allowed"]
    assert res.returncode == 0, res.detail()
    assert _stamp(res)["dirty"] == "true", res.detail()
