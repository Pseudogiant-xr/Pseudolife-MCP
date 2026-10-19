"""CI under the GitHub merge queue: ci.yml's scope job and the required checks.

A queued merge group runs both workflows on the ``merge_group`` event. The
ruleset (``master: PRs with review + CI``) requires the checks ``test``,
``Parity / ubuntu-latest`` and ``Parity / windows-latest``; each must report
under the same name for a group as for a pull request, or the queue waits
until its timeout. ci.yml's ``scope`` job classifies a group like a pull
request, over the group's own range (``merge_group.base_sha`` to its head):
only a Rust-only change skips the Python suite lanes. rust/ci/test_workflow.py
covers rust.yml's own scope job and Parity gate.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.ops_harness import BASH

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
# The ruleset's required_status_checks contexts, read 2026-10-19 with
# `gh api repos/Pseudogiant-xr/Pseudolife-MCP/rulesets/19138403`.
REQUIRED_CHECKS = {"test", "Parity / ubuntu-latest", "Parity / windows-latest"}
GROUP_BASE = "e" * 40


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def events(workflow):
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def check_names(workflow):
    """Every check name a workflow's jobs report, matrix expanded."""
    names = set()
    for job_id, job in workflow["jobs"].items():
        template = job.get("name", job_id)
        matrix = job.get("strategy", {}).get("matrix", {})
        oses = matrix.get("os") if isinstance(matrix.get("os"), list) else None
        if oses and "${{ matrix.os }}" in template and template.count("${{") == 1:
            names.update(template.replace("${{ matrix.os }}", os_name) for os_name in oses)
        else:
            names.add(template)
    return names


@pytest.mark.parametrize("name", ["ci.yml", "rust.yml"])
def test_both_workflows_run_for_merge_groups_and_pull_requests(name):
    triggers = events(load(name))
    assert "merge_group" in triggers and "pull_request" in triggers
    # A types or branches filter could leave a group without its checks.
    assert triggers["merge_group"] is None


def test_every_required_check_reports_under_its_pull_request_name():
    # The same jobs run for both events, so one name set covers both.
    assert REQUIRED_CHECKS <= check_names(load("ci.yml")) | check_names(load("rust.yml"))
    ci = load("ci.yml")
    assert "name" not in ci["jobs"]["test"]
    # The suite job decides only on scope's output, never on the event name.
    assert "event_name" not in ci["jobs"]["test"]["if"]


def test_merge_group_runs_are_never_cancelled_by_concurrency():
    concurrency = load("ci.yml")["concurrency"]
    # Keyed by ref: a group's queue branch is its own ref, distinct from
    # refs/pull/N/merge and refs/heads/master.
    assert concurrency["group"] == "ci-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


# Stand in for `git fetch` and `git diff`, the step's only external inputs. An
# unexpected call is recorded in a file: the step runs git in a command
# substitution, where an exit would only end the subshell.
GIT_STUB = r'''
git() {
  local line="$*" base=HEAD^1
  if [ "$EVENT_NAME" = merge_group ]; then
    if [ "$line" = "fetch --no-tags --depth=1 origin $MERGE_GROUP_BASE_SHA" ]; then
      return "$PL_FETCH_EXIT"
    fi
    base=$MERGE_GROUP_BASE_SHA
  fi
  if [ "$line" != "diff --no-renames --name-only $base HEAD" ]; then
    printf '%s\n' "$line" >> "$PL_UNEXPECTED"; return 97
  fi
  if [ -n "$PL_FILES" ]; then printf '%s\n' "$PL_FILES"; fi
  return "$PL_DIFF_EXIT"
}
'''


def run_scope(tmp_path, event, files, *, base_sha=GROUP_BASE, fetch_exit=0, diff_exit=0):
    if BASH is None:
        pytest.skip("needs bash (Git Bash on Windows)")
    step = next(s for s in load("ci.yml")["jobs"]["scope"]["steps"] if s.get("id") == "scope")
    assert step["shell"] == "bash"
    assert step["env"] == {"EVENT_NAME": "${{ github.event_name }}",
                           "MERGE_GROUP_BASE_SHA": "${{ github.event.merge_group.base_sha }}"}
    output, unexpected = tmp_path / "github_output", tmp_path / "unexpected"
    output.write_text("", encoding="utf-8")
    script = tmp_path / "scope.sh"
    # Actions runs a bash step as `bash -e {0}` (pipefail too: shell: bash).
    script.write_text("set -eo pipefail\n" + GIT_STUB + step["run"], encoding="utf-8", newline="\n")
    env = dict(os.environ, EVENT_NAME=event, MERGE_GROUP_BASE_SHA=base_sha,
               GITHUB_OUTPUT=str(output), PL_UNEXPECTED=str(unexpected),
               PL_FILES="\n".join(files), PL_FETCH_EXIT=str(fetch_exit), PL_DIFF_EXIT=str(diff_exit))
    result = subprocess.run([BASH, str(script)], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not unexpected.exists(), unexpected.read_text(encoding="utf-8")
    return output.read_text(encoding="utf-8").split()


RUST_ONLY = ["rust/daemon/src/main.rs", "rust/ci/test_workflow.py",
             ".github/workflows/rust.yml", "CHANGELOG.md", "changelog.d/2026-10-19-x.md"]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_a_rust_only_change_skips_the_python_lanes(tmp_path, event):
    assert run_scope(tmp_path, event, RUST_ONLY) == ["python=false"]


@pytest.mark.parametrize("group", [
    # A Rust-only PR queued with a Python PR.
    RUST_ONLY + ["pseudolife_memory/service.py"],
    RUST_ONLY + ["tests/test_release_ux.py"],
    RUST_ONLY + [".github/workflows/ci.yml"],
    RUST_ONLY + ["README.md"],
    # Changelog-only: no Rust, so the suite still runs.
    ["CHANGELOG.md", "changelog.d/2026-10-19-x.md"],
])
def test_a_mixed_or_non_rust_merge_group_runs_the_python_lanes(tmp_path, group):
    assert run_scope(tmp_path, "merge_group", group) == ["python=true"]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_other_events_always_run_the_python_lanes(tmp_path, event):
    assert run_scope(tmp_path, event, RUST_ONLY) == ["python=true"]


@pytest.mark.parametrize("base_sha, fetch_exit", [
    ("", 0), ("master", 0), ("E" * 40, 0), ("e" * 39, 0), ("e" * 40 + " x", 0), (GROUP_BASE, 128)])
def test_a_merge_group_without_a_fetched_base_runs_everything(tmp_path, base_sha, fetch_exit):
    assert run_scope(tmp_path, "merge_group", RUST_ONLY, base_sha=base_sha,
                     fetch_exit=fetch_exit) == ["python=true"]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("files, diff_exit", [([], 0), (RUST_ONLY, 128)])
def test_an_empty_or_unreadable_diff_runs_everything(tmp_path, event, files, diff_exit):
    assert run_scope(tmp_path, event, files, diff_exit=diff_exit) == ["python=true"]
