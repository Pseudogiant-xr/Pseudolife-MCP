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
  ``-AllowDirty`` / ``--allow-dirty``. They probe again just before the
  build, so a tree that moves during the backup is not stamped wrong.

Each scenario runs a sandbox COPY of the scripts inside a real git
repository (or deliberately outside one), so the real ``git`` arguments are
what is under test and nothing runs against this checkout. ``docker`` and
the health probe are stubbed as shell functions (``tests/ops_harness.py``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ops_harness import (
    BASH,
    EXIT_FROM_LASTEXITCODE,
    PWSH,
    Scenario,
    hermetic_env,
    run_ps1_batch,
    run_sh_batch,
    scenario_dir,
)

REPO = Path(__file__).resolve().parents[1]
UPDATE_PS1 = REPO / "ops" / "update.ps1"
UPDATE_SH = REPO / "ops" / "update.sh"

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_GIT = shutil.which("git")
# Inherited from a git hook or a debugging shell, these would point the
# sandbox's git at another repository or index, or add trace output.
_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_COMMON_DIR", "GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_EVENT",
            "GIT_TEST_ASSUME_DIFFERENT_OWNER")

pytestmark = pytest.mark.skipif(_GIT is None, reason="git not available")


def _clean_git_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in _GIT_ENV:
        env.pop(name, None)
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c",
         "user.email=test@example.com", "-c", "core.autocrlf=false", *args],
        check=True, capture_output=True, text=True, env=_clean_git_env()).stdout.strip()


def _sandbox(sdir: Path, *, repo: bool, dirty: int = 0) -> tuple[Path, str]:
    """A copy of the update scripts at ``sdir/repo``: a committed git
    repository (``repo=True``) or a plain directory. ``dirty`` adds that
    many paths after the commit: one modified tracked file, the rest
    untracked. Returns the sandbox root and its HEAD sha (or "")."""
    root = sdir / "repo"
    ops = root / "ops"
    ops.mkdir(parents=True)
    (ops / "update.ps1").write_bytes(UPDATE_PS1.read_bytes())
    (ops / "update.sh").write_bytes(UPDATE_SH.read_bytes())
    (ops / "docker-compose.yml").write_text(
        "image: pseudolife-daemon:0.1.0\n", encoding="utf-8", newline="\n")
    (ops / "prune-rollbacks.ps1").write_text(
        "param($Keep, $Repository)\n", encoding="utf-8")
    (ops / "prune-rollbacks.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
    (ops / "prune-rollbacks.sh").chmod(0o755)
    (root / "README.md").write_text("sandbox\n", encoding="utf-8", newline="\n")
    sha = ""
    if repo:
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=_clean_git_env())
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "sandbox")
        sha = _git(root, "rev-parse", "HEAD")
    if dirty:
        (root / "README.md").write_text("edited by another session\n",
                                        encoding="utf-8", newline="\n")
        for i in range(dirty - 1):
            (root / f"notes-{i:02d}.md").write_text("x\n", encoding="utf-8")
    return root, sha


def _env(root: Path, **extra) -> dict[str, str]:
    # Never let git discover a repository above the sandboxes.
    scrub = {name: None for name in _GIT_ENV}
    return hermetic_env(GIT_CEILING_DIRECTORIES=str(root), **scrub, **extra)


def _stamp(res) -> dict[str, str]:
    return dict(line.split("=", 1) for line in res.lines("stamp.txt"))


def _compose_calls(res) -> list[str]:
    return [c for c in res.lines("calls.log") if c.startswith("docker compose")]


def _docker_calls(res) -> list[str]:
    return [c for c in res.lines("calls.log") if c.startswith("docker ")]


# --- PowerShell -------------------------------------------------------------

# `docker compose` records the stamp it was handed. With $TouchOnInspect set,
# the step-2 image inspection writes a file into the sandbox: another
# session editing the checkout while the backup ran.
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
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") {
        if ($global:TouchOnInspect) { Set-Content -LiteralPath $global:TouchOnInspect -Value "late edit" }
        $global:LASTEXITCODE = 1; return
    }
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

