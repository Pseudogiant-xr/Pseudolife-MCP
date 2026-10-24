"""Every job a pull request can start is measured, and the totals stay
inside rust/ci/ci_budget.json's limits (docs/dev/ci-budget.md)."""
import copy
import json

import pytest

import ci_budget
from ci_budget import BUDGET, load, problems, pull_request_reachable, reachable_names

WORKFLOWS = ("rust.yml", "ci.yml")


def budget():
    return json.loads(BUDGET.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_workflow_is_within_its_budget(name):
    assert problems(name, load(name), budget(), load(name, resolved=False)) == []


def test_the_budget_covers_every_workflow_that_runs_on_pull_requests():
    assert set(budget()) == set(WORKFLOWS)
    for name, entry in budget().items():
        assert entry["measured_on"], name
        assert set(entry["limits"]) == {"jobs", "runner_minutes", "longest_job_minutes",
                                        "critical_path_minutes", "why"}, name


def test_a_full_rust_run_is_what_a_batch_starts():
    workflow = load("rust.yml")
    assert pull_request_reachable(workflow) == ["scope", "rust", "fixtures", "candidate", "candidate-harness",
                                                "parity-checks", "daemon-image", "parity"]
    names = reachable_names(workflow)
    assert "Parity checks / ubuntu-latest / h5-light" in names
    assert "Parity checks / windows-latest / h5-light" not in names
    assert "Parity checks / windows-latest / cli-live" not in names


def rust():
    return copy.deepcopy(load("rust.yml")), copy.deepcopy(load("rust.yml", resolved=False))


def check(workflow, raw, entry=None):
    return problems("rust.yml", workflow, entry or budget(), raw)


def add_job(workflow, raw, job_id, job):
    for target in (workflow, raw):
        target["jobs"][job_id] = copy.deepcopy(job)


EXTRA = {"name": "Extra / ${{ matrix.os }}", "needs": "scope", "runs-on": "${{ matrix.os }}",
         "strategy": {"matrix": {"os": ["ubuntu-latest", "windows-latest"]}},
         "if": "${{ !cancelled() && needs.scope.outputs.rust != 'false' }}", "steps": [{"run": "true"}]}


def test_a_new_pull_request_job_needs_a_measurement():
    workflow, raw = rust()
    add_job(workflow, raw, "extra", EXTRA)
    found = check(workflow, raw)
    assert any("no measured minutes" in problem and "Extra / windows-latest" in problem for problem in found)


def test_a_new_shard_needs_a_measurement():
    workflow, raw = rust()
    matrix = workflow["jobs"]["parity-checks"]["strategy"]["matrix"]
    matrix["suite"].append("cli-extra")
    assert any("Parity checks / windows-latest / cli-extra" in problem for problem in check(workflow, raw))


def test_a_measured_job_within_the_limits_passes():
    # The near miss: a legitimate addition that brings its measurement and
    # room under the limits is accepted.
    workflow, raw = rust()
    add_job(workflow, raw, "extra", EXTRA)
    entry = budget()
    rust_entry = entry["rust.yml"]
    for os_name in ("ubuntu-latest", "windows-latest"):
        rust_entry["jobs"][f"Extra / {os_name}"] = {"minutes": 1.0}
    rust_entry["limits"]["jobs"] += 2
    rust_entry["limits"]["runner_minutes"] += 2
    assert check(workflow, raw, entry) == []


def test_a_job_over_the_limits_is_refused():
    workflow, raw = rust()
    entry = budget()
    limits = entry["rust.yml"]["limits"]
    limits["jobs"] -= 1
    limits["runner_minutes"] = 1
    limits["longest_job_minutes"] = 1
    found = " ".join(check(workflow, raw, entry))
    assert "over the limit of" in found and "runner-minutes" in found and "limit for one job" in found


def test_a_nightly_job_is_outside_the_pull_request_budget():
    workflow, raw = rust()
    unneeded = {key: value for key, value in EXTRA.items() if key != "needs"}
    nightly = dict(unneeded, **{"if": "${{ !cancelled() && (github.event_name == 'schedule' || "
                                      "(github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')) }}"})
    add_job(workflow, raw, "extra", nightly)
    assert check(workflow, raw) == []
    # Any inequality on the event admits a pull request.
    loose = dict(unneeded, **{"if": "${{ github.event_name != 'push' && github.event_name == 'schedule' }}"})
    add_job(workflow, raw, "extra", loose)
    assert any("Extra / ubuntu-latest" in problem for problem in check(workflow, raw))


def test_a_need_that_carries_nothing_is_refused():
    # The 2026-10-24 finding: every parity shard needed the whole Rust job
    # and read none of it, so it started 9 minutes after it could have.
    workflow, raw = rust()
    for target in (workflow, raw):
        target["jobs"]["parity-checks"]["needs"].append("rust")
    found = check(workflow, raw)
    assert any("parity-checks needs rust" in problem for problem in found)


def test_a_cache_key_has_one_saver():
    workflow, raw = rust()
    for target in (workflow, raw):
        step = next(s for s in target["jobs"]["fixtures"]["steps"]
                    if str(s.get("uses", "")).startswith("Swatinem/rust-cache"))
        step["with"].pop("save-if")
    assert any("rust-cache key rust is saved by ['rust', 'fixtures']" in problem for problem in check(workflow, raw))


def test_a_heavy_install_must_skip_byte_compiling():
    workflow, raw = rust()
    for target in (workflow, raw):
        step = next(s for s in target["jobs"]["parity-checks"]["steps"]
                    if s.get("name") == "Install checkout and test dependencies")
        step["run"] = step["run"].replace("--no-compile -e", "-e")
    assert any("without --no-compile" in problem for problem in check(workflow, raw))


def test_matrix_names_follow_github_expansion():
    job = {"name": "J / ${{ matrix.os }} / ${{ matrix.suite }}",
           "strategy": {"matrix": {"os": ["a", "b"], "suite": ["x", "y"],
                                   "exclude": [{"os": "b", "suite": "y"}],
                                   "include": [{"suite": "x", "extra": 1}, {"suite": "z"}]}}}
    assert ci_budget.expand("j", job) == ["J / a / x", "J / a / y", "J / b / x", "J / ${{ matrix.os }} / z"]


def test_waiting_on_a_slower_job_lengthens_the_critical_path():
    # Master before #922: the parity shards needed the whole Rust job, which
    # carried data (the bind fixture two shards used) but took 18 minutes.
    workflow, raw = rust()
    for target in (workflow, raw):
        target["jobs"]["parity-checks"]["needs"].append("rust")
        target["jobs"]["parity-checks"]["steps"].append(
            {"uses": "actions/download-artifact@v4", "with": {"name": "rust-made"}})
        target["jobs"]["rust"]["steps"].append(
            {"uses": "actions/upload-artifact@v4", "with": {"name": "rust-made", "path": "x"}})
    found = check(workflow, raw)
    assert any("slowest chain of needs, scope -> rust -> parity-checks" in problem for problem in found)
    assert not any("parity-checks needs rust" in problem for problem in found)
