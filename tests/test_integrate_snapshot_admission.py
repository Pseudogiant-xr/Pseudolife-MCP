"""Batch admission reads the own-head replay checkpoint, not job log text."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import re
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
    if ci_scope == "without predicate":     # a base from before pull_request_skips_rust
        base["rust/ci/docs_scope.py"] = base["rust/ci/docs_scope.py"].replace(
            "def pull_request_skips_rust(", "def _renamed_skips_rust(")
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
    ([], False),
    (["docs/guide.md"], False), (["tests/test_feature.py"], False),
    (["docs/guide.md", "tests/test_feature.py", "changelog.d/note.md"], False),
    (["docs/guide.md", "rust/producer-census.json"], False),
    (["rust/CUTOVER.md", "rust/producer-census.json"], False),
    (["rust/producer-census.json"], True),
    (["docs/guide.md", "rust/producer-census.json.bak"], True),
    (["docs/guide.md", "rust/producer-census.json", "pseudolife_memory/doctor_cli.py"], True),
    (["pseudolife_memory/doctor_cli.py"], True), (["rust/shim/src/main.rs"], True),
    (["tests/test_runtime_seed.py"], True),
    # Markdown under evals/ waits on the head's own scope verdict (below);
    # prompts and code beside it still need the replay.
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


def _rust_jobs(scope, rust):
    """(name, status, conclusion) of one attempt's Scope and Rust jobs."""
    listed = [("Scope", *scope)]
    if rust == "unexpanded":
        listed.append(("Rust / ${{ matrix.os }}", "completed", "skipped"))
    elif rust == "expanded":
        listed += [(name, "in_progress", None) for name in JOBS]
    return listed


def _rust_run(monkeypatch, integrate, *, run_id=9, event="pull_request", head=SHA,
              scope=("completed", "success"), run=("completed", "success"), rust="unexpanded",
              attempt=1, latest=None, extra=()):
    """A rollup of one Rust workflow run on the head, with the Actions job,
    run and attempt-jobs metadata `gh api` returns for it. `latest` is the
    job list of the run's latest attempt when a rerun differs from the
    rollup's; `extra` adds other runs as (run id, event, created_at, rust)."""
    jobs, runs, attempts, rollup = {}, {}, {}, []

    def add_run(rid, ev, sha, created, run_state, listed, attempt_number, latest_listed):
        runs[str(rid)] = {"id": rid, "head_sha": sha, "event": ev, "path": ".github/workflows/rust.yml",
                          "run_attempt": attempt_number, "status": run_state[0], "conclusion": run_state[1],
                          "created_at": created}
        for offset, (name, status, conclusion) in enumerate(listed, 1):
            job_id = rid * 10 + offset
            rollup.append({"name": name, "workflowName": "Rust shim", "status": status.upper(),
                           "conclusion": (conclusion or "").upper() or None, "detailsUrl":
                           f"https://github.com/o/r/actions/runs/{rid}/job/{job_id}"})
            jobs[str(job_id)] = {"name": name, "head_sha": sha, "run_id": rid, "run_attempt": 1,
                                "status": status, "conclusion": conclusion, "steps": [],
                                "started_at": "2026-11-26T01:00:00Z", "completed_at": None}
        attempts[(str(rid), str(attempt_number))] = [
            {"name": name, "status": status, "conclusion": conclusion}
            for name, status, conclusion in latest_listed]

    listed = _rust_jobs(scope, rust)
    add_run(run_id, event, head, "2026-11-26T01:00:00Z", run, listed, attempt,
            listed if latest is None else latest)
    for rid, ev, created, other_rust in extra:
        other = _rust_jobs(("completed", "success"), other_rust)
        add_run(rid, ev, head, created, ("completed", "success"), other, 1, other)

    def gh(*args):
        assert args[0] == "api", args
        found = re.search(r"/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100&page=(\d+)$", args[1])
        if found:
            rid, number, page = found.groups()
            listed = attempts[(rid, number)] if page == "1" else []
            return json.dumps({"total_count": len(attempts[(rid, number)]), "jobs": listed})
        kind, ident = args[1].rsplit("/", 2)[-2:]
        return json.dumps(jobs[ident] if kind == "jobs" else runs[ident])

    monkeypatch.setattr(integrate, "gh", gh)
    return rollup


