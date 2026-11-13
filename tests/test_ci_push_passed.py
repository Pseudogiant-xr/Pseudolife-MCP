"""Nightlies may reuse a successful push only for the exact workflow and SHA."""
from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from ops import ci_push_passed as push

ROOT = Path(__file__).resolve().parents[1]
REPO = "Pseudogiant-xr/Pseudolife-MCP"
SHA = "a" * 40
WORKFLOW = ".github/workflows/rust.yml"
RUN = {"id": 123, "path": WORKFLOW, "event": "push", "head_sha": SHA,
       "head_branch": "master", "head_repository": {"full_name": REPO},
       "status": "completed", "conclusion": "success"}


def decide(runs, **overrides):
    calls = []

    def get(path):
        calls.append(path)
        return {"workflow_runs": runs}

    args = dict(repo=REPO, event="schedule", ref="refs/heads/master", sha=SHA, workflow=WORKFLOW)
    args.update(overrides)
    result = push.decide(get, **args)
    return result, calls


def test_exact_successful_master_push_can_be_reused():
    (passed, reason), calls = decide([RUN])
    assert passed is True and "123" in reason
    assert len(calls) == 1
    parsed = urlsplit(calls[0])
    assert parsed.path == f"/repos/{REPO}/actions/workflows/rust.yml/runs"
    assert parse_qs(parsed.query) == {"event": ["push"], "head_sha": [SHA],
                                    "branch": ["master"], "status": ["success"], "per_page": ["100"]}


@pytest.mark.parametrize("field,value", [
    ("path", ".github/workflows/ci.yml"), ("event", "schedule"),
    ("event", "pull_request"), ("head_sha", "b" * 40), ("head_branch", "other"),
    ("head_repository", {"full_name": "example/fork"}), ("head_repository", None),
    ("status", "in_progress"), ("conclusion", "failure"), ("conclusion", "cancelled"),
    ("conclusion", "skipped"), ("conclusion", None),
])
def test_near_misses_cannot_turn_off_nightly_coverage(field, value):
    run = copy.deepcopy(RUN)
    run[field] = value
    assert decide([run])[0][0] is False


@pytest.mark.parametrize("event,ref", [("push", "refs/heads/master"),
    ("pull_request", "refs/pull/1/merge"), ("merge_group", "refs/heads/master"),
    ("workflow_dispatch", "refs/heads/master"), ("schedule", "refs/heads/other")])
def test_only_a_master_schedule_may_reuse_a_push(event, ref):
    result, calls = decide([RUN], event=event, ref=ref)
    assert result[0] is False and not calls


@pytest.mark.parametrize("response", [None, [], {}, {"workflow_runs": None}, {"workflow_runs": []}])
def test_unreadable_or_empty_listing_runs_everything(response):
    assert push.decide(lambda _: response, repo=REPO, event="schedule", ref="refs/heads/master",
                       sha=SHA, workflow=WORKFLOW)[0] is False


@pytest.mark.parametrize("error", [OSError("offline"), push.ApiError("HTTP 403"), ValueError("bad JSON")])
def test_api_failure_runs_everything(error):
    def get(_):
        raise error
    assert push.decide(get, repo=REPO, event="schedule", ref="refs/heads/master",
                       sha=SHA, workflow=WORKFLOW)[0] is False


def test_missing_token_writes_an_explicit_false(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    output = tmp_path / "output"
    assert push.main(["--workflow", WORKFLOW, "--github-output", str(output)]) == 0
    assert output.read_text() == "passed=false\n"


@pytest.mark.parametrize("workflow,skips,keeps", [
    ("rust.yml", ["rust", "parity-checks", "daemon-image"],
     ["candidate", "candidate-harness", "fixtures", "source-mutants", "parity-selfhosted"]),
    ("ci.yml", ["test", "test-lite-linux", "test-lite-windows", "test-lite-macos", "frontend", "lock-resolve"],
     ["test-windows-nightly"]),
])
def test_workflows_preserve_nightly_only_evidence_and_its_builds(workflow, skips, keeps):
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    scope = jobs["scope"]
    step = next(s for s in scope["steps"] if s.get("id") == "push")
    assert scope["permissions"]["actions"] == "read"
    assert step["if"] == "github.event_name == 'schedule' && github.ref == 'refs/heads/master'"
    assert step["continue-on-error"] is True
    assert step["run"].split() == ["python3", "-m", "ops.ci_push_passed", "--workflow",
                                  f".github/workflows/{workflow}", "--github-output", '"$GITHUB_OUTPUT"']
    assert scope["outputs"]["push_passed"] == "${{ steps.push.outputs.passed }}"
    for name in skips:
        assert "needs.scope.outputs.push_passed != 'true'" in jobs[name]["if"]
    for name in keeps:
        assert "push_passed" not in jobs[name].get("if", "")
