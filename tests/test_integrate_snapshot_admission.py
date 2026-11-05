"""Batch admission reads the own-head replay checkpoint, not job log text."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

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
    jobs["1"]["steps"][0]["conclusion"] = prerequisite
    jobs["1"]["steps"][1]["conclusion"] = "skipped"
    assert integrate._snapshot_state(SHA, rollup, True)[0] == state


def test_api_failure_is_unverified(integrate, actions, monkeypatch):
    def unavailable(*args):
        raise integrate.Refusal("API unavailable")
    monkeypatch.setattr(integrate, "gh", unavailable)
    assert integrate._snapshot_state(SHA, actions[0], True)[0] == "unverified"


@pytest.mark.parametrize("files,required", [
    (["docs/guide.md"], False), (["tests/test_feature.py"], False),
    (["docs/guide.md", "tests/test_feature.py", "changelog.d/note.md"], False),
    (["pseudolife_memory/doctor_cli.py"], True), (["rust/shim/src/main.rs"], True),
    (["tests/test_runtime_seed.py"], True),
])
def test_requirement_uses_frozen_selector_without_importing_member_code(
        integrate, monkeypatch, tmp_path, files, required):
    scope = (ROOT / "rust/ci/parity_scope.py").read_text(encoding="utf-8")
    selector = (ROOT / "rust/ci/pr_precheck.py").read_text(encoding="utf-8")
    (tmp_path / "rust/ci").mkdir(parents=True)
    marker = tmp_path / "executed"
    (tmp_path / "rust/ci/pr_precheck.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('member code')\n")
    (tmp_path / "rust/probe.py").write_text("from tests import test_runtime_seed\n")

    def out(*args):
        if args[0] == "ls-tree":
            return "100644 blob selector"
        if args[0] == "rev-parse":
            return str(tmp_path)
        assert args[0] == "show"
        return selector if args[1].endswith("pr_precheck.py") else scope

    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    monkeypatch.setattr(integrate, "_changed", lambda sha, base: set(files))
    assert integrate._snapshot_requirement(member(integrate), "base") == (required, "")
    assert not marker.exists()


def test_head_moves_during_requirement_read(integrate, monkeypatch):
    monkeypatch.setattr(integrate, "_has_snapshot_selector", lambda base: True)
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: "b" * 40)
    required, why = integrate._snapshot_requirement(member(integrate), "base")
    assert required is None
    assert "head moved" in why


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