@pytest.fixture
def evals_member(integrate, monkeypatch, tmp_path):
    out, _ = _frozen_base(tmp_path, ["evals/README.md"])
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="merged-tree\n"))
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    return lambda rollup: integrate._snapshot_requirement(member(integrate), "base", rollup)


@pytest.mark.parametrize("files", [["evals/README.md"], ["evals/RUST-DRIVERS.md", "llms-full.txt"]])
def test_requirement_agrees_with_the_ci_scope_verdict(integrate, monkeypatch, tmp_path, files):
    # #1992, #2010: the scope job skipped the Rust lanes of Markdown under
    # evals/ through the predicate admission now reads, so once the head's
    # own run shows that skip, no replay is demanded.
    from rust.ci import docs_scope

    assert docs_scope.pull_request_skips_rust(files, True) is True
    out, _ = _frozen_base(tmp_path, files)
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="merged-tree\n"))
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    rollup = _rust_run(monkeypatch, integrate)
    value = member(integrate)
    assert integrate._snapshot_requirement(value, "base", rollup) == (False, "")
    assert value.snapshot_ci_skip is True     # so `open` reads the run again
    value.snapshot_required = False
    value.snapshot, value.snapshot_why = integrate._snapshot_state(SHA, rollup, False)
    assert integrate._not_ready(value) is None


RERUN = ("run cancelled", "scope failed", "rust job absent")


@pytest.mark.parametrize("case", ["no run yet", "scope running", "run still going", *RERUN,
                                  "other head", "push event", "rerun in progress", "other workflow"])
def test_a_docs_lane_skip_needs_the_heads_own_finished_run(integrate, monkeypatch, evals_member, case):
    # The docs lane skips the Rust lanes only when its test selection works,
    # so until the head's own pull-request run shows the skip, admission waits.
    kwargs = {"scope running": {"scope": ("in_progress", None), "run": ("in_progress", None),
                                "rust": "absent"},
              "run still going": {"run": ("in_progress", None)},
              "run cancelled": {"run": ("completed", "cancelled")},
              "scope failed": {"scope": ("completed", "failure")},
              "rust job absent": {"rust": "absent"},
              "other head": {"head": "b" * 40}, "push event": {"event": "push"},
              "rerun in progress": {"run": ("in_progress", None), "attempt": 2,
                                    "latest": [("Scope", "queued", None)]}}.get(case, {})
    rollup = [] if case == "no run yet" else _rust_run(monkeypatch, integrate, **kwargs)
    if case == "other workflow":
        api = integrate.gh
        monkeypatch.setattr(integrate, "gh", lambda *args: json.dumps(
            json.loads(api(*args)) | {"path": ".github/workflows/ci.yml"}) if "/runs/" in args[1] else api(*args))
    required, why = evals_member(rollup)
    assert required is None
    assert ("rerun the Rust workflow" if case in RERUN
            else "retry build (or open, if open reported this) once it has") in why
    value = member(integrate)
    value.snapshot_required = None
    value.snapshot, value.snapshot_why = integrate._snapshot_state(SHA, rollup, None)
    value.snapshot_why = why
    assert integrate._not_ready(value) == why


def test_a_docs_lane_skip_whose_run_ran_the_rust_lanes_needs_their_replay(integrate, monkeypatch, evals_member):
    # The scope job's docs test selection failed: it ran the Rust lanes for
    # these paths, so their replay is the evidence, as for any runtime diff.
    assert evals_member(_rust_run(monkeypatch, integrate, rust="expanded")) == (True, "")
    failed_scope = _rust_run(monkeypatch, integrate, scope=("completed", "failure"),
                             run=("in_progress", None), rust="expanded")
    assert evals_member(failed_scope) == (True, "")


def test_the_latest_attempt_of_the_run_decides(integrate, monkeypatch, evals_member):
    # The rollup can still show an earlier attempt's jobs; a rerun's own job
    # list decides, whichever way it went.
    latest_skipped = _rust_run(monkeypatch, integrate, rust="expanded", attempt=2,
                               latest=_rust_jobs(("completed", "success"), "unexpanded"))
    assert evals_member(latest_skipped) == (False, "")
    latest_ran = _rust_run(monkeypatch, integrate, attempt=2,
                           latest=_rust_jobs(("completed", "success"), "expanded"))
    assert evals_member(latest_ran) == (True, "")


