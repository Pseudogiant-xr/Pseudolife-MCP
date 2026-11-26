"""The headroom check reads recorded runs and names every job near a bound.

Every run here is a recorded GitHub Actions response (trimmed), kept in
ci_headroom_fixtures.json: no test calls GitHub."""
import copy
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ci_budget
import ci_headroom
from ci_headroom import Bounds, Finding, Reading

FIXTURES = json.loads((Path(__file__).with_name("ci_headroom_fixtures.json")).read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)

# The two shapes of ops/ci_tests.sh's bound: one 50-minute bound for every
# selection (master before #2012), and one per selection (#2012).
ONE_BOUND = """\
frozen=()
if [[ "${PSEUDOLIFE_CI_INCLUDE_FROZEN:-}" == true ]]; then
    frozen=(--include-frozen)
fi
/usr/bin/time -v -o ci-results/process.txt \\
    timeout --signal=ABRT --kill-after=60s 50m \\
    python -m pytest -p ops.pytest_failure_lines ${frozen[@]+"${frozen[@]}"} -q -n 2
"""
TWO_BOUNDS = """\
frozen=()
bound=50m
if [[ "${PSEUDOLIFE_CI_INCLUDE_FROZEN:-}" == true ]]; then
    frozen=(--include-frozen)
    bound=72m
fi
/usr/bin/time -v -o ci-results/process.txt \\
    timeout --signal=ABRT --kill-after=60s "$bound" \\
    python -m pytest -p ops.pytest_failure_lines ${frozen[@]+"${frozen[@]}"} -q -n 2
"""

# The jobs of the recorded runs, with the bounds #2012 leaves on master.
CI = {"jobs": {
    "scope": {"runs-on": "ubuntu-latest", "timeout-minutes": 15, "steps": [{"run": "true"}]},
    "test": {"runs-on": "ubuntu-latest", "timeout-minutes": 87,
             "steps": [{"name": "Tests", "run": "bash ops/ci_tests.sh"}]},
    "test-lite-windows": {"runs-on": "windows-latest", "timeout-minutes": 40, "steps": [{"run": "true"}]},
}}
RUST = {"jobs": {
    "scope": {"name": "Scope", "timeout-minutes": 5, "steps": [{"run": "true"}]},
    "rust": {"name": "Rust / ${{ matrix.os }}",
             "strategy": {"matrix": {"os": ["ubuntu-latest", "windows-latest"]}},
             "timeout-minutes": "${{ github.event_name == 'pull_request' && needs.scope.outputs.full == 'false' && 100 || 75 }}",
             "steps": [{"run": "true"}]},
    "candidate": {"name": "Candidate / ${{ matrix.os }}",
                  "strategy": {"matrix": {"os": ["ubuntu-latest", "windows-latest"]}},
                  "timeout-minutes": 25, "steps": [{"run": "true"}]},
    "parity-checks": {"name": "Parity checks / ${{ matrix.os }} / ${{ matrix.suite }}",
                      "strategy": {"matrix": {"os": ["ubuntu-latest", "windows-latest"], "suite": ["cli"],
                                              "include": [{"suite": "cli", "job_timeout": 35}]}},
                      "timeout-minutes": "${{ matrix.job_timeout || 210 }}", "steps": [{"run": "true"}]},
}}


def bounds(script=TWO_BOUNDS, ci=CI, rust=RUST):
    return Bounds({"ci.yml": copy.deepcopy(ci), "rust.yml": copy.deepcopy(rust)}, script)


def runs(workflow=None):
    return [run for run in FIXTURES["runs"] if workflow in (None, run["workflow"])]


def recorded_diagnostics(run):
    """The diagnostics the recorded artifacts hold, as read_runs reads them."""
    artifacts = FIXTURES["artifacts"].get(str(run["id"]), [])

    def diagnostics(job):
        artifact = ci_headroom.artifact_for(job, artifacts)
        if artifact is None:
            return None
        members = FIXTURES["members"][str(artifact["id"])]
        return members["cpu.txt"], members["process.txt"]
    return diagnostics


