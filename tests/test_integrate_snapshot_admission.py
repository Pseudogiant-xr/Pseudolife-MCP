"""Batch admission reads the own-head replay checkpoint, not job log text."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
JOBS = ("Rust / ubuntu-latest", "Rust / windows-latest")
STEP = "Replay touched PR goldens"


@pytest.fixture
def integrate():
    spec = importlib.util.spec_from_file_location("snapshot_admission", ROOT / "ops/integrate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def member(integrate, state="satisfied"):
    value = integrate.Member(123, "change", SHA, "https://github.example/pr/123", "NONE", "pending")
    value.snapshot_required = True
    value.snapshot = state
    value.snapshot_why = "snapshot checkpoint " + state
    return value


@pytest.mark.parametrize("state,admitted", [
    ("satisfied", True), ("not required", True), ("pending", False),
    ("failing", False), ("unverified", False),
])
def test_admission_state(integrate, state, admitted):
    assert (integrate._not_ready(member(integrate, state)) is None) is admitted


@pytest.mark.parametrize("state", ["pending", "failing", "unverified", "satisfied", "not required"])
def test_status_explains_snapshot_admission_before_build(integrate, monkeypatch, capsys, state):
    value = member(integrate, state)
    value.rust_jobs = "Rust / ubuntu-latest: in_progress; Rust / windows-latest: success"
    # Keep status on a member branch, with no integrate/* plan to load.
    monkeypatch.setattr(integrate, "git", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, "topic/snapshot-admission\n", ""))
    monkeypatch.setattr(integrate, "_out", lambda *a: SHA)
    monkeypatch.setattr(integrate, "ready", lambda *a: [value])
    monkeypatch.setattr(integrate, "_fetch_head", lambda *a: SHA)
    monkeypatch.setattr(integrate, "_merge_tree", lambda *a: (True, []))
    assert integrate.status(SimpleNamespace(label="ready-to-batch")) == 0
    out = capsys.readouterr().out
    assert value.rust_jobs in out
    assert f"snapshot {state}" in out
    if state in ("pending", "failing", "unverified"):
        assert "would leave out: " + value.snapshot_why in out
    else:
        assert "would leave out" not in out


@pytest.fixture
def actions(integrate, monkeypatch):
    rollup, jobs = [], {}
    run = {"head_sha": SHA, "event": "pull_request", "path": ".github/workflows/rust.yml",
           "run_attempt": 1}
    for number, name in enumerate(JOBS, 1):
        rollup.append({"name": name, "status": "IN_PROGRESS", "detailsUrl":
                       f"https://github.com/Pseudogiant-xr/Pseudolife-MCP/actions/runs/9/job/{number}"})
        jobs[str(number)] = {"name": name, "head_sha": SHA, "run_id": 9, "run_attempt": 1,
                            "status": "in_progress", "conclusion": None,
                            "started_at": "2026-11-06T01:00:00Z", "completed_at": None,
                            "steps": [{"name": "Build candidate", "number": 1,
                                       "status": "completed", "conclusion": "success"},
                                      {"name": STEP, "number": 2,
                                       "status": "completed", "conclusion": "success"},
                                      {"name": "Nextest", "number": 3,
                                       "status": "in_progress", "conclusion": None}]}
    calls = []

    def gh(*args):
        calls.append(args)
        assert args[0] == "api"
        if "/actions/jobs/" in args[1]:
            return json.dumps(jobs[args[1].rsplit("/", 1)[1]])
        assert args[1].endswith("/actions/runs/9")
        return json.dumps(run)

    monkeypatch.setattr(integrate, "gh", gh)
    return rollup, jobs, run, calls


@pytest.mark.parametrize("conclusion,state", [
    ("success", "satisfied"), ("skipped", "satisfied"),
    (None, "pending"), ("failure", "failing"), ("cancelled", "pending"),
])
def test_exact_replay_step_controls_admission(integrate, actions, conclusion, state):
    rollup, jobs, _, _ = actions
    jobs["1"]["steps"][1].update(conclusion=conclusion,
                                  status="completed" if conclusion else "in_progress")
    actual, why = integrate._snapshot_state(SHA, rollup, True)
    assert actual == state
    value = member(integrate, actual)
    value.snapshot_why = why
    assert (integrate._not_ready(value) is None) is (state == "satisfied")


def test_no_rows_needs_no_actions_metadata(integrate, monkeypatch):
    monkeypatch.setattr(integrate, "gh", lambda *a: pytest.fail("no-row PR queried Actions"))
    assert integrate._snapshot_state(SHA, [], False) == ("not required", "")


@pytest.mark.parametrize("near_miss", ["Rust / ubuntu-latest replay", "Replay touched PR goldens"])
def test_similarly_named_check_cannot_satisfy(integrate, actions, near_miss):
    rollup, _, _, calls = actions
    rollup[0]["name"] = near_miss
    assert integrate._snapshot_state(SHA, rollup, True)[0] == "pending"
    assert not any(args[1].endswith("/jobs/1") for args in calls)


def test_unrelated_pending_check_does_not_block_finished_replay(integrate, actions):
    rollup, _, _, _ = actions
    rollup.append({"name": JOBS[0] + " replay", "status": "IN_PROGRESS"})
    assert integrate._snapshot_state(SHA, rollup, True)[0] == "satisfied"


@pytest.mark.parametrize("where,key,value", [
    ("job", "head_sha", "b" * 40), ("run", "head_sha", "b" * 40),
    ("run", "event", "workflow_dispatch"), ("run", "path", ".github/workflows/ci.yml"),
    ("run", "run_attempt", 2), ("job", "name", JOBS[0] + " replay"),
])
def test_wrong_head_workflow_event_or_attempt_cannot_satisfy(integrate, actions, where, key, value):
    rollup, jobs, run, _ = actions
    (jobs["1"] if where == "job" else run)[key] = value
    assert integrate._snapshot_state(SHA, rollup, True)[0] == "pending"


@pytest.mark.parametrize("change", ["missing", "similar", "duplicate"])
def test_step_identity_is_exact_and_unambiguous(integrate, actions, change):
    rollup, jobs, _, _ = actions
    steps = jobs["1"]["steps"]
    if change == "missing":
        steps.pop(1)
    elif change == "similar":
        steps[1]["name"] += " summary"
    else:
        steps.append(copy.deepcopy(steps[1]))
    assert integrate._snapshot_state(SHA, rollup, True)[0] == "pending"


@pytest.mark.parametrize("prerequisite,state", [("failure", "failing"), ("cancelled", "pending")])
def test_skipped_after_failed_or_cancelled_setup_is_not_a_pass(integrate, actions, prerequisite, state):
    rollup, jobs, _, _ = actions
    jobs["1"].update(status="completed", conclusion=prerequisite)
    jobs["1"]["steps"][0]["conclusion"] = prerequisite
    jobs["1"]["steps"][1]["conclusion"] = "skipped"
    assert integrate._snapshot_state(SHA, rollup, True)[0] == state


def test_api_failure_is_unverified(integrate, actions, monkeypatch):
    def unavailable(*args):
        raise integrate.Refusal("HTTP 403: Resource not accessible by integration\nsecond line")
    monkeypatch.setattr(integrate, "gh", unavailable)
    state, why = integrate._snapshot_state(SHA, actions[0], True)
    assert state == "unverified"
    assert "HTTP 403: Resource not accessible by integration" in why
    assert "second line" not in why


@pytest.mark.parametrize("missing", [False, True])
def test_hand_cancelled_replay_names_push_or_rerun(integrate, actions, missing):
    rollup, jobs, _, _ = actions
    jobs["1"].update(status="completed", conclusion="cancelled")
    if missing:
        jobs["1"]["steps"].pop(1)
    else:
        jobs["1"]["steps"][1]["conclusion"] = "cancelled"
    state, why = integrate._snapshot_state(SHA, rollup, True)
    assert state == "pending"
    assert "cancelled" in why and "push" in why and "rerun" in why


def test_pre_replay_workflow_names_push_or_rerun(integrate, actions):
    rollup, jobs, _, _ = actions
    jobs["1"].update(status="completed", conclusion="success")
    jobs["1"]["steps"].pop(1)
    state, why = integrate._snapshot_state(SHA, rollup, True)
    assert state == "pending"
    assert "absent" in why and "push" in why and "rerun" in why


def test_cancel_after_successful_replay_keeps_the_checkpoint(integrate, actions):
    rollup, jobs, _, _ = actions
    jobs["1"].update(status="completed", conclusion="cancelled")
    assert integrate._snapshot_state(SHA, rollup, True) == ("satisfied", "")


@pytest.mark.parametrize("prerequisite,state", [("success", "satisfied"), ("cancelled", "pending")])
def test_cancel_after_skipped_replay_uses_step_level_states(integrate, actions, prerequisite, state):
    rollup, jobs, _, _ = actions
    jobs["1"].update(status="completed", conclusion="cancelled")
    jobs["1"]["steps"][0]["conclusion"] = prerequisite
    jobs["1"]["steps"][1]["conclusion"] = "skipped"
    assert integrate._snapshot_state(SHA, rollup, True)[0] == state


def test_active_replay_waits_without_suggesting_a_restart(integrate, actions):
    rollup, jobs, _, _ = actions
    jobs["1"]["steps"][1].update(status="in_progress", conclusion=None)
    state, why = integrate._snapshot_state(SHA, rollup, True)
    assert state == "pending"
    assert "rerun" not in why


@pytest.mark.parametrize("conclusion,state", [
    ("success", "satisfied"), (None, "pending"),
    ("failure", "failing"), ("cancelled", "pending"),
])
def test_skipped_replay_uses_job_verdict_for_tolerated_setup_failure(integrate, actions, conclusion, state):
    rollup, jobs, _, _ = actions
    jobs["1"].update(status="completed" if conclusion else "in_progress", conclusion=conclusion)
    jobs["1"]["steps"][0]["conclusion"] = "failure"
    jobs["1"]["steps"][1]["conclusion"] = "skipped"
    assert integrate._snapshot_state(SHA, rollup, True)[0] == state


# The files the base's CI scope verdict is read from (ops/integrate.py
# DOCS_SCOPE_FILES), as the frozen base carries them.
SCOPE_FILES = ("rust/ci/docs_scope.py", "rust/ci/parity_scope.py", "ops/ci_batch_passed.py",
               "rust/producer_census.py")


def _raw_diff(files, mode="100644"):
    return "".join(f":{mode} {mode} {'0' * 40} {'1' * 40} M\0{path}\0" for path in files)


def _frozen_base(tmp_path, files, *, mode="100644", ci_scope=True):
    """Serve the base's selector and CI scope through `_out`, with member
    copies of both in the checkout that raise if anything imports them."""
    base = {path: (ROOT / path).read_text(encoding="utf-8")
            for path in (*SCOPE_FILES, "rust/ci/pr_precheck.py")}
    (tmp_path / "rust/ci").mkdir(parents=True)
    marker = tmp_path / "executed"
    for path in ("rust/ci/pr_precheck.py", "rust/ci/docs_scope.py"):
        (tmp_path / path).write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('member code')\n")
    (tmp_path / "rust/probe.py").write_text("from tests import test_runtime_seed\n")

    def out(*args):
        if args[0] == "ls-tree":
            return f"100644 blob {args[-1]}" if ci_scope or args[-1] != "rust/ci/docs_scope.py" else ""
        if args[0] == "rev-parse":
            return str(tmp_path)
        if args[0] == "diff":
            assert args[1:4] == ("--raw", "-z", "--no-renames"), args
            return _raw_diff(files, mode)
        assert args[0] == "show"
        return base[args[1].split(":", 1)[1]]

    return out, marker


@pytest.mark.parametrize("files,required", [
    (["docs/guide.md"], False), (["tests/test_feature.py"], False),
    (["docs/guide.md", "tests/test_feature.py", "changelog.d/note.md"], False),
    (["docs/guide.md", "rust/producer-census.json"], False),
    (["rust/CUTOVER.md", "rust/producer-census.json"], False),
    (["rust/producer-census.json"], True),
    (["docs/guide.md", "rust/producer-census.json.bak"], True),
    (["docs/guide.md", "rust/producer-census.json", "pseudolife_memory/doctor_cli.py"], True),
    (["pseudolife_memory/doctor_cli.py"], True), (["rust/shim/src/main.rs"], True),
    (["tests/test_runtime_seed.py"], True),
    # CI's scope skips the Rust lanes of Markdown under evals/ (#1992, #2010),
    # so admission waits for no replay there; prompts and code still need it.
    (["evals/README.md"], False), (["evals/RUST-DRIVERS.md", "llms-full.txt"], False),
    (["evals/prompts/extract.md"], True), (["evals/README.md", "rust/shim/src/main.rs"], True),
    (["evals/README.md", "evals/harness.py"], True),
])
def test_requirement_uses_frozen_selector_without_importing_member_code(
        integrate, monkeypatch, tmp_path, files, required):
    out, marker = _frozen_base(tmp_path, files)
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="merged-tree\n"))
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    assert integrate._snapshot_requirement(member(integrate), "base") == (required, "")
    value = member(integrate)
    value.snapshot, value.snapshot_why = integrate._snapshot_state(SHA, [], required)
    assert (integrate._not_ready(value) is None) is (not required)
    assert not marker.exists()


@pytest.mark.parametrize("files", [["evals/README.md"], ["evals/RUST-DRIVERS.md", "llms-full.txt"]])
def test_requirement_agrees_with_the_ci_scope_verdict(integrate, monkeypatch, tmp_path, files):
    # Admission and the scope job call one predicate: whatever the Rust
    # workflow skips for a pull request, admission does not demand.
    from rust.ci import docs_scope

    assert docs_scope.pull_request_skips_rust(files, True) is True
    out, _ = _frozen_base(tmp_path, files)
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="merged-tree\n"))
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    skipped = [{"name": "Scope", "status": "COMPLETED", "conclusion": "SUCCESS"},
               {"name": "Rust / ${{ matrix.os }}", "status": "COMPLETED", "conclusion": "SKIPPED"}]
    assert integrate._snapshot_requirement(member(integrate), "base") == (False, "")
    assert integrate._snapshot_requirement(member(integrate), "base", skipped) == (False, "")
    # When the scope job's docs test selection fails, it runs the Rust lanes
    # for these paths after all, and then their replay is the evidence.
    ran = skipped[:1] + [{"name": name, "status": "IN_PROGRESS"} for name in JOBS]
    assert integrate._snapshot_requirement(member(integrate), "base", ran) == (True, "")
    near_miss = skipped + [{"__typename": "StatusContext", "context": JOBS[0], "name": JOBS[0]}]
    assert integrate._snapshot_requirement(member(integrate), "base", near_miss) == (False, "")


@pytest.mark.parametrize("mode,ci_scope", [("120000", True), ("160000", True), ("100644", False)])
def test_requirement_keeps_the_replay_ci_scope_would_run(integrate, monkeypatch, tmp_path, mode, ci_scope):
    # A symlink or gitlink under a docs name runs the Rust lanes in CI, and
    # a base without the shared scope verdict skips nothing.
    out, _ = _frozen_base(tmp_path, ["evals/README.md"], mode=mode, ci_scope=ci_scope)
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="merged-tree\n"))
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    assert integrate._snapshot_requirement(member(integrate), "base") == (True, "")


@pytest.mark.parametrize("scope", ["SUCCESS", "FAILURE", "CANCELLED"])
def test_code_pr_whose_rust_lanes_were_skipped_is_still_refused(integrate, scope):
    # A run cancelled after its scope (or a failed scope) reports the Rust job
    # unexpanded and SKIPPED, the way a docs-only skip does. For a head whose
    # paths need the replay that is no evidence, so it stays out.
    rollup = [{"name": "Scope", "status": "COMPLETED", "conclusion": scope, "workflowName": "Rust shim",
               "detailsUrl": "https://github.com/o/r/actions/runs/9/job/1"},
              {"name": "Rust / ${{ matrix.os }}", "status": "COMPLETED", "conclusion": "SKIPPED",
               "workflowName": "Rust shim", "detailsUrl": "https://github.com/o/r/actions/runs/9/job/2"}]
    state, why = integrate._snapshot_state(SHA, rollup, True)
    assert state == "pending"
    value = member(integrate, state)
    value.snapshot_why = why
    assert "own-head snapshot replay is pending" in integrate._not_ready(value)


def test_head_moves_during_requirement_read(integrate, monkeypatch):
    monkeypatch.setattr(integrate, "_has_snapshot_selector", lambda base: True)
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: "b" * 40)
    required, why = integrate._snapshot_requirement(member(integrate), "base")
    assert required is None
    assert "head moved" in why


def test_requirement_read_keeps_gh_cause(integrate, monkeypatch):
    monkeypatch.setattr(integrate, "_has_snapshot_selector", lambda base: True)
    def unavailable(number):
        raise integrate.Refusal("HTTP 404: pull head not found\nsecond line")
    monkeypatch.setattr(integrate, "_fetch_head", unavailable)
    required, why = integrate._snapshot_requirement(member(integrate), "base")
    assert required is None
    assert "HTTP 404: pull head not found" in why
    assert "second line" not in why


@pytest.mark.parametrize("conclusion,refuses", [("success", False), (None, True), ("failure", True)])
def test_open_rechecks_exact_checkpoint(integrate, actions, monkeypatch, conclusion, refuses):
    rollup, jobs, _, _ = actions
    jobs["1"]["steps"][1].update(conclusion=conclusion,
                                  status="completed" if conclusion else "in_progress")
    api = integrate.gh
    monkeypatch.setattr(integrate, "gh", lambda *args: json.dumps({
        "headRefOid": SHA, "statusCheckRollup": rollup}) if args[:2] == ("pr", "view") else api(*args))
    monkeypatch.setattr(integrate, "_checks_why", lambda checks: ("pending", ""))
    plan = integrate.Plan("integrate/2026-11-06-batch-1", "base", merged=[member(integrate)])
    if refuses:
        with pytest.raises(integrate.Refusal, match="snapshot|replay"):
            integrate._refuse_red_members(plan)
    else:
        integrate._refuse_red_members(plan)


def test_reported_workflow_names_stay_bound(integrate):
    workflow = yaml.safe_load((ROOT / ".github/workflows/rust.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["rust"]
    assert tuple(job["name"].replace("${{ matrix.os }}", os)
                 for os in job["strategy"]["matrix"]["os"]) == integrate.SNAPSHOT_JOBS == JOBS
    assert sum(step.get("name") == integrate.SNAPSHOT_STEP == STEP for step in job["steps"]) == 1


def test_ready_collects_checkpoint_before_admission(integrate, actions, monkeypatch):
    rollup, jobs, _, _ = actions
    jobs["1"]["steps"][1].update(status="in_progress", conclusion=None)
    api = integrate.gh
    row = {"number": 123, "title": "change", "headRefOid": SHA, "url": "https://github.example/pr/123",
           "statusCheckRollup": rollup}
    monkeypatch.setattr(integrate, "gh", lambda *args: json.dumps([row])
                        if args[:2] == ("pr", "list") else api(*args))
    monkeypatch.setattr(integrate, "_labeled_at", lambda *args: None)
    monkeypatch.setattr(integrate, "_snapshot_requirement", lambda *args: (True, ""))
    selected, = integrate.ready("ready-to-batch", "base")
    assert selected.snapshot_required is True
    assert selected.snapshot == "pending"
    assert "snapshot replay is pending" in integrate._not_ready(selected)


@pytest.mark.parametrize("old_plan,moved", [(False, True), (True, False), (True, True)])
def test_open_refuses_moved_required_head_or_pre_guard_plan(integrate, monkeypatch, old_plan, moved):
    value = member(integrate)
    value.snapshot_required = None if old_plan else True
    monkeypatch.setattr(integrate, "_has_snapshot_selector", lambda base: True)
    monkeypatch.setattr(integrate, "_base_has_snapshot_admission", lambda base: True)
    monkeypatch.setattr(integrate, "gh", lambda *args: json.dumps({
        "headRefOid": "b" * 40 if moved else SHA, "statusCheckRollup": []}))
    plan = integrate.Plan("integrate/2026-11-06-batch-1", "base", merged=[value])
    with pytest.raises(integrate.Refusal, match="rebuild"):
        integrate._refuse_red_members(plan)


def test_open_admits_no_row_member_without_checks(integrate, monkeypatch):
    value = member(integrate, "not required")
    value.snapshot_required = False
    monkeypatch.setattr(integrate, "gh", lambda *args: json.dumps({
        "headRefOid": SHA, "statusCheckRollup": []}))
    plan = integrate.Plan("integrate/2026-11-06-batch-1", "base", merged=[value])
    integrate._refuse_red_members(plan)


@pytest.mark.parametrize("source,aware", [
    ("# snapshot_required is not a field\nclass Member:\n    number: int\n", False),
    ("class Other:\n    snapshot_required: bool\nclass Member:\n    number: int\n", False),
    ("class Member:\n    snapshot_required: bool | None = None\n", True),
])
def test_base_builder_capability_uses_the_member_schema(integrate, monkeypatch, source, aware):
    def out(*args):
        assert args == ("show", "base:ops/integrate.py")
        return source
    monkeypatch.setattr(integrate, "_out", out)
    assert integrate._base_has_snapshot_admission("base") is aware


@pytest.mark.parametrize("phase,required,admitted", [
    ("success", True, True), (None, True, False), ("failure", True, False),
    ("wrong head", True, False), ("success", False, True), ("success", None, False),
])
def test_bootstrap_rechecks_admission_instead_of_bypassing_it(
        integrate, actions, monkeypatch, phase, required, admitted):
    rollup, jobs, _, calls = actions
    if phase == "wrong head":
        jobs["1"]["head_sha"] = "b" * 40
    else:
        jobs["1"]["steps"][1].update(conclusion=phase,
                                      status="completed" if phase else "in_progress")
    api = integrate.gh
    monkeypatch.setattr(integrate, "gh", lambda *args: json.dumps({
        "headRefOid": SHA, "statusCheckRollup": rollup}) if args[:2] == ("pr", "view") else api(*args))
    monkeypatch.setattr(integrate, "_has_snapshot_selector", lambda base: True)
    monkeypatch.setattr(integrate, "_base_has_snapshot_admission", lambda base: False)
    computed = []
    def requirement(value, base, rollup=None):
        computed.append((value.number, base))
        return required, "selector unavailable" if required is None else ""
    monkeypatch.setattr(integrate, "_snapshot_requirement", requirement)
    value = member(integrate)
    value.snapshot_required = None
    plan = integrate.Plan("integrate/2026-11-06-batch-1", "base", merged=[value])
    if admitted:
        integrate._refuse_red_members(plan)
        assert value.snapshot_required is required
        assert value.snapshot == ("satisfied" if required else "not required")
    else:
        with pytest.raises(integrate.Refusal):
            integrate._refuse_red_members(plan)
    assert computed == [(123, "base")]
    if required is False:
        assert not calls


def test_bootstrap_computes_each_member(integrate, actions, monkeypatch):
    rollup, _, _, _ = actions
    values = [member(integrate), member(integrate)]
    values[1].number, values[1].sha = 124, "c" * 40
    for value in values:
        value.snapshot_required = None
    api = integrate.gh
    def gh(*args):
        if args[:2] == ("pr", "view"):
            value = next(value for value in values if value.number == int(args[2]))
            return json.dumps({"headRefOid": value.sha, "statusCheckRollup": rollup})
        return api(*args)
    monkeypatch.setattr(integrate, "gh", gh)
    monkeypatch.setattr(integrate, "_has_snapshot_selector", lambda base: True)
    monkeypatch.setattr(integrate, "_base_has_snapshot_admission", lambda base: False)
    computed = []
    def requirement(value, base, rollup=None):
        computed.append(value.number)
        return value.number == 123, ""
    monkeypatch.setattr(integrate, "_snapshot_requirement", requirement)
    plan = integrate.Plan("integrate/2026-11-06-batch-1", "base", merged=values)
    integrate._refuse_red_members(plan)
    assert computed == [123, 124]
    assert [value.snapshot_required for value in values] == [True, False]


@pytest.mark.parametrize("remainder,master_value,budget_conflict", [
    ("README.md", "new\n", False), ("tests/test_feature.py", "new\n", False),
    ("README.md", "conflicting master edit\n", False), ("README.md", "old\n", True),
    ("README.md", "old\n", False), ("README.md", "old\n", "semantic"),
])
def test_already_landed_runtime_edit_does_not_stall_no_row_merge(
        integrate, monkeypatch, tmp_path, remainder, master_value, budget_conflict):
    def git(*args):
        result = subprocess.run(["git", *args], cwd=tmp_path, capture_output=True,
                                text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def write(path, value):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")

    git("init", "-q", "-b", "master")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    git("config", "commit.gpgsign", "false")
    # python_freeze.py: `build` refuses a master without the guard.
    for path in ("rust/ci/pr_precheck.py", "rust/ci/parity_scope.py", "rust/ci/python_freeze.py"):
        write(path, (ROOT / path).read_text(encoding="utf-8"))
    # The real member-merge path also validates the combined harness recipe.
    write("ops/integrate.py", 'RUST_HARNESS = ("rust/test_fixture.py",)\n')
    write(".github/workflows/rust.yml", json.dumps({"jobs": {
        "rust": {"steps": [{"run": "python -m pytest rust/test_fixture.py"}]},
        "parity-checks": {"steps": []}}}))
    write("rust/ci/harness-test-selections.json", '{"schema": 1, "files": {}}\n')
    write("rust/test_fixture.py", "def test_fixture(): pass\n")
    write("pseudolife_memory/doctor_cli.py", "old\n")
    write(remainder, "old\n")
    def budget(version):
        return json.dumps({"rust.yml": {"jobs": {"rust": {"minutes":
                          version + 1 if budget_conflict == "semantic" else 1}},
                          "fingerprints": {"rust": str(version)}}}) + "\n"
    if budget_conflict:
        write(integrate.CI_BUDGET_CODE, (ROOT / integrate.CI_BUDGET_CODE).read_text(encoding="utf-8"))
        write(integrate.CI_BUDGET, budget(0))
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "feature")
    write("pseudolife_memory/doctor_cli.py", "new\n")
    write(remainder, "new\n")
    if budget_conflict:
        write(integrate.CI_BUDGET, budget(1))
    git("add", ".")
    git("commit", "-qm", "feature")
    sha = git("rev-parse", "HEAD")
    git("checkout", "-q", "master")
    write("pseudolife_memory/doctor_cli.py", master_value)
    write("docs/master-note.md", "independent master edit\n")
    if budget_conflict:
        write(integrate.CI_BUDGET, budget(2))
    git("add", ".")
    git("commit", "-qm", "land runtime edit independently")
    base = git("rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: sha)
    value = member(integrate)
    value.sha = sha
    assert "pseudolife_memory/doctor_cli.py" in integrate._changed(sha, base)
    required, why = integrate._snapshot_requirement(value, base)
    if budget_conflict:
        assert required is False
        assert "budget" in why
        value.snapshot_required = required
        value.snapshot, value.snapshot_why = integrate._snapshot_state(sha, [], required)
        assert integrate._not_ready(value) is None
        assert integrate._merge_tree(base, sha) == (False, [integrate.CI_BUDGET])
        # Member cost evidence has independent tests. Exercise admission
        # through the actual Git merge and frozen budget normalizer here.
        monkeypatch.setattr(integrate, "_budget_member_evidence", lambda *args: [])
        # Its runtime edit is under pseudolife_memory/: since the Python
        # freeze only a member carrying the exception label is admitted.
        value.labels = ("freeze-exception",)
        plan = integrate.Plan("integrate/2026-11-06-batch-1", base)
        # The freeze pre-pass lists open pull requests: none here.
        monkeypatch.setattr(integrate, "gh", lambda *args, **kwargs: "[]")
        integrate._merge_members(plan, [value])
        if budget_conflict == "semantic":
            assert not plan.merged and len(plan.dropped) == 1
            assert "conflicts" in plan.dropped[0].reason
        else:
            assert plan.merged == [value] and not plan.dropped
            assert json.loads((tmp_path / integrate.CI_BUDGET).read_text()) == {
                "rust.yml": {"jobs": {"rust": {"minutes": 1}}}}
            assert (tmp_path / "pseudolife_memory/doctor_cli.py").read_text() == "new\n"
        return
    if master_value == "old\n":
        assert (required, why) == (True, "")
        assert integrate._snapshot_state(sha, [], required)[0] == "pending"
        assert integrate._merge_tree(base, sha)[0] is True
        return
    if master_value != "new\n":
        assert required is False
        assert "conflict handling" in why
        assert integrate._merge_tree(base, sha) == (False, ["pseudolife_memory/doctor_cli.py"])
        return
    assert (required, why) == (False, "")
    value.snapshot_required = required
    value.snapshot, value.snapshot_why = integrate._snapshot_state(sha, [], required)
    assert integrate._not_ready(value) is None
