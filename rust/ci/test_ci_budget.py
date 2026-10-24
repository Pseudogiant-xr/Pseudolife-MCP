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
    assert any("rust-cache key v0-rust-rust on Windows is saved by 2 runs" in problem for problem in found)


def test_one_matrix_job_saving_one_key_from_many_runs_is_refused():
    # Every source-mutant shard saved under one key until 2026-10-24.
    workflow, raw = rust()
    for target in (workflow, raw):
        step = next(s for s in target["jobs"]["source-mutants"]["steps"]
                    if str(s.get("uses", "")).startswith("Swatinem/rust-cache"))
        step["with"].pop("save-if")
    assert any("v0-rust-cli-source-mutants-source-mutants on Linux is saved by 37 runs" in problem
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
    assert savers[("v0-rust-k", "Linux")] == ["a", "c"]
    assert savers[("v0-rust-k", "Windows")] == ["b"]
    assert savers[("v0-rust-k-d", "Linux")] == ["d"]


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



@pytest.mark.parametrize("condition", [
    # The second Opus and Codex reviews of the budget (897ba3aa): GitHub's
    # && and || return an operand, and unlike types compare as numbers.
    "${{ (github.event_name == 'schedule' && 'nightly' || 'pr') == 'pr' }}",
    "${{ (github.event_name == 'pull_request' && 'yes' || 'no') == 'yes' }}",
    "${{ (inputs.os || 'ubuntu-latest') == 'ubuntu-latest' }}",
    "${{ inputs.mode == '' || inputs.mode == 'ci' }}",
    "${{ inputs.skip == false }}",
    "${{ github.run_number > -1 }}",
    "${{ github.run_number > 1e3 || github.run_number < 0x10 }}",
    # A condition this reader cannot follow counts rather than failing.
    "${{ github.event.pull_request.labels.*.name }} weird ~ syntax",
])
def test_conditions_that_hold_on_a_pull_request_count(condition):
    workflow, raw = rust()
    add_job(workflow, raw, "extra", dict(UNNEEDED, **{"if": condition}))
    found = check(workflow, raw)
    assert any("no measured minutes" in problem and "Extra / ubuntu-latest" in problem for problem in found), found


@pytest.mark.parametrize("left, right, same", [(None, "", True), (None, False, True), ("", 0, True),
                                               ("1", 1, True), ("CI", "ci", True), ("ci", None, False),
                                               ("x", 0, False), (True, "1", True)])
def test_equality_follows_githubs_coercion(left, right, same):
    assert ci_budget.equal(left, right) is same


def write_workflow(directory, name, text):
    (directory / name).write_text(text, encoding="utf-8")


def test_every_pull_request_workflow_is_found_however_it_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(ci_budget, "WORKFLOWS", tmp_path)
    write_workflow(tmp_path, "a.yml", "on: [pull_request]\njobs: {}\n")
    write_workflow(tmp_path, "b.yaml", "on:\n  merge_group:\njobs: {}\n")
    write_workflow(tmp_path, "c.yml", "'on': pull_request\njobs: {}\n")
    write_workflow(tmp_path, "d.yml", "on:\n  schedule:\n    - cron: '0 0 * * *'\njobs: {}\n")
    assert ci_budget.pull_request_workflows() == ["a.yml", "b.yaml", "c.yml"]


def test_one_key_on_two_labels_of_one_system_collides():
    # rust-cache keys on the OS and architecture, not the runs-on label.
    cache = "Swatinem/rust-cache@v2"
    workflow = {"jobs": {
        "a": {"runs-on": "ubuntu-latest", "steps": [{"uses": cache, "with": {"shared-key": "k"}}]},
        "b": {"runs-on": "ubuntu-24.04", "steps": [{"uses": cache, "with": {"shared-key": "k"}}]},
        "c": {"runs-on": "ubuntu-24.04-arm", "steps": [{"uses": cache, "with": {"shared-key": "k"}}]}}}
    savers = ci_budget.cache_savers(workflow)
    assert savers[("v0-rust-k", "Linux")] == ["a", "b"]
    assert savers[("v0-rust-k", "Linux-ARM64")] == ["c"]


def test_a_save_that_only_a_manual_run_makes_still_saves():
    cache = "Swatinem/rust-cache@v2"
    workflow = {"jobs": {"a": {"runs-on": "ubuntu-latest", "steps": [
        {"uses": cache, "if": "inputs.mode == 'stress'", "with": {"shared-key": "k"}}]}}}
    assert ci_budget.cache_savers(workflow) == {("v0-rust-k", "Linux"): ["a"]}


def test_remeasure_uses_only_runs_of_the_current_definition():
    workflow, _ = rust()
    entry = copy.deepcopy(budget()["rust.yml"])
    names = reachable_names(workflow)
    samples = {name: {"new": 2.0, "old": 50.0} for name in names}
    stale = {"fixtures": ["old"]}
    assert ci_budget.remeasure(entry, workflow, samples, stale) == []
    assert entry["jobs"]["Fixtures / windows-latest"] == {"minutes": 2.0}
    assert entry["jobs"]["Scope"] == {"minutes": 26.0}


def test_remeasure_refuses_a_changed_job_it_has_no_current_run_of():
    # Codex review of 897ba3aa: a changed job the runs did not measure kept
    # its old minutes and took the new fingerprint.
    workflow, _ = rust()
    workflow["jobs"]["fixtures"]["steps"].append({"run": "sleep 3600"})
    entry = copy.deepcopy(budget()["rust.yml"])
    before = copy.deepcopy(entry)
    samples = {"Scope": {"new": 0.3}}
    missing = ci_budget.remeasure(entry, workflow, samples, {})
    assert "Fixtures / windows-latest" in missing and "Scope" not in missing
    assert entry == before
    # An unchanged job the runs did not run keeps its measurement.
    workflow, _ = rust()
    assert ci_budget.remeasure(entry, workflow, samples, {}) == []


def test_the_estimate_route_records_the_new_job_and_its_template(tmp_path, monkeypatch):
    budget_file = tmp_path / "ci_budget.json"
    budget_file.write_text(BUDGET.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(ci_budget, "BUDGET", budget_file)
    workflow, raw = rust()
    workflow["jobs"]["parity-checks"]["strategy"]["matrix"]["suite"].append("cli-extra")
    monkeypatch.setattr(ci_budget, "load", lambda name, resolved=True: copy.deepcopy(workflow if resolved else raw))
    for os_name in ("ubuntu-latest", "windows-latest"):
        assert ci_budget.main(["estimate", "--workflow", "rust.yml",
                               "--job", f"Parity checks / {os_name} / cli-extra", "--minutes", "8",
                               "--reason", "cli's step times on run 40074592535"]) == 0
    entry = json.loads(budget_file.read_text(encoding="utf-8"))
    entry["rust.yml"]["limits"]["jobs"] += 2
    entry["rust.yml"]["limits"]["runner_minutes"] += 20
    # Estimating never vouches for the other shards (Codex review of
    # 4c68714a): the changed parity-checks job needs its own reason.
    assert [problem for problem in problems("rust.yml", workflow, entry, raw)
            if "job parity-checks changed" in problem]
    budget_file.write_text(json.dumps(entry), encoding="utf-8")
    assert ci_budget.main(["accept", "--workflow", "rust.yml", "--job", "parity-checks", "--reason",
                           "adds the cli-extra suite (estimated); no other shard's steps changed"]) == 0
    entry = json.loads(budget_file.read_text(encoding="utf-8"))
    assert problems("rust.yml", workflow, entry, raw) == []
    assert ci_budget.estimates(entry["rust.yml"]) == ["Parity checks / ubuntu-latest / cli-extra",
                                                      "Parity checks / windows-latest / cli-extra"]
    with pytest.raises(SystemExit):
        ci_budget.main(["estimate", "--workflow", "rust.yml", "--job", "Not a job", "--minutes", "1",
                        "--reason", "x"])


def test_an_entry_without_minutes_is_named():
    entry = budget()
    entry["rust.yml"]["jobs"]["Scope"] = {"estimate": "forgot the minutes"}
    workflow, raw = rust()
    assert check(workflow, raw, entry) == ["rust.yml: budget entries without numeric minutes: ['Scope']"]



@pytest.mark.parametrize("condition", ["${{ github.event_name == }}", "${{ (github.event_name == 'push' }}",
                                       "${{ contains(github.event_name, 'p' }}"])
def test_a_truncated_condition_counts_rather_than_crashing(condition):
    workflow, raw = rust()
    add_job(workflow, raw, "extra", dict(UNNEEDED, **{"if": condition}))
    assert any("Extra / ubuntu-latest" in problem for problem in check(workflow, raw))


def test_an_estimate_never_replaces_a_measurement(tmp_path, monkeypatch):
    budget_file = tmp_path / "ci_budget.json"
    budget_file.write_text(BUDGET.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(ci_budget, "BUDGET", budget_file)
    with pytest.raises(SystemExit):
        ci_budget.main(["estimate", "--workflow", "rust.yml", "--job", "Parity checks / ubuntu-latest / eval",
                        "--minutes", "1", "--reason", "cheaper, surely"])
    assert budget_file.read_text(encoding="utf-8") == BUDGET.read_text(encoding="utf-8")


def test_an_unreadable_run_is_left_out_not_fatal(monkeypatch, capsys):
    import subprocess
    monkeypatch.setattr(ci_budget, "fetch_run", lambda run_id: {"head_sha": "f" * 40})

    def missing(name, sha):
        raise subprocess.CalledProcessError(1, ["gh"])
    monkeypatch.setattr(ci_budget, "definitions_at", missing)
    stale = ci_budget.stale_definitions("rust.yml", ["7"], {"scope": "x", "rust": "y"})
    assert stale == {"scope": ["7"], "rust": ["7"]}
    assert "run 7: its workflow at ffffffffffff cannot be read" in capsys.readouterr().err



def test_a_condition_that_ends_falsy_keeps_the_job_out():
    # Codex review of 4c68714a: && returns an operand, here None on a pull
    # request, which is falsy though not False.
    workflow, raw = rust()
    add_job(workflow, raw, "extra", dict(UNNEEDED, **{"if": "${{ inputs.mode && inputs.mode == 'stress' }}"}))
    assert check(workflow, raw) == []


def test_a_pull_request_run_executed_its_merge_with_the_base(monkeypatch):
    # Codex review of 4c68714a: jobs the branch left alone run as the base
    # has them, jobs it changed as the branch has them.
    # Only what the head settles is credited: b ran as master-b, not as the
    # head's old-b, so its times count for neither (a job only the base has,
    # c, and one only the head has, d, are handled too).
    definitions = {"head": {"a": "branch-a", "b": "old-b", "d": "new-d"},
                   "base": {"a": "old-a", "b": "master-b", "c": "base-c"},
                   "fork-point": {"a": "old-a", "b": "old-b", "c": "base-c"}}
    monkeypatch.setattr(ci_budget, "definitions_at", lambda name, sha: definitions[sha])
    monkeypatch.setattr(ci_budget, "merge_base", lambda base, head: "fork-point")
    run = {"event": "pull_request", "head_sha": "head", "pull_requests": [{"base": {"sha": "base"}}]}
    assert ci_budget.executed_definitions("rust.yml", run) == {"a": "branch-a", "d": "new-d"}
    assert ci_budget.executed_definitions("rust.yml", dict(run, event="push")) == definitions["head"]



def test_a_run_with_another_parity_scope_ran_other_shards(monkeypatch):
    # Opus review of 127464a9: shards' rows and parts live in
    # parity_scope.py, so old runs resolved with today's file looked current.
    raw_text = (ci_budget.WORKFLOWS / "rust.yml").read_text(encoding="utf-8")
    scope_text = (ci_budget.ROOT / "rust/ci/parity_scope.py").read_text(encoding="utf-8")
    files = {".github/workflows/rust.yml": raw_text, "rust/ci/parity_scope.py": scope_text}
    monkeypatch.setattr(ci_budget, "_contents", lambda path, sha: files[path])
    current = ci_budget.fingerprints(load("rust.yml"))
    assert ci_budget.definitions_at("rust.yml", "same") == current
    files["rust/ci/parity_scope.py"] = scope_text.replace("'connect', 'maintainer'", "'connect'")
    moved = ci_budget.definitions_at("rust.yml", "moved")
    assert "parity-checks" not in moved and moved["candidate"] == current["candidate"]


def test_a_closed_pull_requests_run_counts_only_once_master_has_it(monkeypatch):
    monkeypatch.setattr(ci_budget, "definitions_at", lambda name, sha: {"a": "head-a"})
    run = {"event": "pull_request", "head_sha": "h", "pull_requests": []}
    monkeypatch.setattr(ci_budget, "on_master", lambda sha: True)
    assert ci_budget.executed_definitions("rust.yml", run) == {"a": "head-a"}
    monkeypatch.setattr(ci_budget, "on_master", lambda sha: False)
    assert ci_budget.executed_definitions("rust.yml", run) == {}