# name -> (sandbox kwargs, extra args, extra setup)
_PS_SCENARIOS = {
    "clean": (dict(repo=True), "", ""),
    "dirty_refused": (dict(repo=True, dirty=2), "", ""),
    "dirty_allowed": (dict(repo=True, dirty=2), "-AllowDirty", ""),
    "many_dirty_refused": (dict(repo=True, dirty=25), "", ""),
    "not_a_repo_refused": (dict(repo=False), "", ""),
    "not_a_repo_allowed": (dict(repo=False), "-AllowDirty", ""),
    "changed_during_deploy": (dict(repo=True), "", "TOUCH"),
    "foreign_owner_refused": (dict(repo=True), "",
                              "$env:GIT_TEST_ASSUME_DIFFERENT_OWNER = '1'\n"),
    "clean_under_fi_culture": (dict(repo=True), "",
                               "[Threading.Thread]::CurrentThread.CurrentCulture = 'fi-FI'\n"),
    "clean_with_git_trace": (dict(repo=True), "", "$env:GIT_TRACE = '1'\n"),
}


@pytest.fixture(scope="module")
def deploys(tmp_path_factory):
    if PWSH is None:
        pytest.skip("pwsh not available")
    root = tmp_path_factory.mktemp("update_build_stamp_ps1")
    scenarios, shas = [], {}
    for name, (sandbox, extra, extra_setup) in _PS_SCENARIOS.items():
        sdir = scenario_dir(root, name)
        (sdir / "calls.log").write_text("", encoding="utf-8")
        repo, shas[name] = _sandbox(sdir, **sandbox)
        touch = (f'$global:TouchOnInspect = "{repo / "late-edit.txt"}"\n'
                 if extra_setup == "TOUCH" else "$global:TouchOnInspect = $null\n")
        setup = (f'$global:CallsLog = "{sdir / "calls.log"}"\n'
                 "$env:PSEUDOLIFE_BUILD_GIT_SHA = 'caller-value'\n"
                 "Remove-Item Env:\\PSEUDOLIFE_BUILD_DIRTY -ErrorAction SilentlyContinue\n"
                 "Remove-Item Env:\\GIT_TEST_ASSUME_DIFFERENT_OWNER -ErrorAction SilentlyContinue\n"
                 "Remove-Item Env:\\GIT_TRACE -ErrorAction SilentlyContinue\n"
                 "[Threading.Thread]::CurrentThread.CurrentCulture = [Globalization.CultureInfo]::InvariantCulture\n"
                 + touch + (extra_setup if extra_setup != "TOUCH" else "") + _PS_DOCKER)
        # finally: a run that throws must still record the caller's env.
        invoke = (f'& {{ try {{ & "{repo / "ops" / "update.ps1"}" -NoBackup -NoCachePrune '
                  f'-Tag unittest -HealthRetries 2 -HealthDelayMs 50 {extra} }} '
                  f'finally {{ {_PS_AFTER} }} }}')
        scenarios.append(Scenario(name, setup, invoke,
                                  exit_code=EXIT_FROM_LASTEXITCODE))
    run = run_ps1_batch(root, scenarios, env=_env(root))
    run.shas = shas
    return run