def test_the_newest_pull_request_run_decides(integrate, monkeypatch, evals_member):
    # A reopened PR can carry two pull-request runs on one head.
    older_ran = _rust_run(monkeypatch, integrate, extra=[(8, "pull_request", "2026-11-26T00:00:00Z", "expanded")])
    assert evals_member(older_ran) == (False, "")
    newer_ran = _rust_run(monkeypatch, integrate, extra=[(10, "pull_request", "2026-11-26T02:00:00Z", "expanded")])
    assert evals_member(newer_ran) == (True, "")


def test_another_runs_rust_jobs_do_not_undo_the_heads_skip(integrate, monkeypatch, evals_member):
    # A manual dispatch on the same head expands the job names too; only the
    # head's own pull-request run decides (its replay would never come).
    rollup = _rust_run(monkeypatch, integrate, extra=[(12, "workflow_dispatch", "2026-11-26T03:00:00Z", "expanded")])
    assert evals_member(rollup) == (False, "")
    contexts = [{"__typename": "StatusContext", "context": "Scope", "name": "Scope", "state": "SUCCESS"},
                {"name": "Scope", "status": "COMPLETED", "conclusion": "SUCCESS"}]   # no Actions job
    assert evals_member(contexts)[0] is None


@pytest.mark.parametrize("since_build,refusal", [
    pytest.param("still skipped", None, id="still skipped"),
    pytest.param("rerun ran the lanes", "pending", id="rerun ran the lanes"),
    # Inside `open` the batch is built, so the retry must name open too.
    pytest.param("rerun going", r"has not shown .*retry build \(or open, if open reported this\) once it has",
                 id="rerun going"),
    pytest.param("unreadable", r"could not be read \(HTTP 502: bad gateway\); "
                 r"retry build \(or open, if open reported this\)", id="unreadable"),
    pytest.param("head moved", "head moved since snapshot admission", id="head moved"),
])
def test_open_reads_a_ci_scope_skip_again(integrate, monkeypatch, since_build, refusal):
    # `build` admitted the member on its run's skip; a rerun before `open`
    # may have run the Rust lanes after all, and then their replay counts.
    kwargs = {"rerun ran the lanes": {"attempt": 2, "latest": _rust_jobs(("completed", "success"), "expanded")},
              "rerun going": {"run": ("in_progress", None), "attempt": 2,
                              "latest": [("Scope", "in_progress", None)]}}.get(since_build, {})
    rollup = _rust_run(monkeypatch, integrate, **kwargs)
    api = integrate.gh
    if since_build == "unreadable":
        def api(*args, _api=api):
            if "/attempts/" in args[1]:
                raise integrate.Refusal("HTTP 502: bad gateway")
            return _api(*args)
    head = "b" * 40 if since_build == "head moved" else SHA
    monkeypatch.setattr(integrate, "gh", lambda *args: json.dumps({"headRefOid": head, "statusCheckRollup": rollup})
                        if args[:2] == ("pr", "view") else api(*args))
    monkeypatch.setattr(integrate, "_checks_why", lambda checks: ("pending", ""))
    value = member(integrate)
    value.snapshot_required, value.snapshot_ci_skip = False, True
    plan = integrate.Plan("integrate/2026-11-26-batch-1", "base", merged=[value])
    if refusal is None:
        integrate._refuse_red_members(plan)
        assert value.snapshot == "not required"
    else:
        with pytest.raises(integrate.Refusal, match=refusal):
            integrate._refuse_red_members(plan)


def test_unreadable_scope_metadata_waits_and_names_the_cause(integrate, monkeypatch, evals_member):
    rollup = _rust_run(monkeypatch, integrate)
    def unavailable(*args):
        raise integrate.Refusal("HTTP 502: bad gateway\nsecond line")
    monkeypatch.setattr(integrate, "gh", unavailable)
    required, why = evals_member(rollup)
    assert required is None
    assert "scope job metadata could not be read (HTTP 502: bad gateway)" in why and "retry build" in why


def _paged_jobs(monkeypatch, integrate, jobs, *, total=None, per_page=100):
    """`gh api` serving one attempt's job list `per_page` at a time; returns
    the pages requested."""
    requested = []

    def gh(*args):
        found = re.search(r"/runs/9/attempts/2/jobs\?per_page=100&page=(\d+)$", args[1])
        assert found, args
        page = int(found.group(1))
        requested.append(page)
        return json.dumps({"total_count": len(jobs) if total is None else total,
                           "jobs": jobs[(page - 1) * per_page:page * per_page]})

    monkeypatch.setattr(integrate, "gh", gh)
    return requested


