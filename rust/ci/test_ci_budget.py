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
    # Read from the triggers, so a new workflow file cannot run unbudgeted.
    assert set(ci_budget.pull_request_workflows()) == set(WORKFLOWS) == set(budget())
    for name, entry in budget().items():
        assert len(entry["measured_on"]) >= 2, name
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
    assert pull_request_reachable(load("ci.yml")) == ["scope", "guards", "lock-resolve", "test", "test-lite-linux",
                                                      "test-lite-windows", "test-lite-macos", "frontend"]


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
UNNEEDED = {key: value for key, value in EXTRA.items() if key != "needs"}


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
    rust_entry["fingerprints"]["extra"] = ci_budget.fingerprint(workflow["jobs"]["extra"])  # as measure records
    assert check(workflow, raw, entry) == []


def test_an_estimate_counts_until_it_is_measured():
    # A shard-only change starts no parity job on its own pull request, so
    # it may enter an estimate that the batch run's measurement replaces.
    workflow, raw = rust()
    add_job(workflow, raw, "extra", EXTRA)
    entry = budget()
    rust_entry = entry["rust.yml"]
    for os_name in ("ubuntu-latest", "windows-latest"):
        rust_entry["jobs"][f"Extra / {os_name}"] = {"minutes": 1.0, "estimate": "its steps' times on run 1"}
    rust_entry["limits"]["jobs"] += 2
    rust_entry["limits"]["runner_minutes"] += 2
    rust_entry["fingerprints"]["extra"] = ci_budget.fingerprint(workflow["jobs"]["extra"])  # as measure records
    assert check(workflow, raw, entry) == []
    assert ci_budget.estimates(rust_entry) == ["Extra / ubuntu-latest", "Extra / windows-latest"]


@pytest.mark.parametrize("limit, words", [("jobs", "job count 43 is over its limit"),
                                          ("runner_minutes", "runner-minutes per full run, over the limit"),
                                          ("longest_job_minutes", "limit for one job"),
                                          ("critical_path_minutes", "slowest chain of needs")])
def test_each_limit_is_enforced_on_its_own(limit, words):
    workflow, raw = rust()
    entry = budget()
    limits = entry["rust.yml"]["limits"]
    limits[limit] = limits[limit] - 1 if limit == "jobs" else 1
    found = check(workflow, raw, entry)
    assert len(found) == 1 and words in found[0], found


def test_a_job_the_workflow_dropped_is_refused():
    entry = budget()
    entry["rust.yml"]["jobs"]["Retired / ubuntu-latest"] = {"minutes": 1.0}
    workflow, raw = rust()
    assert any("no longer has: ['Retired / ubuntu-latest']" in problem for problem in check(workflow, raw, entry))


def test_repeated_job_names_are_refused():
    workflow, raw = rust()
    add_job(workflow, raw, "scope-twin", workflow["jobs"]["scope"])
    assert any("job names repeat: ['Scope']" in problem for problem in check(workflow, raw))


def test_a_nightly_job_is_outside_the_pull_request_budget():
    workflow, raw = rust()
    nightly = dict(UNNEEDED, **{"if": "${{ !cancelled() && (github.event_name == 'schedule' || "
                                      "(github.event_name == 'workflow_dispatch' && inputs.mode == 'ci')) }}"})
    add_job(workflow, raw, "extra", nightly)
    assert check(workflow, raw) == []


@pytest.mark.parametrize("condition", [
    # The Opus review of d01d51f5: each of these starts on a pull request or
    # a master push, and a text match had counted them nightly-only.
    "${{ !(github.event_name == 'schedule') }}",
    "${{ github.event_name == 'schedule' || contains(github.event.pull_request.labels.*.name, 'heavy') }}",
    "${{ github.event_name == 'workflow_dispatch' || github.ref == 'refs/heads/master' }}",
    "${{ github.event_name != 'push' && github.event_name == 'schedule' || true }}",
])
def test_a_condition_a_pull_request_or_push_can_meet_counts(condition):
    workflow, raw = rust()
    add_job(workflow, raw, "extra", dict(UNNEEDED, **{"if": condition}))
    assert any("Extra / ubuntu-latest" in problem for problem in check(workflow, raw))


