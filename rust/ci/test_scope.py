"""Exercise change detection against real Git histories and gate outcomes."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

import scope

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("paths,expected", [
    ([".github/workflows/rust.yml", "rust/ci/scope.py", "changelog.d/example.md"], "contracts"),
    (["rust/ci/test_scope.py"], "contracts"),
    (["rust/ci/scope.py", "rust/shim/src/main.rs"], "full"),
    (["rust/ci/scope.py", "pyproject.toml"], "full"),
    (["rust/ci/scope.py", "pseudolife_memory/service.py"], "full"),
    (["rust/daemon/harness/dbstate.py"], "full"),
    (["rust/ci/run_coverage.py"], "full"),
    (["rust/ci/coverage_plan.py"], "full"),
    (["rust/ci/source_mutants.json"], "full"),
    (["pseudolife_memory/re_evidence.py"], "none"),
    (["README.md"], "none"),
    ([], "full"),
])
def test_workflow_only_changes_use_contracts_and_mixed_changes_run_full(paths, expected):
    assert scope.classify(paths) == expected


@pytest.mark.parametrize("path", [
    "rust/shim/src/main.rs", "rust/daemon/src/main.rs", "rust/ci/scope.py",
    "evals/rust_port/oracle_selection.py", "evals/rust_baseline/runner.py",
    "pseudolife_memory/storage/schema.py", "pyproject.toml", "ops/requirements.lock.txt",
    "ops/Dockerfile.daemon-rust", "plugin/hooks/session-start.sh", ".dockerignore",
    ".github/workflows/rust.yml", "pseudolife_memory/web/static/index.html",
    "tests/conftest.py", "evals/claude_shim.py",
    "tests/pg_defaults.py", "tests/test_transfer_cli.py", "tests/maintainer_authenticator.py",
    "tests/test_coordination_audit.py", "tests/asgi_helpers.py",
    "tests/pg_fixtures.py", "tests/test_cli_dispatch.py", "evals/memory_policy_bench.py",
    "evals/memory_policy_daemon.py",
])
def test_shared_and_native_inputs_run_rust(path):
    assert scope.relevant(path)


@pytest.mark.parametrize("path", [
    "pseudolife_memory/service.py", "pseudolife_memory/re_evidence.py",
    "tests/test_re_evidence.py", "README.md", "docs/guide/configuration.md",
    "changelog.d/2026-10-21-example.md", ".github/workflows/ci.yml",
])
def test_unrelated_python_and_docs_skip_rust(path):
    assert not scope.relevant(path)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def history(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "CI test")
    git(tmp_path, "config", "user.email", "ci@example.com")
    (tmp_path / "README.md").write_text("base")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def commit_file(root, path):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed")
    git(root, "add", ".")
    git(root, "commit", "-m", "change")
    return git(root, "rev-parse", "HEAD")


def test_push_checks_every_commit_and_both_sides_of_renames(history):
    root, base = history
    commit_file(root, "rust/old.rs")
    head = commit_file(root, "python-fix.py")
    assert scope.select_lane(root, "push", base, head) == "full"
    base = head
    git(root, "mv", "rust/old.rs", "moved.py")
    git(root, "commit", "-m", "move out of rust")
    head = git(root, "rev-parse", "HEAD")
    assert scope.select_lane(root, "push", base, head) == "full"


def test_pr_uses_tested_merge_diff(history):
    root, base = history
    head = commit_file(root, "pseudolife_memory/service.py")
    assert scope.select_lane(root, "pull_request", base, head) == "none"
    commit_file(root, "rust/changed.rs")
    assert scope.select_lane(root, "pull_request", base, "HEAD") == "full"


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_nightly_and_manual_always_run(history, event):
    root, base = history
    assert scope.select_lane(root, event, base, base) == "full"


@pytest.mark.parametrize("base", ["", "0" * 40, "missing-commit"])
def test_unknown_history_runs_checks(history, base):
    root, head = history
    assert scope.select_lane(root, "push", base, head) == "full"


def test_empty_diff_runs_checks(history):
    root, base = history
    assert scope.select_lane(root, "pull_request", base, base) == "full"


def test_workflow_scopes_expensive_jobs_but_keeps_entry_points_and_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    assert set(events) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    assert events["pull_request"] is None
    assert events["push"] == {"branches": ["master"]}
    jobs = workflow["jobs"]
    contracts = jobs["contracts"]
    assert contracts["needs"] == "scope"
    assert contracts["if"] == "needs.scope.outputs.lane == 'contracts'"
    assert contracts["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest"]
    assert contracts["strategy"]["fail-fast"] is False
    assert not contracts.get("continue-on-error")
    scripts = "\n".join(step.get("run", "") for step in contracts["steps"])
    for name in ("test_scope.py", "test_workflow.py", "test_h5_inventory.py", "test_console_mime.py", "test_snapshot_settle.py", "test_timings.py"):
        assert "rust/ci/" + name in scripts
    assert "python rust/ci/inventory.py" in scripts
    for name in ("rust", "candidate", "parity-checks", "source-mutants", "daemon-image"):
        assert "scope" in ([jobs[name]["needs"]] if isinstance(jobs[name]["needs"], str) else jobs[name]["needs"])
        assert jobs[name]["if"].startswith("needs.scope.outputs.needed == 'true'")
    assert jobs["parity"]["if"].startswith("always() &&")
    assert "github.event_name" in workflow["concurrency"]["group"]
    assert "github.event_name == 'pull_request' && github.ref || github.run_id" in workflow["concurrency"]["group"]
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


@pytest.mark.parametrize("scope_result,needed,job_result,event,accepted", [
    ("success", "false", "skipped", "pull_request", True),
    ("success", "false", "skipped", "push", True),
    ("success", "false", "skipped", "schedule", False),
    ("success", "false", "skipped", "workflow_dispatch", False),
    ("failure", "false", "skipped", "pull_request", False),
    ("cancelled", "false", "skipped", "pull_request", False),
    ("success", "", "skipped", "pull_request", False),
    ("success", "false", "failure", "pull_request", False),
    ("success", "true", "skipped", "pull_request", False),
    ("success", "true", "success", "push", True),
])
def test_final_gate_only_accepts_proven_unrelated_skips(scope_result, needed, job_result, event, accepted):
    gate = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text())["jobs"]["parity"]
    needs = {name: {"result": job_result} for name in gate["needs"]}
    needs["scope"] = {"result": scope_result, "outputs": {"needed": needed, "lane": "full" if needed == "true" else "none"}}
    needs["contracts"]["result"] = "skipped"
    result = subprocess.run(["pwsh", "-NoProfile", "-Command", gate["steps"][0]["run"]],
                            env=dict(os.environ, GATE_RESULTS=json.dumps(needs), GATE_EVENT=event),
                            capture_output=True, text=True)
    assert result.returncode == (0 if accepted else 1), result.stderr


@pytest.mark.parametrize("event", ["pull_request", "push", "schedule", "workflow_dispatch"])
@pytest.mark.parametrize("contracts_result", ["success", "failure", "cancelled", "skipped", ""])
def test_contract_lane_requires_both_os_contracts_and_cannot_skip_nightly(event, contracts_result):
    gate = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text())["jobs"]["parity"]
    needs = {name: {"result": "skipped"} for name in gate["needs"]}
    needs["scope"] = {"result": "success", "outputs": {"needed": "false", "lane": "contracts"}}
    needs["contracts"]["result"] = contracts_result
    result = subprocess.run(["pwsh", "-NoProfile", "-Command", gate["steps"][0]["run"]],
                            env=dict(os.environ, GATE_RESULTS=json.dumps(needs), GATE_EVENT=event),
                            capture_output=True, text=True)
    assert result.returncode == (0 if event in ("pull_request", "push") and contracts_result == "success" else 1), result.stderr
