"""CI under the GitHub merge queue: ci.yml's scope job and the required checks.

A queued merge group runs both workflows on the ``merge_group`` event. The
ruleset (``master: PRs with review + CI``) requires the checks ``test``,
``Parity / ubuntu-latest`` and ``Parity / windows-latest``; each must report
under the same name for a group as for a pull request, or the queue waits
until its timeout. ci.yml's ``scope`` job classifies a group like a pull
request, over everything the group adds to master (master's tip, fetched, to
the group head; never ``merge_group.base_sha``, which may name the queue entry
ahead): only a Rust-only change skips the Python suite lanes. A pull request
from any branch but ``integrate/*`` skips them too (fast lanes only; its
integration batch runs them), which leaves the skipped ``test`` job reporting
success to its required check.
rust/ci/test_workflow.py covers rust.yml's own scope job and Parity gate.
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
# The checks docs/specs/2026-10-19-merge-queue.md asks the maintainer to add
# before enabling the queue, which waits only for required checks.
ADDED_CHECKS = {"guards", "test-lite-linux", "test-lite-windows", "test-lite-macos",
                "frontend", "lock-resolve", "Rust daemon / disposable image"}
SPEC = ROOT / "docs" / "specs" / "2026-10-19-merge-queue.md"


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


def test_every_check_the_spec_asks_to_require_exists():
    names = check_names(load("ci.yml")) | check_names(load("rust.yml"))
    assert ADDED_CHECKS <= names
    spec = SPEC.read_text(encoding="utf-8")
    for name in REQUIRED_CHECKS | ADDED_CHECKS:
        assert f"`{name}`" in spec, name


def ci_concurrency_group(event, *, ref="refs/heads/master", head_ref="", run_id="101"):
    """ci.yml's group expression, evaluated the way Actions does for the
    shapes it uses: an ordinary pull request's group, or one per run."""
    expression = load("ci.yml")["concurrency"]["group"]
    assert expression == ("${{ github.event_name == 'pull_request' && github.head_ref != '' "
                          "&& !startsWith(github.head_ref, 'integrate/') "
                          "&& format('ci-pr-{0}', github.ref) "
                          "|| format('ci-{0}-{1}', github.event_name, github.run_id) }}")
    # Actions' startsWith ignores case.
    if event == "pull_request" and head_ref and not head_ref.lower().startswith("integrate/"):
        return f"ci-pr-{ref}"
    return f"ci-{event}-{run_id}"


def test_only_an_ordinary_pull_request_supersedes_its_own_runs():
    assert load("ci.yml")["concurrency"]["cancel-in-progress"] is True
    pr1 = ci_concurrency_group("pull_request", ref="refs/pull/1/merge", head_ref="fix/x")
    assert pr1 == ci_concurrency_group("pull_request", ref="refs/pull/1/merge",
                                       head_ref="fix/x", run_id="102")
    assert pr1 != ci_concurrency_group("pull_request", ref="refs/pull/2/merge", head_ref="fix/x")
    # Master pushes, the nightly, manual runs, merge groups (the queue's
    # verdict) and integrate/* batch runs each get a group of their own, so
    # nothing cancels them, pending or running.
    queued = "refs/heads/gh-readonly-queue/master/pr-830-" + "a" * 40
    for event, ref, head_ref in (("push", "refs/heads/master", ""),
                                 ("schedule", "refs/heads/master", ""),
                                 ("workflow_dispatch", "refs/heads/master", ""),
                                 ("merge_group", queued, ""),
                                 ("pull_request", "refs/pull/3/merge",
                                  "integrate/2026-10-29-batch-1"),
                                 ("pull_request", "refs/pull/3/merge", "")):
        first = ci_concurrency_group(event, ref=ref, head_ref=head_ref, run_id="1")
        assert first != ci_concurrency_group(event, ref=ref, head_ref=head_ref, run_id="2")
        assert first != pr1


