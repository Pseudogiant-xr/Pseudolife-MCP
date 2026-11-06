"""Exercise the real workflow guards, including forged planner outputs."""
import json
from pathlib import Path

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


def context(event="push", ref="refs/heads/master", head="", head_repo=REPO, repository=REPO):
    return {"github": {"event_name": event, "ref": ref, "head_ref": head,
                       "repository": repository, "event": {"pull_request": {
                           "head": {"repo": {"full_name": head_repo}}}}},
            "matrix": {"os": "ubuntu-latest", "suite": "cli-startup"},
            "needs": {**{name: {"result": "success"} for name in
                         ("candidate", "candidate-harness", "fixtures")},
                      "scope": {"result": "success", "outputs": {"full": "true"}},
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
    # A forged planner output must still be blocked by the runner expression.
    weakened = "${{ matrix.os" + expression.split(" && matrix.os", 1)[1]
    values = context(event="pull_request", head="integrate/example", head_repo="fork/repo")
    assert evaluate(expression, values) == "ubuntu-latest"
    assert evaluate(weakened, values) == ["self-hosted", "pseudolife-parity-linux"]