def read(checked: Bounds) -> Reading:
    reading = Reading()
    for run in runs():
        ci_headroom.examine(checked, run["workflow"], run, run["jobs"], recorded_diagnostics(run), reading)
    return reading


def table(reading):
    return sorted((f.workflow, f.job, f.kind, f.selection, f.run_id, f.attempt, f.bound,
                   round(f.share * 100)) for f in reading.findings)


# --- the recorded runs, against both shapes of the bound ----------------------

def test_the_25_november_frozen_run_was_near_the_old_bound():
    # 49:18 of pytest against the 50 minutes it ran under: the near miss
    # nothing noticed before the 0.26.0 cut lost three attempts.
    pytest_rows = [row for row in table(read(bounds(ONE_BOUND))) if row[2] == "pytest"]
    assert pytest_rows == [
        ("ci.yml", "test", "pytest", "frozen-inclusive", 45512966117, 1, 50.0, 100),
        ("ci.yml", "test", "pytest", "frozen-inclusive", 45512966117, 2, 50.0, 99),
        ("ci.yml", "test", "pytest", "frozen-inclusive", 45747873168, 2, 50.0, 101),
        ("ci.yml", "test", "pytest", "frozen-inclusive", 45747873168, 3, 50.0, 100),
    ]


def test_the_frozen_runs_clear_the_72_minute_bound():
    found = read(bounds(TWO_BOUNDS))
    assert [row for row in table(found) if row[2] == "pytest" or row[1] == "test"] == []


def test_the_recorded_windows_near_misses_are_named():
    # Today's numbers with #2012 on master: the four Windows bounds the
    # 2026-11-12..26 survey found above 80% (test-lite-windows is listed
    # once although filter=all repeats it under each attempt).
    assert table(read(bounds(TWO_BOUNDS))) == [
        ("ci.yml", "test-lite-windows", "job", "", 45512966117, 1, 40.0, 87),
        ("ci.yml", "test-lite-windows", "job", "", 45747873168, 1, 40.0, 91),
        ("rust.yml", "Parity checks / windows-latest / cli", "job", "", 45533353153, 1, 35.0, 84),
        ("rust.yml", "Rust / windows-latest", "job", "", 45052267402, 1, 75.0, 96),
        ("rust.yml", "Rust / windows-latest", "job", "", 45106587399, 1, 100.0, 90),
    ]


def test_a_finding_names_its_run_cpu_and_share():
    found = [f for f in read(bounds(ONE_BOUND)).findings if f.kind == "pytest" and f.run_id == 45512966117
             and f.attempt == 2]
    assert len(found) == 1
    finding = found[0]
    assert finding.cpu == "AMD EPYC 7763 64-Core Processor"
    assert finding.job_id == 138890627461 and finding.minutes == pytest.approx(49.30, abs=0.01)
    assert not finding.killed and finding.note == ""
    body = ci_headroom.render([finding], NOW)
    assert "AMD EPYC 7763 64-Core Processor" in body and "**99%**" in body
    assert "actions/runs/45512966117/job/138890627461" in body


def test_a_killed_run_is_reported_as_a_lower_bound():
    killed = [f for f in read(bounds(ONE_BOUND)).findings if f.kind == "pytest" and f.killed]
    assert sorted((f.run_id, f.attempt) for f in killed) == [(45512966117, 1), (45747873168, 2), (45747873168, 3)]
    assert all(f.note.startswith("killed by the bound it ran under") for f in killed)


