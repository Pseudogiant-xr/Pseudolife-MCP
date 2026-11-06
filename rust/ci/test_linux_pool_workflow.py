"""Exercise workflow event selection, including incorrect planner results."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

import ci_budget


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / ".github/workflows/rust.yml"
REPO = "Pseudogiant-xr/Pseudolife-MCP"


class Actions(ci_budget.Evaluator):
    def __init__(self, context):
        super().__init__(context["github"]["event_name"], [], set())
        self.context = context

    def lookup(self, path):
        value = self.context
        for name in path:
            value = value.get(name, "") if isinstance(value, dict) else ""
        return value

    def disjunction(self):
        value = self.conjunction()
        while self.peek("||"):
            self.take()
            other = self.conjunction()
            value = value if value else other
        return value

    def conjunction(self):
        value = self.comparison()
        while self.peek("&&"):
            self.take()
            other = self.comparison()
            value = other if value else value
        return value

    def call(self, name):
        if name != "fromJSON":
            return super().call(name)
        assert self.take()[1] == "("
        value = self.disjunction()
        assert self.take()[1] == ")"
        return json.loads(value)


BASE_SHA = "b" * 40
HEAD_SHA = "c" * 40


def context(event="push", ref="refs/heads/master", head="", head_repo=REPO, repository=REPO):
    # The scope job's `integrate` output, as its exact-case step emits it
    # (test_scope_marks_only_exact_integrate_refs runs that step).
    integrate = "true" if event == "pull_request" and head.startswith("integrate/") else "false"
    return {"github": {"event_name": event, "ref": ref, "head_ref": head, "sha": HEAD_SHA,
                       "repository": repository, "event": {"pull_request": {
                           "base": {"sha": BASE_SHA},
                           "head": {"repo": {"full_name": head_repo}}}}},
            "steps": {"checkout": {"outcome": "success"}},
            "matrix": {"os": "ubuntu-latest", "suite": "cli-startup"},
            "needs": {**{name: {"result": "success"} for name in
                         ("candidate", "candidate-harness", "fixtures")},
                      "scope": {"result": "success",
                                "outputs": {"full": "true", "integrate": integrate}},
                      "parity-pool": {"outputs": {"linux_pool": "self-hosted"}}}}


def evaluate(expression, values):
    return Actions(values).run(ci_budget.condition_text(expression))


CASES = [
    ({}, True),
    ({"ref": "refs/heads/integrate/example"}, False),
    ({"event": "pull_request", "ref": "refs/pull/123/merge", "head": "integrate/example"}, True),
    ({"event": "pull_request", "ref": "refs/pull/123/merge", "head": "integrate/example", "head_repo": "fork/repo"}, False),
    ({"event": "pull_request", "head": "feature/example"}, False),
    ({"ref": "refs/heads/feature/example"}, False),
    ({"repository": "fork/repo"}, False),
    ({"event": "pull_request_target"}, False),
    ({"event": "merge_group"}, False),
    ({"event": "schedule"}, False),
    ({"event": "workflow_dispatch"}, False),
]


@pytest.mark.parametrize("changes,allowed", CASES)
def test_secret_step_and_selfhosted_runner_share_the_trusted_event_boundary(changes, allowed):
    jobs = yaml.safe_load(PATH.read_text())["jobs"]
    planner = jobs["parity-pool"]
    values = context(**changes)
    assert evaluate(planner["if"], values) is allowed
    step = next(step for step in planner["steps"] if step.get("id") == "pool")
    assert evaluate(step["if"], values) is allowed
    assert evaluate(jobs["parity-checks"]["runs-on"], values) == (
        ["self-hosted", "pseudolife-parity-linux"] if allowed else "ubuntu-latest")


@pytest.mark.parametrize("system,suite,choice", [
    ("windows-latest", "cli-startup", "self-hosted"),
    ("ubuntu-latest", "eval", "self-hosted"),
    ("ubuntu-latest", "cli-startup", "hosted"),
    ("ubuntu-latest", "cli-startup", ""),
])
def test_only_one_linux_shard_moves_and_empty_failed_planners_keep_hosted(system, suite, choice):
    jobs = yaml.safe_load(PATH.read_text())["jobs"]
    values = context()
    values["matrix"] = {"os": system, "suite": suite}
    values["needs"]["parity-pool"]["outputs"]["linux_pool"] = choice
    assert evaluate(jobs["parity-checks"]["runs-on"], values) == system


def test_secret_is_confined_to_the_hosted_planner_step_and_gates_keep_their_meaning():
    workflow = yaml.safe_load(PATH.read_text())
    jobs = workflow["jobs"]
    assert PATH.read_text().count("${{ secrets.PARITY_VARS_READ_TOKEN }}") == 1
    planner = jobs["parity-pool"]
    assert planner["runs-on"] == "ubuntu-latest"
    assert planner["needs"] == ["scope", "candidate", "candidate-harness", "fixtures"]
    step = next(step for step in planner["steps"] if step.get("id") == "pool")
    assert step["env"] == {"PARITY_VARS_READ_TOKEN": "${{ secrets.PARITY_VARS_READ_TOKEN }}"}
    assert "env" not in planner
    assert planner["steps"][0]["with"]["persist-credentials"] is False
    assert workflow[True]["push"]["branches"] == ["master"]
    assert jobs["parity"]["name"] == "Parity / ${{ matrix.os }}"
    assert jobs["parity"]["needs"] == ["scope", "rust", "candidate", "parity-checks", "source-mutants"]
    assert not jobs["parity-checks"].get("continue-on-error", False)
    assert "parity-pool" in jobs["parity-checks"]["needs"]


def test_fork_regression_detects_removing_the_runner_event_guard():
    jobs = yaml.safe_load(PATH.read_text())["jobs"]
    expression = jobs["parity-checks"]["runs-on"]
    # The runner expression repeats event selection independently of the planner.
    weakened = "${{ matrix.os" + expression.split(" && matrix.os", 1)[1]
    values = context(event="pull_request", head="integrate/example", head_repo="fork/repo")
    assert evaluate(expression, values) == "ubuntu-latest"
    assert evaluate(weakened, values) == ["self-hosted", "pseudolife-parity-linux"]


def test_planner_step_errors_cannot_create_a_red_check_that_blocks_batch_merge():
    planner = yaml.safe_load(PATH.read_text())["jobs"]["parity-pool"]
    assert not planner.get("continue-on-error", False)
    checkout, pool = planner["steps"]
    assert checkout["id"] == "checkout" and checkout["continue-on-error"] is True
    assert pool["continue-on-error"] is True
    assert 0 < checkout["timeout-minutes"] + pool["timeout-minutes"] < planner["timeout-minutes"]
    values = context()
    values["steps"]["checkout"]["outcome"] = "failure"
    assert evaluate(pool["if"], values) is False


def test_mixed_case_integrate_ref_stays_hosted_when_scope_classifies_it_as_ordinary():
    jobs = yaml.safe_load(PATH.read_text())["jobs"]
    values = context(event="pull_request", head="Integrate/example")
    values["needs"]["scope"]["outputs"]["full"] = "false"
    planner = jobs["parity-pool"]
    assert evaluate(planner["if"], values) is False
    assert evaluate(planner["steps"][1]["if"], values) is False
    assert evaluate(jobs["parity-checks"]["runs-on"], values) == "ubuntu-latest"


@pytest.mark.parametrize("head", ["Integrate/example", "INTEGRATE/example", "iNtegrate/x"])
def test_mixed_case_integrate_ref_with_full_scope_never_reaches_the_token(head):
    # GitHub's startsWith ignores case, and scope leaves full=true for an
    # ordinary pull request whose diff is empty, so only the exact-case
    # scope output may admit the token step and the self-hosted label.
    jobs = yaml.safe_load(PATH.read_text())["jobs"]
    values = context(event="pull_request", ref="refs/pull/123/merge", head=head)
    assert values["needs"]["scope"]["outputs"] == {"full": "true", "integrate": "false"}
    planner = jobs["parity-pool"]
    step = next(step for step in planner["steps"] if step.get("id") == "pool")
    assert evaluate(planner["if"], values) is False
    assert evaluate(step["if"], values) is False
    assert evaluate(jobs["parity-checks"]["runs-on"], values) == "ubuntu-latest"


def test_every_trust_gate_reads_the_exact_case_scope_output():
    text = PATH.read_text()
    jobs = yaml.safe_load(text)["jobs"]
    planner = jobs["parity-pool"]
    step = next(step for step in planner["steps"] if step.get("id") == "pool")
    for expression in (planner["if"], step["if"], jobs["parity-checks"]["runs-on"]):
        assert "needs.scope.outputs.integrate == 'true'" in expression
        assert "startsWith(github.head_ref" not in expression
    assert jobs["scope"]["outputs"]["integrate"] == "${{ steps.trusted.outputs.integrate }}"


def run_integrate_step(tmp_path, event, head):
    step = next(s for s in yaml.safe_load(PATH.read_text())["jobs"]["scope"]["steps"]
                if s.get("id") == "trusted")
    assert step["shell"] == "pwsh"
    assert step["env"] == {"EVENT_NAME": "${{ github.event_name }}", "HEAD_REF": "${{ github.head_ref }}"}
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", step["run"]],
                            env=dict(os.environ, EVENT_NAME=event, HEAD_REF=head, GITHUB_OUTPUT=str(output)),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return output.read_text(encoding="utf-8-sig").split()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell 7")
@pytest.mark.parametrize("event,head,expected", [
    ("pull_request", "integrate/2026-11-06-batch-7", "true"),
    ("pull_request", "integrate/", "true"),
    # Near misses: case, prefix, missing slash, other events and no head ref.
    ("pull_request", "Integrate/2026-11-06-batch-7", "false"),
    ("pull_request", "INTEGRATE/x", "false"),
    ("pull_request", "integrate", "false"),
    ("pull_request", "xintegrate/x", "false"),
    ("pull_request", "", "false"),
    ("pull_request_target", "integrate/x", "false"),
    ("merge_group", "integrate/x", "false"),
    ("push", "", "false"),
])
def test_scope_marks_only_exact_integrate_refs(tmp_path, event, head, expected):
    assert run_integrate_step(tmp_path, event, head) == [f"integrate={expected}"]


@pytest.mark.parametrize("event,ref,head,revision", [
    ("pull_request", "refs/pull/123/merge", "integrate/example", BASE_SHA),
    ("push", "refs/heads/master", "", HEAD_SHA),
])
def test_planner_runs_reviewed_base_code_not_the_batch_merge_ref(event, ref, head, revision):
    # A batch member's code must not be able to read the token or print a
    # self-hosted choice: the reader comes from the base (or pushed master).
    planner = yaml.safe_load(PATH.read_text())["jobs"]["parity-pool"]
    checkout = planner["steps"][0]
    assert checkout["uses"] == "actions/checkout@v4"
    assert evaluate(checkout["with"]["ref"], context(event=event, ref=ref, head=head)) == revision
