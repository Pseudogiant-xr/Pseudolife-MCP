"""ops/batch_timeline.py: where an integration batch's time goes.

The report is a pure function over a collected artifact, so most cases
build one by hand. The #892 case is transcribed from GitHub's real
responses for that batch (2026-10-21), trimmed to the fields the report
reads; the collect cases replace the module's `gh()` with a fake, so no
test touches the network.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "batch_timeline_under_test", ROOT / "ops" / "batch_timeline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


timeline = _load()

HEAD_892 = "85fd4676f6ebc106ca9b244797e1c9d228789092"
CUTOFF_890 = "2026-10-21T06:45:04Z"   # when #890 (batch-only heavy CI) merged


def job(name, created, started, completed, conclusion="success", steps=5,
        annotations=()):
    return {"name": name, "created_at": f"2026-10-21T{created}Z",
            "started_at": f"2026-10-21T{started}Z",
            "completed_at": f"2026-10-21T{completed}Z", "conclusion": conclusion,
            "steps": steps, "annotations": list(annotations)}


NOT_ACQUIRED = ("The job was not acquired by Runner of type hosted even after "
                "multiple attempts")

BODY_892 = f"""Integration batch of 2 reviewed pull requests, built by `python ops/integrate.py build` from `master` at `0c6ed19e4dc6a14a585f89922ec3403309539a4a`.

## Members

| PR | Head merged | Review evidence | CI on its head |
| --- | --- | --- | --- |
| #888 chore(deps): bump certifi | `3168e364b969ba55f9e240ebad1296dc3237ec7e` | https://github.com/o/r/pull/888 (none) | green |
| #891 fix(console): close the vite alerts | `5cddf0ec32cb009e102445636ab970700b66eac0` | https://github.com/o/r/pull/891 (none) | pending |

## Validation