# Stand in for `git fetch` and `git diff`, the step's only external inputs. An
# unexpected call is recorded in a file: the step runs git in a command
# substitution, where an exit would only end the subshell.
GIT_STUB = r'''
git() {
  local line="$*" base=HEAD^1
  if [ "$EVENT_NAME" = merge_group ]; then
    if [ "$line" = "fetch --no-tags --depth=1 origin refs/heads/master" ]; then
      return "$PL_FETCH_EXIT"
    fi
    base=FETCH_HEAD
  fi
  if [ "$line" != "diff --no-renames --name-only $base HEAD" ]; then
    printf '%s\n' "$line" >> "$PL_UNEXPECTED"; return 97
  fi
  if [ -n "$PL_FILES" ]; then printf '%s\n' "$PL_FILES"; fi
  return "$PL_DIFF_EXIT"
}
'''


BATCH_REF = "integrate/2026-10-21-batch-1"


def run_scope(tmp_path, event, files, *, base_ref="refs/heads/master", fetch_exit=0, diff_exit=0,
              head_ref=None):
    """Run the scope step as Actions would. `head_ref` is github.head_ref,
    which Actions sets only for pull requests: by default a batch branch's,
    whose pull request is classified by its diff."""
    if BASH is None:
        pytest.skip("needs bash (Git Bash on Windows)")
    if head_ref is None:
        head_ref = BATCH_REF if event == "pull_request" else ""
    step = next(s for s in load("ci.yml")["jobs"]["scope"]["steps"] if s.get("id") == "scope")
    assert step["shell"] == "bash"
    assert step["env"] == {"EVENT_NAME": "${{ github.event_name }}",
                           "MERGE_GROUP_BASE_REF": "${{ github.event.merge_group.base_ref }}",
                           "HEAD_REF": "${{ github.head_ref }}"}
    output, unexpected = tmp_path / "github_output", tmp_path / "unexpected"
    output.write_text("", encoding="utf-8")
    script = tmp_path / "scope.sh"
    # Actions runs a bash step as `bash -e {0}` (pipefail too: shell: bash).
    script.write_text("set -eo pipefail\n" + GIT_STUB + step["run"], encoding="utf-8", newline="\n")
    env = dict(os.environ, EVENT_NAME=event, MERGE_GROUP_BASE_REF=base_ref, HEAD_REF=head_ref,
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


@pytest.mark.parametrize("base_ref, fetch_exit", [
    ("", 0), ("master", 0), ("refs/heads/topic", 0), ("refs/heads/master2", 0),
    ("refs/heads/master x", 0), ("refs/heads/master", 128)])
def test_a_merge_group_without_a_fetched_master_tip_runs_everything(tmp_path, base_ref, fetch_exit):
    assert run_scope(tmp_path, "merge_group", RUST_ONLY, base_ref=base_ref,
                     fetch_exit=fetch_exit) == ["python=true"]


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("files, diff_exit", [([], 0), (RUST_ONLY, 128)])
def test_an_empty_or_unreadable_diff_runs_everything(tmp_path, event, files, diff_exit):
    assert run_scope(tmp_path, event, files, diff_exit=diff_exit) == ["python=true"]


# Ordinary pull requests run the fast lanes only; their integration batch's
# pull request (an integrate/* head) runs every lane (docs/dev/merge-batches.md).
ORDINARY_REFS = ["claude/some-change", "fix/x", "integrate", "integrated/x",
                 "Integrate/2026-10-21-batch-1", "x/integrate/y", "master"]
PYTHON_CHANGE = ["pseudolife_memory/service.py", "tests/test_x.py"]


# CI and dependency inputs: before 2026-10-29 a pull request changing one ran
# every lane on its own head; now its batch does.
CI_INPUTS = [".github/workflows/ci.yml", ".github/workflows/rust.yml",
             ".github/workflows/release.yml", "pyproject.toml", "ops/requirements.lock.txt",
             "rust/Cargo.lock", "evals/rust_port/users_comparison/Cargo.lock",
             "frontend/package-lock.json", "rust/rust-toolchain.toml"]
# Near misses that are not CI or dependency inputs.
NOT_CI_INPUTS = ["docs/pyproject.toml.md", "ops/requirements.lock.txt.bak",
                 "docs/Cargo.lock.md", "github/workflows/x.yml", "rust/Cargo.toml",
                 "frontend/package.json", "tests/test_package-lock.json.py"]


@pytest.mark.parametrize("head_ref", ORDINARY_REFS)
@pytest.mark.parametrize("files", [PYTHON_CHANGE, ["rust/daemon/src/main.rs"], ["README.md"],
                                   NOT_CI_INPUTS])
def test_an_ordinary_pull_request_runs_the_fast_lanes_only(tmp_path, head_ref, files):
    assert run_scope(tmp_path, "pull_request", files, head_ref=head_ref) == ["python=false"]


@pytest.mark.parametrize("ci_input", CI_INPUTS)
def test_an_ordinary_pull_request_changing_ci_inputs_runs_the_fast_lanes_only(tmp_path, ci_input):
    # Only batches spend the heavy lanes (maintainer, 2026-10-29): the edit
    # runs every lane, under the edited workflow, on its batch's head.
    assert run_scope(tmp_path, "pull_request", PYTHON_CHANGE + [ci_input],
                     head_ref="claude/some-change") == ["python=false"]


@pytest.mark.parametrize("files, diff_exit", [([], 0), (PYTHON_CHANGE, 128)])
def test_an_ordinary_pull_request_with_an_unreadable_diff_runs_everything(tmp_path, files,
                                                                          diff_exit):
    assert run_scope(tmp_path, "pull_request", files, diff_exit=diff_exit,
                     head_ref="claude/some-change") == ["python=true"]


@pytest.mark.parametrize("head_ref", [BATCH_REF, "integrate/2026-10-07-rust-modes"])
def test_a_batch_pull_request_runs_every_lane_its_diff_needs(tmp_path, head_ref):
    assert run_scope(tmp_path, "pull_request", PYTHON_CHANGE, head_ref=head_ref) == ["python=true"]
    assert run_scope(tmp_path, "pull_request", RUST_ONLY, head_ref=head_ref) == ["python=false"]


def test_a_pull_request_without_a_head_ref_is_classified_by_its_diff(tmp_path):
    # Fail-safe: a missing github.head_ref never selects the fast lanes.
    assert run_scope(tmp_path, "pull_request", PYTHON_CHANGE, head_ref="") == ["python=true"]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch", "merge_group"])
def test_only_pull_requests_read_the_head_ref(tmp_path, event):
    assert run_scope(tmp_path, event, PYTHON_CHANGE, head_ref="claude/some-change") == ["python=true"]


BATCH_PASSED = "fromJSON(needs.scope.outputs.batch_passed || '[]')"


def test_the_fast_lanes_skip_every_suite_lane_and_keep_guards():
    jobs = load("ci.yml")["jobs"]
    for name in ("test", "test-lite-linux", "test-lite-windows", "test-lite-macos"):
        assert jobs[name]["needs"] == "scope", name
        assert jobs[name]["if"] == ("${{ !cancelled() && needs.scope.outputs.python != 'false' "
                                    f"&& !contains({BATCH_PASSED}, '{name}') }}}}"), name
    assert jobs["guards"]["if"] == "needs.scope.outputs.python == 'false'"
    # Cheap lanes are never scoped by the diff: only a master push whose
    # merged batch passed them on the same tree skips them, and a failed
    # scope still runs them (ops/ci_batch_passed.py).
    for name in ("lock-resolve", "frontend"):
        assert jobs[name]["needs"] == "scope", name
        assert jobs[name]["if"] == f"${{{{ !cancelled() && !contains({BATCH_PASSED}, '{name}') }}}}", name
