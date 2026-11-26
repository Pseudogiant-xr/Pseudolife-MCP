"""A release cut's own pull request runs `test` frozen-inclusive, and nothing
else changes for it or for any other pull request (ops/ci_release_cut.py)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
CUT_CLAUSE = "(needs.scope.outputs.python != 'false' || needs.scope.outputs.cut == 'true')"
# What the Classify step runs, copied into each fixture repository at its base.
CARRIED = ("ops/ci_release_cut.py", "evals/rust_port/__init__.py",
           "evals/rust_port/oracle_selection.py", "evals/rust_port/oracle_pins.py")


def classify_step():
    return next(step for step in CI["jobs"]["scope"]["steps"] if step.get("id") == "scope")


def test_scope_publishes_the_cut_mark():
    assert CI["jobs"]["scope"]["outputs"]["cut"] == "${{ steps.scope.outputs.cut }}"
    assert ("python3 -m ops.ci_release_cut --base HEAD^1 --head HEAD "
            '--github-output "$GITHUB_OUTPUT"') in classify_step()["run"]


def test_only_test_reads_the_cut_mark():
    readers = sorted(job_id for job_id, job in CI["jobs"].items()
                     if "needs.scope.outputs.cut" in json.dumps(job))
    assert readers == ["test"]
    assert CUT_CLAUSE in CI["jobs"]["test"]["if"]
    # The lanes a cut must not start keep the plain python decision, and
    # guards still runs beside the cut's `test`.
    for lane in ("test-lite-windows", "test-lite-macos"):
        assert "needs.scope.outputs.python != 'false'" in CI["jobs"][lane]["if"]
    assert CI["jobs"]["guards"]["if"] == "needs.scope.outputs.python == 'false'"
    assert CI["jobs"]["test"]["env"]["PSEUDOLIFE_CI_INCLUDE_FROZEN"] == "${{ needs.scope.outputs.include_frozen }}"


# --- the Classify step itself, on a pull request's merge commit ---------------

def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def commit(root, message="fixture"):
    git(root, "add", "-A")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "--quiet", "-m", message)
    return git(root, "rev-parse", "HEAD")


def version(root, number):
    """The release version in every file a cut moves (oracle_selection's
    VERSION_FILES that this fixture carries)."""
    write(root, "pyproject.toml", f'[project]\nname = "fixture"\nversion = "{number}"\n')
    write(root, "rust/shim/Cargo.toml", f'[package]\nname = "pseudolife-stdio"\nversion = "{number}"\n')
    write(root, "rust/Cargo.lock", f'[[package]]\nname = "pseudolife-stdio"\nversion = "{number}"\n')
    write(root, "plugin/release.json", json.dumps({"version": number}) + "\n")
    write(root, "pseudolife_memory/compose/docker-compose.yml", f"image: pseudolife-daemon:{number}\n")


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "--quiet", "-b", "master")
    git(root, "config", "core.autocrlf", "false")
    for path in CARRIED:
        write(root, path, (ROOT / path).read_text(encoding="utf-8"))
    version(root, "0.18.0")
    write(root, "pseudolife_memory/shim.py", "CONTRACT = 'unchanged'\n")
    write(root, "CHANGELOG.md", "# Changelog\n")
    write(root, "changelog.d/2026-11-20-a-fix.md", "- A fix.\n")
    base = commit(root)
    return root, base


def pull_request(root, base, change, *, master_moves=True):
    """The merge commit Actions checks out for a pull request whose branch
    starts at ``base``: first parent master's tip, second the branch."""
    git(root, "checkout", "--quiet", "-b", "branch", base)
    change(root)
    branch = commit(root, "branch")
    git(root, "checkout", "--quiet", "master")
    if master_moves:
        # Master moved on after the branch started: the merge's own diff is
        # still only the branch's change.
        write(root, "docs/unrelated.md", "Merged meanwhile.\n")
        write(root, "rust/shim/src/meanwhile.rs", "// merged meanwhile\n")
        commit(root, "master")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
        "merge", "--quiet", "--no-ff", "-m", "merge", branch)


def cut(root):
    version(root, "0.18.1")
    write(root, "CHANGELOG.md", "# Changelog\n\n## 0.18.1\n\n- A fix.\n")
    (root / "changelog.d/2026-11-20-a-fix.md").unlink()


def ordinary(root):
    write(root, "rust/shim/src/fix.rs", "// a fix\n")


def classify(root, head_ref):
    output = root.parent / "github-output"
    output.write_text("", encoding="utf-8")
    env = dict(os.environ, EVENT_NAME="pull_request", HEAD_REF=head_ref, GITHUB_OUTPUT=str(output),
               MERGE_GROUP_BASE_REF="", BEFORE="", WEEKLY_FROZEN="")
    run = subprocess.run(["bash", "-e", "-c", classify_step()["run"]], cwd=root, env=env,
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    lines = output.read_text(encoding="utf-8").split()
    return dict(line.split("=", 1) for line in lines), run.stdout


posix = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None or shutil.which("python3") is None,
                           reason="the Classify step runs in bash with python3 on ubuntu-latest")


@posix
def test_a_cut_pull_request_runs_test_with_the_frozen_files(repository):
    root, base = repository
    pull_request(root, base, cut)
    outputs, log = classify(root, "t3/release-0-18-1")
    assert outputs == {"python": "false", "cut": "true", "include_frozen": "true"}
    assert "a release cut (0.18.0 -> 0.18.1)" in log


@posix
def test_a_cut_branch_level_with_master_is_a_cut_too(repository):
    root, base = repository
    pull_request(root, base, cut, master_moves=False)
    assert classify(root, "t3/release-0-18-1")[0] == {"python": "false", "cut": "true", "include_frozen": "true"}


@posix
def test_an_ordinary_pull_request_keeps_its_fast_lanes(repository):
    root, base = repository
    pull_request(root, base, ordinary)
    outputs, log = classify(root, "t3/a-fix")
    assert outputs == {"python": "false"}
    assert "not a release cut" in log


@posix
def test_a_cut_with_a_code_change_is_not_a_cut(repository):
    root, base = repository

    def cut_and_code(root):
        cut(root)
        ordinary(root)
    pull_request(root, base, cut_and_code)
    assert classify(root, "t3/release-0-18-1")[0] == {"python": "false"}


@posix
def test_a_cut_batch_is_classified_by_its_diff_as_before(repository):
    # The batch branch runs every lane, frozen files included (the cut moves
    # the packaged compose file under pseudolife_memory/), without the mark.
    root, base = repository
    pull_request(root, base, cut)
    assert classify(root, "integrate/2026-11-26-batch-6")[0] == {"include_frozen": "true", "python": "true"}


@posix
def test_a_crashed_check_leaves_the_fast_lanes(repository):
    root, base = repository
    pull_request(root, base, cut)
    (root / "evals/rust_port/oracle_selection.py").unlink()
    outputs, log = classify(root, "t3/release-0-18-1")
    assert outputs == {"python": "false"}
    assert "the release-cut check failed: fast lanes only" in log