- Local full suite: passed at `{HEAD_892}` on local-wsl: 12710 passed, 84 skipped, 68 warnings in 576.41s (0:09:36)
- Rust harness Python tests: 830 passed at 85fd4676 (stated by the operator, not checked)
"""


def batch_892() -> dict:
    return {
        "number": 892, "title": "integrate: 2026-10-21 batch 3 (#888, #891)",
        "state": "MERGED", "createdAt": "2026-10-21T08:11:03Z",
        "mergedAt": "2026-10-21T09:27:13Z", "closedAt": "2026-10-21T09:27:14Z",
        "headRefName": "integrate/2026-10-21-batch-3", "headRefOid": HEAD_892,
        "body": BODY_892,
        "commits": [
            {"oid": "3168e364b969ba55f9e240ebad1296dc3237ec7e",
             "committedDate": "2026-10-21T06:42:23Z", "messageHeadline": "chore(deps): bump"},
            {"oid": "5cddf0ec32cb009e102445636ab970700b66eac0",
             "committedDate": "2026-10-21T07:01:48Z", "messageHeadline": "test: re-record"},
            {"oid": "a9f1c9e49d00000000000000000000000000000a",
             "committedDate": "2026-10-21T08:00:20Z",
             "messageHeadline": "Merge #888 into integrate/2026-10-21-batch-3: chore"},
            {"oid": HEAD_892, "committedDate": "2026-10-21T08:00:22Z",
             "messageHeadline": "Merge #891 into integrate/2026-10-21-batch-3: fix"},
        ],
        "runs": [
            {"id": 39567362205, "name": "CI", "event": "pull_request",
             "created_at": "2026-10-21T08:11:06Z", "run_attempt": 2, "attempts": [
                 {"attempt": 1, "run_started_at": "2026-10-21T08:11:06Z",
                  "conclusion": "failure", "jobs": [
                      job("scope", "08:11:07", "08:11:09", "08:11:20"),
                      job("guards", "08:11:21", "08:11:21", "08:11:20", "skipped", 0),
                      job("test", "08:11:20", "08:11:23", "08:43:21"),
                      job("test-lite-macos", "08:11:20", "08:11:20", "08:26:21",
                          "cancelled", 0, [NOT_ACQUIRED]),
                  ]},
                 {"attempt": 2, "run_started_at": "2026-10-21T09:12:18Z",
                  "conclusion": "success", "jobs": [
                      # Retained from attempt 1: created at the rerun, started
                      # an hour earlier. Not re-executed, so not waited for.
                      job("scope", "09:12:20", "08:11:09", "08:11:20"),
                      job("test", "09:12:20", "08:11:23", "08:43:21"),
                      job("guards", "09:12:20", "09:12:20", "08:11:20", "skipped", 0),
                      job("test-lite-macos", "09:12:20", "09:13:30", "09:25:38"),
                  ]},
             ]},
            {"id": 39567362101, "name": "Rust shim", "event": "pull_request",
             "created_at": "2026-10-21T08:11:06Z", "run_attempt": 1, "attempts": [
                 {"attempt": 1, "run_started_at": "2026-10-21T08:11:06Z",
                  "conclusion": "success", "jobs": [
                      job("Scope", "08:11:07", "08:11:09", "08:11:23"),
                      job("Rust / windows-latest", "08:11:23", "08:11:26", "08:30:13"),
                      job("Rust / ubuntu-latest", "08:11:23", "08:11:58", "08:20:59"),
                      job("Stress report", "08:21:24", "08:21:24", "08:21:23", "skipped", 0),
                      job("Parity checks / ubuntu-latest / cli",
                          "08:30:13", "08:30:15", "08:41:51"),
                      job("Parity checks / windows-latest / w1c-connection",
                          "08:30:13", "08:40:34", "09:05:55"),
                      job("Parity / windows-latest", "09:05:55", "09:07:53", "09:07:57"),
                      job("Parity / ubuntu-latest", "09:05:55", "09:09:45", "09:09:55"),
                  ]},
             ]},
            # Code scanning's dynamic run is not part of the batch's CI.
            {"id": 39567360035, "name": "PR #892", "event": "dynamic",
             "created_at": "2026-10-21T08:11:05Z", "run_attempt": 1, "attempts": [
                 {"attempt": 1, "run_started_at": "2026-10-21T08:11:05Z",
                  "conclusion": "success", "jobs": [
                      job("Analyze", "08:11:06", "08:11:10", "10:59:59"),
                  ]},
             ]},
        ],
    }


def members_892() -> dict:
    return {
        "888": {"number": 888, "title": "chore(deps): bump certifi", "state": "MERGED",
                "createdAt": "2026-10-21T02:10:03Z", "mergedAt": "2026-10-21T09:27:16Z",
                "events": [
                    {"event": "labeled", "at": "2026-10-21T02:10:05Z", "label": "dependencies"},
                    {"event": "head_ref_force_pushed", "at": "2026-10-21T06:42:25Z"},
                    {"event": "labeled", "at": "2026-10-21T07:59:41Z",
                     "label": "ready-to-batch"},
                ]},
        "891": {"number": 891, "title": "fix(console): close the vite alerts",
                "state": "MERGED", "createdAt": "2026-10-21T07:05:52Z",
                "mergedAt": "2026-10-21T09:27:15Z",
                "events": [
                    {"event": "committed", "at": "2026-10-21T06:54:05Z"},
                    {"event": "committed", "at": "2026-10-21T07:01:48Z"},
                    {"event": "labeled", "at": "2026-10-21T07:59:44Z",
                     "label": "ready-to-batch"},
                ]},
    }


def artifact(*batches, members=None, direct=()) -> dict:
    return {"schema": 1, "repository": "o/r", "label": "ready-to-batch",
            "since": "2026-10-19T00:00:00Z", "collected_at": "2026-10-23T00:00:00Z",
            "batches": list(batches), "members": members or {},
            "direct_merges": list(direct)}


def report_892(**kwargs):
    return timeline.report(artifact(batch_892(), members=members_892()), **kwargs)


def test_batch_phases_split_the_892_timeline():
    b = report_892()["batches"][0]
    p = b["phases"]
    assert p["build"] == 2                      # 08:00:20 -> 08:00:22
    assert p["local_validation"] == 641         # head commit -> batch PR opened
    assert p["suite"] == pytest.approx(576.41)  # from the body's suite line
    assert p["ci_first_pass"] == 3532           # PR opened -> last attempt-1 job
    assert p["failure_repair"] == 943           # first pass done -> rerun green
    assert p["awaiting_merge"] == 95            # last check green -> merged
    assert p["batch_wall"] == 4570              # PR opened -> merged


def test_critical_path_splits_runner_wait_from_execution():
    p = report_892()["batches"][0]["phases"]
    assert b_path(report_892()) == [
        "Rust shim: Scope", "Rust shim: Rust / windows-latest",
        "Rust shim: Parity checks / windows-latest / w1c-connection",
        "Rust shim: Parity / ubuntu-latest"]
    assert p["ci_runner_wait"] == 856       # 2 + 3 + 621 + 230
    assert p["ci_execution"] == 2672        # 14 + 1127 + 1521 + 10
    assert p["ci_orchestration"] == 4       # PR -> run 3, run -> first job 1
    assert p["ci_runner_wait"] + p["ci_execution"] + p["ci_orchestration"] \
        == p["ci_first_pass"]


def b_path(rep):
    return rep["batches"][0]["critical_path"]


def test_rerun_is_flagged_with_its_cause_and_click_latency():
    (rerun,) = report_892()["batches"][0]["reruns"]
    assert rerun["run"] == "CI" and rerun["attempt"] == 2
    assert rerun["failed_jobs"] == [{"name": "test-lite-macos", "conclusion": "cancelled",
                                     "required": False, "never_started": True,
                                     "annotations": [NOT_ACQUIRED]}]
    assert rerun["wait"] == 1737           # attempt 1 done 08:43:21 -> rerun 09:12:18
    assert rerun["execution"] == 800       # 09:12:18 -> 09:25:38
    assert rerun["runner_wait"] == 70      # only the re-executed job waited


def test_retained_and_skipped_jobs_never_count_as_waiting():
    b = report_892()["batches"][0]
    # The retained `test` job (created 09:12:20, started 08:11:23) would add
    # an hour of negative wait if counted; skipped jobs end before they start.
    waits = {j["name"]: j["wait"] for j in b["jobs"] if j["attempt"] == 2}
    assert waits == {"CI: test-lite-macos": 70}


def test_a_job_no_runner_acquired_counts_as_waiting_not_executing():
    b = report_892()["batches"][0]
    (macos,) = [j for j in b["jobs"] if j["name"] == "CI: test-lite-macos"
                and j["attempt"] == 1]
    assert macos["wait"] == 901 and macos["execution"] == 0


def test_dynamic_runs_are_not_part_of_the_batch_ci():
    b = report_892()["batches"][0]
    assert all(not j["name"].startswith("PR #892") for j in b["jobs"])
    assert b["phases"]["ci_first_pass"] == 3532   # not 10:59:59


def test_member_admission_build_wait_and_lead():
    rep = report_892()
    m = {r["number"]: r for r in rep["members"]}
    assert m[888]["admission"] == 20978         # opened 02:10:03 -> labelled 07:59:41
    assert m[888]["revising"] == 16342          # opened -> last push 06:42:25
    assert m[888]["after_last_push"] == 4636
    assert m[888]["awaiting_build"] == 39       # labelled -> first merge 08:00:20
    assert m[888]["lead"] == 5252               # labelled -> batch merged 09:27:13
    assert m[891]["revising"] == 0              # last commit predates opening
    assert m[891]["after_last_push"] == 3232
    assert m[891]["outcome"] == "merged" and m[891]["batch"] == 892


def test_a_member_never_labelled_has_no_admission_split():
    # #850's members predate the label: unknown, not zero.
    members = members_892()
    members["888"]["events"] = [e for e in members["888"]["events"]
                                if e.get("label") != "ready-to-batch"]
    m = {r["number"]: r for r in timeline.report(
        artifact(batch_892(), members=members))["members"]}
    assert m[888]["admission"] is None and m[888]["revising"] is None
    assert m[888]["after_last_push"] is None and m[888]["lead"] is None


def test_member_whose_ci_was_pending_at_build_is_flagged():
    b = report_892()["batches"][0]
    assert b["pending_at_build"] == [891]


def test_regime_follows_the_cutoff_and_the_summary_states_n():
    rep = report_892(cutoff=CUTOFF_890)
    assert rep["batches"][0]["regime"] == "after"
    after = rep["summary"]["after"]
    assert after["batches"] == 1 and after["members"] == 2
    assert after["phases"]["failure_repair"] == {"n": 1, "median": 943, "max": 943,
                                                 "total": 943}
    assert rep["summary"]["before"]["batches"] == 0


def _closed_then_rebuilt():
    failed = copy.deepcopy(batch_892())
    failed.update(number=865, state="CLOSED", mergedAt=None,
                  createdAt="2026-10-21T08:00:00Z", closedAt="2026-10-21T08:05:00Z",
                  headRefName="integrate/2026-10-21-batch-2",
                  headRefOid="1" * 40, runs=[])
    failed["commits"][2]["committedDate"] = "2026-10-21T07:59:50Z"
    failed["commits"][3].update(oid="1" * 40, committedDate="2026-10-21T07:59:52Z")
    return failed


def test_a_closed_batch_is_repair_time_for_the_members_it_carried():
    failed = _closed_then_rebuilt()
    rep = timeline.report(artifact(failed, batch_892(), members=members_892()))
    closed = next(b for b in rep["batches"] if b["number"] == 865)
    assert closed["outcome"] == "closed" and closed["replaced_by"] == 892
    m = {r["number"]: r for r in rep["members"]}
    # labelled 07:59:41 -> first build 07:59:50; first build -> landing build 08:00:20
    assert m[888]["awaiting_build"] == 9
    assert m[888]["rebuild_repair"] == 30
    assert m[888]["batches"] == [865, 892]


def test_dropped_members_are_listed_with_their_reason():
    b = batch_892()
    b["body"] = b["body"].replace("## Validation", (
        "## Dropped\n\n- #877: conflicts with #891 in frontend/package.json; once "
        "that merges, merge master forward\n\n## Validation"))
    rep = timeline.report(artifact(b, members=members_892()))
    assert rep["batches"][0]["dropped"] == [
        {"number": 877, "reason": "conflicts with #891 in frontend/package.json; once "
                                  "that merges, merge master forward"}]


def test_a_broken_job_chain_reports_no_critical_path_rather_than_a_guess():
    b = batch_892()
    shim = b["runs"][1]["attempts"][0]["jobs"]
    # The final gate now starts long after anything finished: no predecessor.
    shim[-1].update(created_at="2026-10-21T09:08:30Z", started_at="2026-10-21T09:09:45Z")
    p = timeline.report(artifact(b, members=members_892()))["batches"][0]
    assert p["critical_path"] is None
    assert "no job" in p["critical_path_note"]
    assert p["phases"]["ci_runner_wait"] is None
    assert p["phases"]["ci_first_pass"] == 3532


def test_zero_length_jobs_ending_together_cannot_loop_the_walk():
    # Two instant gate jobs created and finished in the same second each
    # "finish when the other was queued"; the walk must only step back to
    # a job queued earlier, or it never ends (seen on the 2026-10-19 batches).
    b = batch_892()
    shim = b["runs"][1]["attempts"][0]["jobs"]
    shim[-2].update(created_at="2026-10-21T09:09:55Z", started_at="2026-10-21T09:09:55Z",
                    completed_at="2026-10-21T09:09:55Z")
    shim[-1].update(created_at="2026-10-21T09:09:55Z", started_at="2026-10-21T09:09:55Z",
                    completed_at="2026-10-21T09:09:55Z")
    p = timeline.report(artifact(b, members=members_892()))["batches"][0]
    assert p["critical_path"] is None or len(p["critical_path"]) == len(
        set(p["critical_path"]))


def test_a_hand_built_batch_lists_members_from_its_own_table():
    # #850 predates ops/integrate.py: a "| #N | Change | Lane |" table, no
    # head column and no "Merge #N into" commits.
    b = batch_892()
    b["body"] = ("## What is in it\n\n| PR | Change | Lane |\n|---|---|---|\n"
                 "| #888 | bump certifi | ops |\n| #891 | vite alerts | Console |\n")
    for c in b["commits"]:
        c["messageHeadline"] = "wip"
    rep = timeline.report(artifact(b, members=members_892()))
    assert rep["batches"][0]["members"] == [888, 891]
    assert rep["batches"][0]["phases"]["build"] is None


def test_a_rerun_cause_quotes_the_failure_before_any_warning():
    b = batch_892()
    macos = b["runs"][0]["attempts"][0]["jobs"][3]
    macos["annotations"] = [
        {"level": "warning", "message": "Node.js 20 is deprecated."},
        {"level": "failure", "message": NOT_ACQUIRED}]
    (rerun,) = timeline.report(artifact(b, members=members_892()))["batches"][0]["reruns"]
    assert rerun["failed_jobs"][0]["annotations"][0] == NOT_ACQUIRED


def test_direct_merges_in_the_window_are_listed_apart_from_members():
    rep = timeline.report(artifact(batch_892(), members=members_892(), direct=[
        {"number": 893, "title": "ci(integrate): merge guard",
         "mergedAt": "2026-10-21T09:29:26Z", "headRefName": "ci/integrate-merge-guard"},
        {"number": 888, "title": "chore(deps)", "mergedAt": "2026-10-21T09:27:16Z",
         "headRefName": "dependabot/pip"},
    ]))
    assert [d["number"] for d in rep["direct_merges"]] == [893]


def test_markdown_names_n_and_every_phase():
    text = timeline.render(report_892(cutoff=CUTOFF_890))
    assert "#892" in text and "n=1" in text
    for phase in ("awaiting admission", "awaiting build", "local validation",
                  "runner wait", "execution", "failure repair", "awaiting merge"):
        assert phase in text


EVIDENCE = ROOT / "evals" / "results" / "batch-lifecycle-20261023"


def test_committed_artifact_backs_the_findings():
    """Every number the findings note and rust/CUTOVER.md cite, recomputed
    from the committed artifact, so a report change that moves one fails."""
    art = json.loads((EVIDENCE / "artifact.json").read_text(encoding="utf-8"))
    rep = timeline.report(art, cutoff=CUTOFF_890)
    before, after = rep["summary"]["before"], rep["summary"]["after"]
    d = timeline._d
    assert (before["batches"], before["closed"], after["batches"]) == (7, 1, 1)
    assert before["phases"]["admission"]["n"] == 21
    assert d(before["phases"]["admission"]["median"]) == "7h47m"
    assert d(before["phases"]["admission"]["max"]) == "29h54m"
    assert d(before["phases"]["after_last_push"]["median"]) == "4h38m"
    assert d(before["phases"]["awaiting_build"]["median"]) == "1m08s"
    assert d(before["phases"]["lead"]["median"]) == "2h35m"
    assert d(before["phases"]["lead"]["max"]) == "6h23m"
    assert d(before["phases"]["ci_first_pass"]["median"]) == "1h17m"
    assert d(before["phases"]["ci_first_pass"]["max"]) == "2h55m"
    assert d(before["phases"]["ci_runner_wait"]["median"]) == "15m07s"
    assert d(before["phases"]["local_validation"]["median"]) == "10m09s"
    assert d(before["phases"]["suite"]["median"]) == "8m01s"
    assert d(before["phases"]["awaiting_merge"]["max"]) == "1h09m"
    assert d(before["phases"]["failure_repair"]["max"]) == "25m21s"
    merged = [b for b in rep["batches"] if b["outcome"] == "merged"]
    assert len(merged) == 8
    assert all(any("Parity checks / windows-latest" in step for step in b["critical_path"])
               for b in merged)
    wait = sum(b["phases"]["ci_runner_wait"] for b in merged)
    execution = sum(b["phases"]["ci_execution"] for b in merged)
    assert (d(execution), d(wait)) == ("6h47m", "3h54m")
    assert round(100 * wait / (wait + execution)) == 37
    b = {x["number"]: x for x in rep["batches"]}
    assert d(b[868]["phases"]["ci_runner_wait"]) == "2h23m"
    assert b[865]["replaced_by"] == 866
    assert d(b[892]["phases"]["batch_wall"]) == "1h16m"
    assert d(b[892]["phases"]["failure_repair"]) == "15m43s"
    (rerun,) = b[892]["reruns"]
    assert (d(rerun["wait"]), d(rerun["execution"])) == ("28m57s", "13m20s")
    assert not rerun["failed_jobs"][0]["required"]
    assert [drop["number"] for x in rep["batches"] for drop in x["dropped"]] == [884]
    m = {x["number"]: x for x in rep["members"]}
    assert d(m[842]["rebuild_repair"]) == "1h47m"
    assert len(rep["direct_merges"]) == 11


class FakeGh:
    """Answers collect's gh calls from canned responses and records them."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        b = batch_892()
        self.batch = b
        self.members = members_892()

    def __call__(self, *args: str, input: str | None = None) -> str:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["pr", "list"] and "merged" in args:
            return json.dumps([{"number": 893, "title": "ci: guard",
                                "mergedAt": "2026-10-21T09:29:26Z",
                                "headRefName": "ci/integrate-merge-guard",
                                "author": {"login": "someone"}}])
        if args[:2] == ["pr", "list"]:
            return json.dumps([
                {"number": 892, "headRefName": self.batch["headRefName"],
                 "isCrossRepository": False, "createdAt": self.batch["createdAt"]},
                {"number": 999, "headRefName": "integrate/2026-10-21-batch-9",
                 "isCrossRepository": True, "createdAt": self.batch["createdAt"]},
                {"number": 893, "headRefName": "ci/integrate-merge-guard",
                 "isCrossRepository": False, "createdAt": self.batch["createdAt"]},
            ])
        if args[:2] == ["pr", "view"] and args[2] == "892":
            view = {k: v for k, v in self.batch.items() if k != "runs"}
            view["commits"] = [dict(c, authors=[{"email": "a@example.com",
                                                 "login": "someone"}])
                               for c in view["commits"]]
            return json.dumps(view)
        if args[:2] == ["pr", "view"]:
            m = self.members[args[2]]
            return json.dumps({k: v for k, v in m.items() if k != "events"})
        path = next(a for a in args if a.startswith("repos/"))
        if "/timeline" in path:
            number = path.split("/issues/")[1].split("/")[0]
            return "\n".join(json.dumps(e) for e in self.members[number]["events"])
        if "actions/runs?head_sha=" in path:
            assert HEAD_892 in path
            return "\n".join(json.dumps({k: v for k, v in r.items() if k != "attempts"})
                             for r in self.batch["runs"])
        if "/attempts/" in path and path.endswith("/jobs?per_page=100"):
            run_id, attempt = path.split("/runs/")[1].split("/attempts/")
            run = next(r for r in self.batch["runs"] if str(r["id"]) == run_id)
            jobs = run["attempts"][int(attempt.split("/")[0]) - 1]["jobs"]
            return "\n".join(json.dumps(dict({k: v for k, v in j.items()
                                              if k != "annotations"}, id=i))
                             for i, j in enumerate(jobs))
        if "/attempts/" in path:
            run_id, attempt = path.split("/runs/")[1].split("/attempts/")
            run = next(r for r in self.batch["runs"] if str(r["id"]) == run_id)
            a = run["attempts"][int(attempt) - 1]
            return json.dumps({"run_started_at": a["run_started_at"],
                               "conclusion": a["conclusion"]})
        if "/annotations" in path:
            return (json.dumps({"level": "warning", "message": "Node.js 20 is deprecated."})
                    + "\n" + json.dumps({"level": "failure", "message": NOT_ACQUIRED}) + "\n")
        raise AssertionError(f"unexpected gh call {args}")