def test_an_attempts_job_list_is_read_across_pages(integrate, monkeypatch):
    # A matrix-heavy run lists more than one page of jobs; the snapshot job
    # can sit on a later page, and missing it would waive a required replay.
    jobs = [{"name": f"Parity / shard {n}", "status": "completed", "conclusion": "success"}
            for n in range(150)] + [{"name": "Rust / ubuntu-latest", "status": "in_progress", "conclusion": None}]
    requested = _paged_jobs(monkeypatch, integrate, jobs)
    assert integrate._attempt_jobs(9, 2) == jobs
    assert requested == [1, 2]


def test_a_job_list_that_ends_short_of_its_count_is_unread(integrate, monkeypatch):
    # GitHub reported 151 jobs but the pages ran out at 100: a verdict from
    # the partial list could miss the snapshot job, so it is not given.
    jobs = [{"name": f"Parity / shard {n}", "status": "completed", "conclusion": "success"} for n in range(100)]
    _paged_jobs(monkeypatch, integrate, jobs, total=151)
    with pytest.raises(ValueError, match="the jobs of run 9 attempt 2 ended at 100 of 151"):
        integrate._attempt_jobs(9, 2)


@pytest.mark.parametrize("raw", [
    pytest.param(":100644 100644 0000 1111 M\0evals/README.md", id="no-final-nul"),
    pytest.param(":100644 100644 0000 1111 M\0evals/README.md\0extra\0", id="odd-records"),
    pytest.param("100644 100644 0000 1111 M\0evals/README.md\0", id="no-colon"),
])
def test_a_malformed_net_diff_is_unread_not_guessed(integrate, monkeypatch, raw):
    monkeypatch.setattr(integrate, "_out", lambda *args: raw)
    with pytest.raises(ValueError, match="raw Git diff"):
        integrate._net_diff("base", "tree")


def test_a_selector_verdict_of_the_wrong_shape_is_unread(integrate, monkeypatch, tmp_path):
    out, _ = _frozen_base(tmp_path, ["evals/README.md"])
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="merged-tree\n"))
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    monkeypatch.setattr(integrate, "_run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="[1, 0, 0]\n", stderr=""))
    required, why = integrate._snapshot_requirement(member(integrate), "base", [])
    assert required is None and "three booleans" in why


def test_an_always_skipped_diff_needs_no_actions_metadata(integrate, monkeypatch, tmp_path):
    # Docs beside the generated census skip the Rust lanes whatever the docs
    # lane does, so admission reads no run, as before.
    out, _ = _frozen_base(tmp_path, ["docs/guide.md", "rust/producer-census.json"])
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="merged-tree\n"))
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    monkeypatch.setattr(integrate, "gh", lambda *args: pytest.fail(f"unexpected gh call {args}"))
    assert integrate._snapshot_requirement(member(integrate), "base", []) == (False, "")


@pytest.mark.parametrize("mode,ci_scope", [("120000", True), ("160000", True), ("100644", False),
                                           ("100644", "without predicate")])
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


@pytest.mark.parametrize("files", [["rust/shim/src/main.rs"], ["evals/README.md", "evals/harness.py"]])
def test_a_skipped_rust_job_does_not_waive_a_code_prs_replay(integrate, monkeypatch, tmp_path, files):
    # Even a run that looks exactly like a docs skip waives nothing for a
    # diff the scope job's predicate does not skip.
    out, _ = _frozen_base(tmp_path, files)
    monkeypatch.setattr(integrate, "_out", out)
    monkeypatch.setattr(integrate, "git", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="merged-tree\n"))
    monkeypatch.setattr(integrate, "_fetch_head", lambda number: SHA)
    rollup = _rust_run(monkeypatch, integrate)
    assert integrate._snapshot_requirement(member(integrate), "base", rollup) == (True, "")


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
    # The docs-lane skip is read from these two job names: a rename would
    # leave such a PR waiting for evidence that never comes.
    assert job["name"] == integrate.UNEXPANDED_RUST_JOB
    assert workflow["jobs"]["scope"]["name"] == integrate.SCOPE_JOB


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
