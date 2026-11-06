"""The Windows branch of parity-checks' runs-on: same trust boundary, listed suites only."""
import pytest
import yaml

from test_linux_pool_workflow import CASES, PATH, context, evaluate

WINDOWS = ["self-hosted", "pseudolife-parity-windows"]


def windows_context(suite="w1c-connection", suites='["w1c-connection"]', **changes):
    values = context(**changes)
    values["matrix"] = {"os": "windows-latest", "suite": suite}
    values["needs"]["parity-pool"]["outputs"]["windows_suites"] = suites
    return values


def runs_on():
    return yaml.safe_load(PATH.read_text())["jobs"]["parity-checks"]["runs-on"]


@pytest.mark.parametrize("changes,allowed", CASES)
def test_windows_route_shares_the_trusted_event_boundary(changes, allowed):
    assert evaluate(runs_on(), windows_context(**changes)) == (WINDOWS if allowed else "windows-latest")


@pytest.mark.parametrize("suite,suites,expected", [
    ("w1c-connection", '["w1c-connection"]', WINDOWS),
    # eval is the planned second shard but not yet allowlisted.
    ("eval", '["w1c-connection","eval"]', "windows-latest"),
    ("eval", '["eval"]', "windows-latest"),
    # A planner that lists a suite outside the allowlist cannot move it.
    ("judges", '["judges"]', "windows-latest"),
    ("cli-live", '["cli-live","w1c-connection"]', "windows-latest"),
    # Empty, missing or failed planner output keeps hosted.
    ("w1c-connection", "[]", "windows-latest"),
    ("w1c-connection", "", "windows-latest"),
])
def test_only_listed_allowlisted_windows_suites_move(suite, suites, expected):
    assert evaluate(runs_on(), windows_context(suite=suite, suites=suites)) == expected


def test_a_windows_choice_never_moves_a_linux_shard_or_the_reverse():
    values = windows_context()
    values["matrix"] = {"os": "ubuntu-latest", "suite": "w1c-connection"}
    values["needs"]["parity-pool"]["outputs"]["linux_pool"] = "hosted"
    assert evaluate(runs_on(), values) == "ubuntu-latest"
    values = windows_context(suite="cli-startup", suites="[]")
    values["needs"]["parity-pool"]["outputs"]["linux_pool"] = "self-hosted"
    assert evaluate(runs_on(), values) == "windows-latest"


def test_fork_regression_detects_removing_the_windows_event_guard():
    expression = runs_on()
    # Strip the event boundary from the Windows branch only: the branch then
    # starts at its matrix.os test.
    linux, windows = expression.split(" || github.repository", 1)
    weakened = linux + " || matrix.os == 'windows-latest'" + windows.split(" && matrix.os == 'windows-latest'", 1)[1]
    values = windows_context(event="pull_request", head="integrate/example", head_repo="fork/repo")
    assert evaluate(expression, values) == "windows-latest"
    assert evaluate(weakened, values) == WINDOWS


def test_the_planner_publishes_the_windows_output_from_its_only_token_step():
    text = PATH.read_text()
    planner = yaml.safe_load(text)["jobs"]["parity-pool"]
    assert planner["outputs"]["windows_suites"] == "${{ steps.pool.outputs.windows_suites }}"
    assert text.count("${{ secrets.PARITY_VARS_READ_TOKEN }}") == 1
    assert "PARITY_BATCH_WINDOWS_POOL" not in text and "PARITY_WINDOWS_HEALTH" not in text