def test_collect_records_what_report_reads_and_no_personal_fields(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(timeline, "gh", fake)
    art = timeline.collect("2026-10-19T00:00:00Z")
    assert [b["number"] for b in art["batches"]] == [892]   # forks and others skipped
    assert sorted(art["members"]) == ["888", "891"]
    assert [d["number"] for d in art["direct_merges"]] == [893]
    text = json.dumps(art)
    assert "email" not in text and "login" not in text and "authors" not in text
    rep = timeline.report(art)
    assert rep["batches"][0]["phases"]["ci_first_pass"] == 3532
    assert rep["batches"][0]["reruns"][0]["failed_jobs"][0]["annotations"] == [
        NOT_ACQUIRED, "Node.js 20 is deprecated."]
    # Only failed or cancelled jobs have their annotations fetched.
    assert sum("/annotations" in " ".join(c) for c in fake.calls) == 1


def test_collect_never_writes_to_github(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(timeline, "gh", fake)
    timeline.collect("2026-10-19T00:00:00Z")
    for args in fake.calls:
        assert args[:2] not in (["pr", "create"], ["pr", "edit"], ["pr", "comment"],
                                ["pr", "merge"])
        assert "-X" not in args and "--method" not in args


def test_cli_report_reads_a_saved_artifact(tmp_path, capsys):
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(artifact(batch_892(), members=members_892())),
                    encoding="utf-8")
    out = tmp_path / "report.json"
    assert timeline.main(["report", str(path), "--cutoff", CUTOFF_890,
                          "--json", str(out)]) == 0
    assert "#892" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["after"]["batches"] == 1