def test_a_job_that_never_reached_a_runner_is_not_measured():
    # Candidate / windows-latest of run 43507789403 sat 29 minutes in the
    # queue and was cancelled with no step run: its times are the queue's.
    candidate = [job for run in runs("rust.yml") for job in run["jobs"]
                 if job["name"] == "Candidate / windows-latest"]
    assert len(candidate) == 1 and candidate[0]["steps"] == []
    assert ci_headroom.minutes_between(candidate[0]["started_at"], candidate[0]["completed_at"]) > 25
    assert not any(f.job.startswith("Candidate") for f in read(bounds()).findings)


def test_each_attempt_reads_its_own_artifact():
    run = next(run for run in runs("ci.yml") if run["id"] == 45747873168)
    artifacts = FIXTURES["artifacts"]["45747873168"]
    tests = sorted((job for job in run["jobs"] if job["name"] == "test"), key=lambda job: job["run_attempt"])
    picked = [ci_headroom.artifact_for(job, artifacts)["id"] for job in tests]
    assert len(set(picked)) == 3
    cpus = [ci_headroom.parse_cpu(FIXTURES["members"][str(artifact)]["cpu.txt"]) for artifact in picked]
    assert cpus == ["INTEL(R) XEON(R) PLATINUM 8573C", "AMD EPYC 7763 64-Core Processor",
                    "AMD EPYC 7763 64-Core Processor"]


def test_two_artifacts_in_one_job_window_are_not_guessed_between():
    job = {"started_at": "2026-11-26T03:00:00Z", "completed_at": "2026-11-26T04:00:00Z"}
    one = {"name": "test-diagnostics", "created_at": "2026-11-26T03:59:00Z", "id": 1}
    assert ci_headroom.artifact_for(job, [one])["id"] == 1
    assert ci_headroom.artifact_for(job, [one, dict(one, id=2)]) is None
    assert ci_headroom.artifact_for(job, [dict(one, expired=True)]) is None
    assert ci_headroom.artifact_for(job, [dict(one, name="test-lite-windows-results")]) is None


# --- bounds -------------------------------------------------------------------

def test_the_rust_job_bound_follows_the_run():
    checked = bounds()
    ordinary = {"event": "pull_request", "head_branch": "t3/some-fix"}
    batch = {"event": "pull_request", "head_branch": "integrate/2026-11-26-batch-1"}
    for run, minutes in ((ordinary, 100.0), (batch, 75.0), ({"event": "push", "head_branch": "master"}, 75.0),
                         ({"event": "schedule", "head_branch": "master"}, 75.0)):
        assert checked.job("rust.yml", "Rust / windows-latest", run)[1:] == (minutes, ""), run


def test_a_matrix_timeout_takes_its_combination():
    assert bounds().job("rust.yml", "Parity checks / windows-latest / cli", {"event": "push"})[1:] == (35.0, "")


def test_an_unresolved_timeout_takes_its_smallest_number_and_says_so():
    rust = copy.deepcopy(RUST)
    rust["jobs"]["rust"]["timeout-minutes"] = "${{ needs.plan.outputs.slow == 'true' && 120 || 60 }}"
    assert bounds(rust=rust).job("rust.yml", "Rust / windows-latest", {"event": "push"})[1:] == (
        60.0, "smallest of 60/120 (unresolved)")


def test_a_name_from_an_unexpanded_matrix_is_placed_by_its_template():
    rust = copy.deepcopy(RUST)
    rust["jobs"]["stress"] = {"name": "Stress / ${{ inputs.os }} / ${{ matrix.iteration }}",
                              "strategy": {"matrix": {"iteration": "${{ fromJSON(needs.plan.outputs.loop) }}"}},
                              "timeout-minutes": 45, "steps": [{"run": "true"}]}
    assert bounds(rust=rust).job("rust.yml", "Stress / windows-latest / 7", {"event": "workflow_dispatch"})[1] == 45.0
    assert bounds(rust=rust).job("rust.yml", "Frozen judge probes / windows-latest", {"event": "push"}) is None


def test_a_job_without_a_timeout_has_githubs_default():
    ci = copy.deepcopy(CI)
    del ci["jobs"]["scope"]["timeout-minutes"]
    assert bounds(ci=ci).job("ci.yml", "scope", {"event": "push"})[1:] == (360.0, "GitHub's default")