def test_a_clean_tree_is_deployed_with_its_commit(deploys):
    res = deploys["clean"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert stamp["sha"] == deploys.shas["clean"], res.detail()
    assert stamp["dirty"] == "false", res.detail()
    assert _RFC3339.match(stamp["time"]), res.detail()


def test_git_chatter_on_stderr_is_not_mistaken_for_dirty_paths(deploys):
    # A successful git can still write warnings or GIT_TRACE lines to
    # stderr; only its stdout is data.
    res = deploys["clean_with_git_trace"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert (stamp["sha"], stamp["dirty"]) == (
        deploys.shas["clean_with_git_trace"], "false"), res.detail()


def test_the_build_time_is_rfc3339_whatever_the_locale(deploys):
    # fi-FI's time separator is '.', and a custom .NET format uses it for ':'.
    res = deploys["clean_under_fi_culture"]
    assert res.returncode == 0, res.detail()
    assert _RFC3339.match(_stamp(res)["time"]), res.detail()


def test_a_dirty_tree_is_refused_before_anything_runs(deploys):
    res = deploys["dirty_refused"]
    out = res.stdout + res.stderr
    assert res.returncode == 1, res.detail()
    assert _docker_calls(res) == [], (
        "the refusal came after docker had already been called" + res.detail())
    assert "REFUSING to deploy" in out, res.detail()
    assert "-AllowDirty" in out, "the refusal does not name the override" + res.detail()
    assert "README.md" in out and "notes-00.md" in out, (
        "the refusal does not show what is dirty" + res.detail())


def test_a_long_refusal_says_how_many_paths_it_left_out(deploys):
    res = deploys["many_dirty_refused"]
    assert res.returncode == 1, res.detail()
    assert "... and 5 more" in res.stdout + res.stderr, res.detail()


def test_allow_dirty_deploys_and_stamps_the_image_dirty(deploys):
    # PowerShell scripts absorb an undeclared -Foo into $args, so a script
    # without the switch would silently refuse; pin the declaration too.
    assert re.search(r"\[switch\]\$AllowDirty", UPDATE_PS1.read_text(encoding="utf-8"))
    res = deploys["dirty_allowed"]
    assert res.returncode == 0, res.detail()
    assert _stamp(res)["dirty"] == "true", res.detail()
    assert "-AllowDirty: deploying anyway" in res.stdout + res.stderr, res.detail()


def test_a_tree_git_cannot_describe_is_refused_with_gits_reason(deploys):
    res = deploys["not_a_repo_refused"]
    out = res.stdout + res.stderr
    assert res.returncode == 1, res.detail()
    assert _docker_calls(res) == [], res.detail()
    assert "cannot tell whether" in out, res.detail()
    assert "not a git repository" in out, "git's own reason is hidden" + res.detail()


def test_a_foreign_owned_checkout_names_the_git_fix(deploys):
    # sudo, or a checkout on a drive with foreign ownership: git refuses with
    # its safe.directory advice, which the operator needs, not a guess.
    res = deploys["foreign_owner_refused"]
    out = res.stdout + res.stderr
    assert res.returncode == 1, res.detail()
    assert "safe.directory" in out, res.detail()


def test_allow_dirty_on_a_non_repository_stamps_unknown(deploys):
    res = deploys["not_a_repo_allowed"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert (stamp["sha"], stamp["dirty"]) == ("unknown", "unknown"), res.detail()


def test_a_tree_that_changes_during_the_deploy_is_not_built(deploys):
    res = deploys["changed_during_deploy"]
    assert res.returncode != 0, res.detail()
    assert _compose_calls(res) == [], (
        "built a tree that no longer matched its stamp" + res.detail())
    assert "checkout changed during this deploy" in res.stdout + res.stderr, res.detail()


def test_the_callers_environment_is_left_as_it_was(deploys):
    for name in ("clean", "dirty_allowed", "changed_during_deploy"):
        after = dict(line.split("=", 1) for line in deploys[name].lines("after.txt"))
        assert after == {"sha": "caller-value", "dirty_present": "False"}, (
            deploys[name].detail())


def test_the_guard_runs_before_the_backup():
    # Execution tests run with -NoBackup (a real backup must never run from
    # a test), so the ordering against step 1 is pinned on the text.
    for path, backup in ((UPDATE_PS1, 'Step "Backing up the bank'),
                         (UPDATE_SH, 'step "Backing up the bank')):
        text = path.read_text(encoding="utf-8")
        assert text.index("REFUSING to deploy") < text.index(backup), path.name


# --- bash port --------------------------------------------------------------

_SH_DOCKER = '''
docker() {
    echo "docker $*" >> "$CALLS"
    if [ "$1" = compose ]; then
        printf 'sha=%s\\ndirty=%s\\ntime=%s\\n' "${PSEUDOLIFE_BUILD_GIT_SHA:-}" \\
            "${PSEUDOLIFE_BUILD_DIRTY:-}" "${PSEUDOLIFE_BUILD_TIME:-}" > "$SCENARIO_DIR/stamp.txt"
    fi
    if [ "$1" = image ] && [ "$2" = inspect ] && [ -n "${TOUCH_ON_INSPECT:-}" ]; then
        echo "late edit" > "$TOUCH_ON_INSPECT"
    fi
    return 0
}
curl() { echo '{"status":"ok","schema":42,"persist_errors":0}'; }
export -f docker curl
'''

_SH_SCENARIOS = {
    "sh_clean": (dict(repo=True), "", False),
    "sh_clean_with_git_trace": (dict(repo=True), "", "export GIT_TRACE=1"),
    "sh_foreign_owner_refused": (dict(repo=True), "", "export GIT_TEST_ASSUME_DIFFERENT_OWNER=1"),
    "sh_dirty_refused": (dict(repo=True, dirty=2), "", False),
    "sh_many_dirty_refused": (dict(repo=True, dirty=25), "", False),
    "sh_dirty_allowed": (dict(repo=True, dirty=2), "--allow-dirty", False),
    "sh_not_a_repo_refused": (dict(repo=False), "", False),
    "sh_not_a_repo_allowed": (dict(repo=False), "--allow-dirty", False),
    "sh_changed_during_deploy": (dict(repo=True), "", True),
}


@pytest.fixture(scope="module")
def sh_deploys(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("update_build_stamp_sh")
    scenarios, shas = [], {}
    for name, (sandbox, extra, touch) in _SH_SCENARIOS.items():
        sdir = scenario_dir(root, name)
        (sdir / "calls.log").write_text("", encoding="utf-8")
        repo, shas[name] = _sandbox(sdir, **sandbox)
        touch_line = (f'export TOUCH_ON_INSPECT="{(repo / "late-edit.txt").as_posix()}"\n'
                      if touch is True else "unset TOUCH_ON_INSPECT\n")
        if isinstance(touch, str):
            touch_line += touch + "\n"
        setup = (f'export CALLS="{(sdir / "calls.log").as_posix()}"\n'
                 "export HEALTH_RETRIES=2\nexport HEALTH_DELAY_MS=50\n"
                 f"export SCENARIO_DIR\n{touch_line}{_SH_DOCKER}")
        invoke = (f'bash "{(repo / "ops" / "update.sh").as_posix()}" --no-backup '
                  f"--no-cache-prune --tag unittest {extra}")
        scenarios.append(Scenario(name, setup, invoke))
    run = run_sh_batch(root, scenarios, env=_env(root))
    run.shas = shas
    return run


def test_update_sh_stamps_a_clean_tree(sh_deploys):
    res = sh_deploys["sh_clean"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert (stamp["sha"], stamp["dirty"]) == (sh_deploys.shas["sh_clean"], "false"), res.detail()
    assert _RFC3339.match(stamp["time"]), res.detail()


def test_update_sh_ignores_git_chatter_on_stderr(sh_deploys):
    res = sh_deploys["sh_clean_with_git_trace"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert (stamp["sha"], stamp["dirty"]) == (
        sh_deploys.shas["sh_clean_with_git_trace"], "false"), res.detail()


def test_update_sh_quotes_gits_safe_directory_advice(sh_deploys):
    res = sh_deploys["sh_foreign_owner_refused"]
    assert res.returncode == 1, res.detail()
    assert "safe.directory" in res.stderr, res.detail()


def test_update_sh_refuses_a_dirty_tree_and_lists_it(sh_deploys):
    res = sh_deploys["sh_dirty_refused"]
    assert res.returncode == 1, res.detail()
    assert _docker_calls(res) == [], res.detail()
    assert "--allow-dirty" in res.stderr, res.detail()
    assert "README.md" in res.stderr and "notes-00.md" in res.stderr, res.detail()


def test_update_sh_says_how_many_paths_it_left_out(sh_deploys):
    res = sh_deploys["sh_many_dirty_refused"]
    assert res.returncode == 1, res.detail()
    assert "... and 5 more" in res.stderr, res.detail()


def test_update_sh_allow_dirty_stamps_dirty(sh_deploys):
    res = sh_deploys["sh_dirty_allowed"]
    assert res.returncode == 0, res.detail()
    assert _stamp(res)["dirty"] == "true", res.detail()


def test_update_sh_refuses_a_non_repository_with_gits_reason(sh_deploys):
    res = sh_deploys["sh_not_a_repo_refused"]
    assert res.returncode == 1, res.detail()
    assert _docker_calls(res) == [], res.detail()
    assert "not a git repository" in res.stderr, res.detail()


def test_update_sh_allow_dirty_on_a_non_repository_stamps_unknown(sh_deploys):
    res = sh_deploys["sh_not_a_repo_allowed"]
    assert res.returncode == 0, res.detail()
    stamp = _stamp(res)
    assert (stamp["sha"], stamp["dirty"]) == ("unknown", "unknown"), res.detail()


def test_update_sh_does_not_build_a_tree_that_changed(sh_deploys):
    res = sh_deploys["sh_changed_during_deploy"]
    assert res.returncode != 0, res.detail()
    assert _compose_calls(res) == [], res.detail()
    assert "checkout changed during this deploy" in res.stderr, res.detail()