@pytest.mark.parametrize("condition, needs", [
    # Near misses the other way: these never start on a pull request,
    # merge group or push, so they need no budget entry.
    ("${{ inputs.mode == 'ci' }}", None),
    ("${{ !cancelled() && needs.stress-plan.outputs.kind == 'cargo' }}", "stress-plan"),
    ("${{ !cancelled() && needs.stress-plan.result=='success' }}", "stress-plan"),
    ("${{ always() && needs['stress-plan'].result == 'success' }}", "stress-plan"),
])
def test_a_condition_no_pull_request_can_meet_is_outside_the_budget(condition, needs):
    workflow, raw = rust()
    job = dict(UNNEEDED, **{"if": condition})
    if needs:
        job["needs"] = needs
        job["steps"] = [{"run": "echo ${{ needs['stress-plan'].outputs.kind }}"}]
    add_job(workflow, raw, "extra", job)
    assert check(workflow, raw) == []


def test_a_need_that_carries_nothing_is_refused():
    # The 2026-10-24 finding: every parity shard needed the whole Rust job
    # and read almost none of it, so it started 9 minutes after it could have.
    workflow, raw = rust()
    for target in (workflow, raw):
        target["jobs"]["parity-checks"]["needs"].append("rust")
    found = check(workflow, raw)
    assert any("parity-checks needs rust" in problem for problem in found)


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


def test_a_cache_key_has_one_saver():
    workflow, raw = rust()
    for target in (workflow, raw):
        step = next(s for s in target["jobs"]["fixtures"]["steps"]
                    if str(s.get("uses", "")).startswith("Swatinem/rust-cache"))
        step["with"].pop("save-if")
    found = check(workflow, raw)
    assert any("rust-cache key v0-rust-rust on windows-latest is saved by 2 runs" in problem for problem in found)


def test_one_matrix_job_saving_one_key_from_many_runs_is_refused():
    # Every source-mutant shard saved under one key until 2026-10-24.
    workflow, raw = rust()
    for target in (workflow, raw):
        step = next(s for s in target["jobs"]["source-mutants"]["steps"]
                    if str(s.get("uses", "")).startswith("Swatinem/rust-cache"))
        step["with"].pop("save-if")
    assert any("v0-rust-cli-source-mutants-source-mutants on ubuntu-latest is saved by 37 runs" in problem
               for problem in check(workflow, raw))


def test_one_key_saved_on_two_systems_is_two_caches():
    # rust-cache appends the runner's OS, so these never collide; a key with
    # add-job-id-key off is shared across jobs, so those do.
    cache = "Swatinem/rust-cache@v2"
    workflow = {"jobs": {
        "a": {"runs-on": "ubuntu-latest", "steps": [{"uses": cache, "with": {"shared-key": "k"}}]},
        "b": {"runs-on": "windows-latest", "steps": [{"uses": cache, "with": {"shared-key": "k"}}]},
        "c": {"runs-on": "ubuntu-latest", "steps": [{"uses": cache, "with": {"key": "k", "add-job-id-key": False}}]},
        "d": {"runs-on": "ubuntu-latest", "steps": [{"uses": cache, "with": {"key": "k"}}]}}}
    savers = ci_budget.cache_savers(workflow)
    assert savers[("v0-rust-k", "ubuntu-latest")] == ["a", "c"]
    assert savers[("v0-rust-k", "windows-latest")] == ["b"]
    assert savers[("v0-rust-k-d", "ubuntu-latest")] == ["d"]


def test_a_heavy_install_must_skip_byte_compiling():
    workflow, raw = rust()
    for target in (workflow, raw):
        step = next(s for s in target["jobs"]["parity-checks"]["steps"]
                    if s.get("name") == "Install checkout and test dependencies")
        step["run"] = step["run"].replace("--no-compile -e", "-e")
    assert any("without --no-compile" in problem for problem in check(workflow, raw))


@pytest.mark.parametrize("script, heavy", [
    ('python -m pip install ".[dev,lite]"', True),
    ("python -m pip install .", True),
    ('python -m pip install `\n    -e ".[dev]"', True),
    ('python -m pip install \\\n    "torch==2.13.0"', True),
    ('python -m pip install --index-url https://x "torch==2.13.0"', True),
    ('python -m pip install "mcp==2.2.0"', False),
    ('python -m pip install "onnx==1.22.0" ./wheels/x.whl', False),
])
def test_heavy_installs_are_found_however_they_are_written(script, heavy):
    workflow = {"jobs": {"j": {"steps": [{"run": script}]}}}
    assert bool(ci_budget.heavy_pip_installs(workflow)) == heavy


def test_matrix_names_follow_github_expansion():
    job = {"name": "J / ${{ matrix.os }} / ${{ matrix.suite }}",
           "strategy": {"matrix": {"os": ["a", "b"], "suite": ["x", "y"],
                                   "exclude": [{"os": "b", "suite": "y"}],
                                   "include": [{"suite": "x", "extra": 1}, {"suite": "z"}]}}}
    # An unset matrix key renders empty, as GitHub renders it.
    assert ci_budget.expand("j", job) == ["J / a / x", "J / a / y", "J / b / x", "J /  / z"]