def test_both_shapes_of_the_pytest_bound_are_read():
    assert ci_headroom.pytest_bounds(ONE_BOUND) == {"live": 50, "frozen-inclusive": 50}
    assert ci_headroom.pytest_bounds(TWO_BOUNDS) == {"live": 50, "frozen-inclusive": 72}
    with pytest.raises(ValueError):
        ci_headroom.pytest_bounds(TWO_BOUNDS.replace('"$bound"', "$BOUND"))


def test_the_checkout_bounds_are_readable():
    # ops/ci_tests.sh and every workflow job this checkout defines.
    checked = Bounds.from_checkout()
    assert set(checked.pytest) == set(ci_headroom.SELECTIONS)
    assert checked.pytest["live"] <= checked.pytest["frozen-inclusive"]
    assert any(shape.runs_ci_tests for shape in checked.shapes["ci.yml"])
    for workflow in ("ci.yml", "rust.yml"):
        for shape in checked.shapes[workflow]:
            for name, combo in shape.combos.items():
                for run in ({"event": "push"}, {"event": "pull_request", "head_branch": "t3/x"}):
                    bound, note = ci_headroom.job_bound(workflow, shape, combo, run)
                    assert bound is not None and bound > 0, (workflow, name, note)


# --- what an artifact holds ---------------------------------------------------

def test_process_records_give_selection_time_and_kill():
    killed = FIXTURES["members"]["15088089826"]["process.txt"]
    parsed = ci_headroom.parse_process(killed)
    assert parsed.selection == "frozen-inclusive" and parsed.killed and parsed.minutes == pytest.approx(50.47, abs=0.01)
    live = killed.replace(" --include-frozen", "").replace("Exit status: 124", "Exit status: 1")
    assert ci_headroom.parse_process(live).selection == "live" and not ci_headroom.parse_process(live).killed
    assert ci_headroom.parse_process("Command being timed: \"make\"\n") is None
    assert ci_headroom.parse_process("") is None


def test_the_cpu_name_is_reduced_to_plain_text():
    assert ci_headroom.parse_cpu("Model name:   AMD EPYC 7763 64-Core Processor\n") == "AMD EPYC 7763 64-Core Processor"
    assert ci_headroom.parse_cpu("Model name: Evil | [x](http://e) <b>\n") == "Evil  x(httpe) b"
    assert ci_headroom.parse_cpu("Model name: " + "A" * 300) == "A" * 80
    assert ci_headroom.parse_cpu("no model here") == "not recorded"


def test_only_the_two_members_are_read_each_under_its_cap():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("cpu.txt", "Model name: X\n")
        bundle.writestr("process.txt", "x" * (ci_headroom.MEMBER_BYTES + 1))
        bundle.writestr("junit.xml", "<testsuite/>")
    assert ci_headroom.read_members(buffer.getvalue()) == {"cpu.txt": "Model name: X\n"}


# --- reading runs through the API ---------------------------------------------