def test_includes_follow_githubs_documented_example():
    # docs.github.com, "Expanding or adding matrix configurations": six jobs.
    job = {"runs-on": "x", "strategy": {"matrix": {
        "fruit": ["apple", "pear"], "animal": ["cat", "dog"],
        "include": [{"color": "green"}, {"color": "pink", "animal": "cat"}, {"fruit": "apple", "shape": "circle"},
                    {"fruit": "banana"}, {"fruit": "banana", "animal": "cat"}]}}}
    assert ci_budget.combinations(job) == [
        {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
        {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
        {"fruit": "pear", "animal": "cat", "color": "pink"},
        {"fruit": "pear", "animal": "dog", "color": "green"},
        {"fruit": "banana"},
        {"fruit": "banana", "animal": "cat"}]
    # A matrix job with no name is reported as "<id> (<values>)".
    assert ci_budget.expand("j", job)[0] == "j (apple, cat, pink, circle)"


def test_measure_refuses_a_single_run(capsys):
    with pytest.raises(SystemExit):
        ci_budget.main(["measure", "--workflow", "rust.yml", "1"])
    assert "two or more runs" in capsys.readouterr().err


def test_a_changed_job_needs_a_new_measurement():
    # Codex review of d01d51f5: an added setup step keeps the job's name,
    # so its old minutes would have passed unchanged.
    workflow, raw = rust()
    for target in (workflow, raw):
        target["jobs"]["fixtures"]["steps"].append({"name": "Sixty-minute setup", "run": "sleep 3600"})
    assert any("job fixtures changed since its minutes were measured" in problem for problem in check(workflow, raw))


def test_a_changed_job_whose_cost_holds_is_accepted_with_its_reason(tmp_path, monkeypatch):
    budget_file = tmp_path / "ci_budget.json"
    budget_file.write_text(BUDGET.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(ci_budget, "BUDGET", budget_file)
    workflow, raw = rust()
    for target in (workflow, raw):
        target["jobs"]["fixtures"]["steps"].append({"name": "Print the toolchain", "run": "rustc --version"})
    monkeypatch.setattr(ci_budget, "load", lambda name, resolved=True: copy.deepcopy(workflow if resolved else raw))
    with pytest.raises(SystemExit):
        ci_budget.main(["accept", "--workflow", "rust.yml", "--job", "fixtures", "--reason", " "])
    assert ci_budget.main(["accept", "--workflow", "rust.yml", "--job", "fixtures",
                           "--reason", "prints one line"]) == 0
    entry = json.loads(budget_file.read_text(encoding="utf-8"))
    assert entry["rust.yml"]["accepted"][-1]["reason"] == "prints one line"
    assert problems("rust.yml", workflow, entry, raw) == []


def test_a_raised_limit_names_its_reason():
    base = budget()
    current = copy.deepcopy(base)
    current["rust.yml"]["limits"]["runner_minutes"] += 50
    assert ci_budget.unexplained_raises(base, current) == [
        "rust.yml: raises ['runner_minutes'] without a new reason in limits.why"]
    current["rust.yml"]["limits"]["why"] = " "
    assert ci_budget.unexplained_raises(base, current)
    current["rust.yml"]["limits"]["why"] = "2026-11-01: the new w1c-tunnel shard, measured on runs 1 and 2"
    assert ci_budget.unexplained_raises(base, current) == []
    # Lowering a limit needs no reason.
    lowered = copy.deepcopy(base)
    lowered["rust.yml"]["limits"]["runner_minutes"] -= 50
    assert ci_budget.unexplained_raises(base, lowered) == []


def test_no_limit_rises_over_master_without_a_reason():
    import subprocess
    shown = subprocess.run(["git", "show", "origin/master:rust/ci/ci_budget.json"], cwd=ci_budget.ROOT,
                           capture_output=True, text=True)
    if shown.returncode:
        pytest.skip("origin/master has no CI budget yet (or is not fetched)")
    assert ci_budget.unexplained_raises(json.loads(shown.stdout), budget()) == []


def test_a_nightly_branch_does_not_hide_a_pull_request_branch():
    # Codex review of d01d51f5.
    workflow, raw = rust()
    add_job(workflow, raw, "extra", dict(EXTRA, **{
        "if": "${{ github.event_name == 'schedule' || needs.scope.outputs.rust == 'true' }}"}))
    assert any("Extra / ubuntu-latest" in problem for problem in check(workflow, raw))