class Recorded:
    """Serves the recorded responses; counts what it was asked for."""

    def __init__(self, fork=False, issues=()):
        self.fork, self.issues, self.calls, self.writes = fork, [dict(issue) for issue in issues], [], []

    def pages(self, path, key):
        self.calls.append(path)
        if "/workflows/" in path:
            workflow = path.split("/workflows/")[1].split("/")[0]
            listed = copy.deepcopy(runs(workflow))
            if self.fork:
                for run in listed:
                    run["head_repository"] = {"full_name": "someone/fork"}
            return listed
        if path.endswith("&per_page=100") and "/jobs?" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return next(run["jobs"] for run in runs() if run["id"] == run_id)
        if "/artifacts?" in path:
            return FIXTURES["artifacts"][path.split("/runs/")[1].split("/")[0]]
        if "/issues?" in path:
            return [issue for issue in self.issues if issue["state"] == "open"]
        raise AssertionError(path)

    def bytes(self, path):
        self.calls.append(path)
        members = FIXTURES["members"][path.split("/artifacts/")[1].split("/")[0]]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name, text in members.items():
                bundle.writestr(name, text)
        return buffer.getvalue()

    def json(self, path, *, method="GET", fields=None):
        self.writes.append((method, path, fields))
        if method == "POST" and path.endswith("/issues"):
            issue = {"number": 900 + len(self.issues), "title": fields["title"], "body": fields["body"],
                     "state": "open", "user": {"login": ci_headroom.AUTHOR}}
            self.issues.append(issue)
            return issue
        number = int(path.split("/issues/")[1].split("/")[0])
        issue = next(issue for issue in self.issues if issue["number"] == number)
        if method == "PATCH":
            issue.update(fields)
        return {}


def test_read_runs_reads_the_recorded_runs_and_their_artifacts():
    github = Recorded()
    reading = ci_headroom.read_runs(github, bounds(ONE_BOUND), NOW)
    assert table(reading) == table(read(bounds(ONE_BOUND)))
    downloads = [call for call in github.calls if call.endswith("/zip")]
    assert len(downloads) == 5   # one per `test` attempt in the two ci.yml runs


def test_an_oversized_artifact_is_not_downloaded(monkeypatch):
    large = copy.deepcopy(FIXTURES)
    for listed in large["artifacts"].values():
        for artifact in listed:
            artifact["size_in_bytes"] = ci_headroom.ARTIFACT_BYTES + 1
    monkeypatch.setitem(globals(), "FIXTURES", large)
    github = Recorded()
    reading = ci_headroom.read_runs(github, bounds(ONE_BOUND), NOW)
    assert not any(call.endswith("/zip") for call in github.calls)
    assert [f for f in reading.findings if f.kind == "pytest"] == []


def test_a_first_attempt_too_short_to_near_a_bound_is_not_listed():
    # Run 45533353153 took about an hour; the smallest rust.yml bound in
    # these fixtures is Scope's 5 minutes, so 3 minutes is under 80% of it.
    checked = bounds()
    run = dict(next(run for run in runs("rust.yml") if run["id"] == 45533353153),
               run_started_at="2026-11-25T02:29:52Z")
    assert ci_headroom.shortest_bound(checked, "rust.yml", run) == 5.0
    assert ci_headroom.too_short(checked, "rust.yml", dict(run, updated_at="2026-11-25T02:32:51Z"))
    assert not ci_headroom.too_short(checked, "rust.yml", dict(run, updated_at="2026-11-25T02:33:53Z"))
    # A later attempt's times leave out the earlier attempts' jobs.
    assert not ci_headroom.too_short(checked, "rust.yml", dict(run, updated_at="2026-11-25T02:30:00Z", run_attempt=2))
    assert not ci_headroom.too_short(checked, "rust.yml", {"run_attempt": 1, "event": "push"})


def test_a_short_run_costs_no_jobs_call(monkeypatch):
    short = copy.deepcopy(FIXTURES)
    for run in short["runs"]:
        run.update(run_started_at=run["created_at"], updated_at=run["created_at"], run_attempt=1)
    monkeypatch.setitem(globals(), "FIXTURES", short)
    github = Recorded()
    reading = ci_headroom.read_runs(github, bounds(), datetime(2026, 11, 1, tzinfo=timezone.utc), NOW)
    assert reading.findings == [] and reading.short == len(runs())
    assert not any("/jobs?" in call for call in github.calls)


def test_an_unreadable_artifact_costs_its_reading_not_the_pass(monkeypatch):
    class Broken(Recorded):
        def bytes(self, path):
            raise ci_headroom.GitHubError("gh api repos/x failed: HTTP 410: artifact expired")
    reading = ci_headroom.read_runs(Broken(), bounds(ONE_BOUND), NOW)
    assert [f for f in reading.findings if f.kind == "pytest"] == []
    assert len(reading.unreadable) == 5
    assert "HTTP 410: artifact expired" in ci_headroom.summary(reading)
    assert table(reading) == [row for row in table(read(bounds(ONE_BOUND))) if row[2] == "job"]


def test_a_failed_gh_call_names_gh_s_own_reason(monkeypatch):
    class Done:
        returncode, stdout, stderr = 1, b"", b"HTTP 403: API rate limit exceeded for installation\n"
    monkeypatch.setattr(ci_headroom.subprocess, "run", lambda *args, **kwargs: Done())
    with pytest.raises(ci_headroom.GitHubError, match="API rate limit exceeded"):
        ci_headroom.GitHub().pages("repos/x/actions/runs", "workflow_runs")


def test_the_issue_lists_at_most_its_worst_bounds():
    finding = current()[0]
    many = [Finding(**{**finding.__dict__, "job": f"job {n:02d}", "minutes": finding.bound * (0.8 + n / 200)})
            for n in range(ci_headroom.MAX_BOUNDS + 3)]
    rust = copy.deepcopy(RUST)
    for n in range(ci_headroom.MAX_BOUNDS + 3):
        rust["jobs"][f"j{n}"] = {"name": f"job {n:02d}", "timeout-minutes": finding.bound, "steps": [{"run": "true"}]}
    ci = copy.deepcopy(CI)
    ci["jobs"].update(rust["jobs"])
    kept = ci_headroom.merge(bounds(ci=ci), [], many, NOW)
    assert [f.job for f in kept] == [f"job {n:02d}" for n in range(ci_headroom.MAX_BOUNDS + 2, 2, -1)]
    assert len(ci_headroom.render(kept, NOW)) < 65_536


def test_a_rerun_of_an_older_run_is_read():
    # A rerun keeps its run's creation time: the listing covers the whole
    # retention window, one day per query, and keeps what was active since.
    class Listing:
        def __init__(self):
            self.paths = []

        def pages(self, path, key):
            self.paths.append(path)
            return [{"id": 1, "updated_at": "2026-11-26T08:00:00Z"},     # rerun today, created long ago
                    {"id": 2, "updated_at": "2026-11-20T08:00:00Z"},     # nothing since
                    {"id": 3}]
    listing = Listing()
    since = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    found = ci_headroom.recent_runs(listing, "ci.yml", since, NOW)
    assert sorted(run["id"] for run in found) == [1, 3]
    days = [path.split("created=")[1].split("&")[0] for path in listing.paths]
    assert days[0] == "2026-11-12" and days[-1] == "2026-11-26" and len(days) == ci_headroom.RETENTION_DAYS + 1


def test_a_bound_keeps_its_newest_near_miss():
    # Five older, worse runs must not push out the newest one: it decides
    # how long the bound stays listed.
    finding = current()[0]
    older = [Finding(**{**finding.__dict__, "run_id": n, "minutes": finding.bound * 0.95,
                        "completed_at": f"2026-11-2{n}T00:00:00Z"}) for n in range(5)]
    newest = Finding(**{**finding.__dict__, "run_id": 99, "minutes": finding.bound * 0.85,
                        "completed_at": "2026-11-26T00:00:00Z"})
    kept = ci_headroom.merge(bounds(), [], [*older, newest], NOW)
    assert newest in kept and len(kept) == ci_headroom.PER_BOUND + 1
    # Once the older ones age out the newest still holds the bound listed.
    later = datetime(2026, 12, 8, 12, 0, tzinfo=timezone.utc)
    assert ci_headroom.merge(bounds(), kept, [], later) == [newest]


def test_sync_reads_at_most_its_days(monkeypatch):
    class Offline:
        def __init__(self):
            raise AssertionError("a refused sync reached GitHub")
    monkeypatch.setattr(ci_headroom, "GitHub", Offline)
    with pytest.raises(SystemExit) as refused:
        ci_headroom.main(["sync", "--days", str(ci_headroom.SYNC_DAYS + 1)])
    assert refused.value.code == 2


def test_a_forks_runs_are_never_read():
    # A fork's run carries its own workflow's job names and its branch's
    # artifacts: none of it reaches the issue.
    github = Recorded(fork=True)
    reading = ci_headroom.read_runs(github, bounds(ONE_BOUND), NOW)
    assert not any("/jobs?" in call or "/artifacts" in call for call in github.calls)
    assert reading.findings == [] and reading.forks == len(runs())


def test_a_rendered_cell_carries_no_markup():
    finding = Finding(**{**current()[0].__dict__, "job": "Rust / [x](https://e.example) @someone `y` <b>|",
                         "cpu": "AMD EPYC 7763 64-Core Processor"})
    row = next(line for line in ci_headroom.render([finding], NOW).splitlines() if "Rust / " in line)
    assert "Rust / ?x?(https://e.example) ?someone ?y? ?b??" in row
    for character in "[]@`<>":
        assert character not in row.split("|")[2], character


# --- the one issue ------------------------------------------------------------

def current(script=TWO_BOUNDS):
    return read(bounds(script)).findings


def test_findings_open_one_issue_and_a_rerun_changes_nothing():
    github = Recorded()
    assert ci_headroom.sync(github, bounds(), current(), NOW) == "opened #900 with 5 findings"
    (issue,) = github.issues
    assert issue["title"] == ci_headroom.TITLE
    assert ci_headroom.parse_state(issue["body"]) == ci_headroom.merge(bounds(), [], current(), NOW)
    assert ci_headroom.sync(github, bounds(), current(), NOW) == "#900 unchanged (5 findings)"
    assert len(github.writes) == 1


def test_an_earlier_finding_stays_until_its_bound_is_raised():
    github = Recorded()
    ci_headroom.sync(github, bounds(), current(), NOW)
    # The next day reads no new near miss: the issue keeps what it had.
    assert ci_headroom.sync(github, bounds(), [], NOW) == "#900 unchanged (5 findings)"
    raised = copy.deepcopy(CI)
    raised["jobs"]["test-lite-windows"]["timeout-minutes"] = 51
    assert ci_headroom.sync(github, bounds(ci=raised), [], NOW) == "updated #900 with 3 findings"
    assert not any(f.job == "test-lite-windows" for f in ci_headroom.parse_state(github.issues[0]["body"]))


def test_the_issue_closes_when_every_finding_aged_out():
    github = Recorded()
    ci_headroom.sync(github, bounds(), current(), NOW)
    later = datetime(2026, 12, 11, 9, 0, tzinfo=timezone.utc)
    assert ci_headroom.sync(github, bounds(), [], later) == "closed #900"
    assert github.issues[0]["state"] == "closed"
    assert ci_headroom.sync(github, bounds(), [], later) == "no finding; no issue open"


def test_an_issue_someone_else_opened_under_the_title_is_left_alone():
    other = {"number": 5, "title": ci_headroom.TITLE, "body": "", "state": "open", "user": {"login": "someone"}}
    github = Recorded(issues=[other])
    assert ci_headroom.sync(github, bounds(), current(), NOW) == "opened #901 with 5 findings"
    assert github.issues[0]["body"] == ""


def test_each_bound_keeps_its_worst_runs():
    finding = current()[0]
    many = [Finding(**{**finding.__dict__, "run_id": finding.run_id + n, "minutes": finding.bound * (0.81 + n / 100)})
            for n in range(9)]
    kept = ci_headroom.merge(bounds(), [], many, NOW)
    assert len(kept) == ci_headroom.PER_BOUND
    assert [round(f.share, 2) for f in kept] == [0.89, 0.88, 0.87, 0.86, 0.85]


@pytest.mark.parametrize("body", [
    "", "no state", "<!-- ci-headroom-state {not json} -->",
    '<!-- ci-headroom-state {"findings": [{"workflow": "ci.yml"}]} -->',
    '<!-- ci-headroom-state {"findings": "x"} -->',
    '<!-- ci-headroom-state {"findings": 5} -->',
    '<!-- ci-headroom-state {"findings": null} -->',
    '<!-- ci-headroom-state [1, 2] -->',
])
def test_a_malformed_state_is_ignored(body):
    assert ci_headroom.parse_state(body) == []


def test_a_state_field_of_the_wrong_type_drops_that_finding():
    good = ci_headroom.merge(bounds(), [], current(), NOW)
    state = json.loads(ci_headroom.STATE.search(ci_headroom.render(good, NOW)).group(1))
    state["findings"][0]["killed"] = "false"
    state["findings"][1]["minutes"] = -1
    # A time without a zone would fail merge()'s comparison every day.
    state["findings"][2]["completed_at"] = "2026-11-26"
    body = f"<!-- ci-headroom-state {json.dumps(state)} -->"
    assert ci_headroom.parse_state(body) == good[3:]
    ci_headroom.merge(bounds(), ci_headroom.parse_state(body), [], NOW)


def test_a_job_name_cannot_end_the_state_comment():
    finding = Finding(**{**current()[0].__dict__, "job": "x --> y | z"})
    body = ci_headroom.render([finding], NOW)
    assert ci_headroom.parse_state(body) == [finding]
    assert body.split("<!-- ci-headroom-state", 1)[1].count("-->") == 1


def test_the_report_names_what_no_workflow_defines_now():
    reading = Reading()
    run = dict(runs("rust.yml")[0], workflow="rust.yml")
    job = dict(run["jobs"][0], name="Frozen judge probes (report only) / windows-latest")
    ci_headroom.examine(bounds(), "rust.yml", run, [job], lambda job: None, reading)
    assert reading.unplaced == {"rust.yml: Frozen judge probes (report only) / windows-latest": 1}
    assert "renamed or removed" in ci_headroom.summary(reading)


def test_days_is_bounded_by_the_retention(monkeypatch):
    class Offline:
        def __init__(self):
            raise AssertionError("an out-of-range --days reached GitHub")
    monkeypatch.setattr(ci_headroom, "GitHub", Offline)
    for days in ("15", "0"):
        with pytest.raises(SystemExit) as refused:
            ci_headroom.main(["report", "--days", days])
        assert refused.value.code == 2


def test_two_templates_matching_one_name_place_it_nowhere():
    rust = copy.deepcopy(RUST)
    for job_id in ("one", "two"):
        rust["jobs"][job_id] = {"name": "Probe / ${{ inputs.os }}", "timeout-minutes": 5, "steps": [{"run": "true"}]}
    assert bounds(rust=rust).job("rust.yml", "Probe / windows-latest", {"event": "push"}) is None


def test_the_scheduled_workflow_alone_holds_issue_write():
    import yaml
    workflows = ci_headroom.WORKFLOWS
    headroom = yaml.safe_load((workflows / "ci-headroom.yml").read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as True.
    assert set(headroom[True]) == {"schedule", "workflow_dispatch"}
    assert headroom["permissions"] == {"contents": "read"}
    (job,) = headroom["jobs"].values()
    assert job["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    assert any("rust/ci/ci_headroom.py sync" in str(step.get("run", "")) for step in job["steps"])
    assert "ci-headroom.yml" not in ci_budget.pull_request_workflows()
    for path in sorted(workflows.glob("*.y*ml")):
        if path.name != "ci-headroom.yml":
            assert "issues: write" not in path.read_text(encoding="utf-8"), path.name


def test_the_threshold_is_about_80_percent():
    assert ci_headroom.THRESHOLD == 0.8 and ci_budget.REPOSITORY == ci_headroom.REPOSITORY
