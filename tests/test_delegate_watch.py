"""ops/delegate_watch.py: wake a delegate session only at decision points.

The watcher polls GitHub, the suite results directory and the suite leases,
and exits with one line per event; a quiet run exits after its timeout. These
tests drive it with a fake command runner, clock and sleep, so nothing here
touches the network or the board.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("delegate_watch", ROOT / "ops" / "delegate_watch.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def run_(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "workflowName": "CI"}


def pr(number, head="a" * 40, checks=(), mergeable="MERGEABLE", state="CLEAN", title="t"):
    return {"number": number, "title": title, "headRefOid": head, "mergeable": mergeable,
            "mergeStateStatus": state, "isDraft": False, "statusCheckRollup": list(checks)}


class World:
    """Canned answers for gh and the lease CLI, changed between polls."""

    def __init__(self, tmp_path):
        self.prs = []
        self.runs = []
        self.leases = {"full-suite": 0, "full-suite@box": 0}
        self.board_up = True
        self.gh_timeout = False
        self.fail_view = False
        self.pr_states = {}
        self.fail_gh = False
        self.calls = []
        self.suites = tmp_path / "suite-results"
        self.suites.mkdir()
        # The shim's digest with no mail: a watermark line and an empty body.
        (tmp_path / "digest.txt").write_text("0\n", encoding="utf-8")

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "gh":
            if self.gh_timeout:
                raise watch.subprocess.TimeoutExpired(cmd, 120)
            if self.fail_gh:
                return 1, "", "HTTP 502"
            if cmd[1:3] == ["pr", "list"]:
                return 0, json.dumps(self.prs), ""
            if cmd[1:3] == ["run", "list"]:
                return 0, json.dumps(self.runs), ""
            if cmd[1:3] == ["pr", "view"]:
                if self.fail_view:
                    return 1, "", "HTTP 502"
                return 0, json.dumps({"state": self.pr_states.get(cmd[3], "MERGED")}), ""
        if cmd[1:4] == ["lease", "check", "--json"]:
            held = self.leases[cmd[4]] == 1
            body = {"name": cmd[4], "held": held, "local": {"state": "free"},
                    "board": {"available": self.board_up}}
            return int(held), json.dumps(body), ""
        raise AssertionError(cmd)

    def suite(self, name, **fields):
        body = {"machine": "remote", "commit": "c" * 40, "exit_code": 0,
                "summary": "18548 passed, 81 skipped in 1907.05s"}
        body.update(fields)
        (self.suites / name).write_text(json.dumps(body), encoding="utf-8")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def main(world, clock, tmp_path, capsys, *extra, between=None):
    """Run the watcher; ``between(poll)`` changes the world after each poll."""
    polls = {"n": 0}

    def sleep(seconds):
        polls["n"] += 1
        if between:
            between(polls["n"])
        clock.sleep(seconds)

    code = watch.main(["--state", str(tmp_path / "state.json"), "--suite-results", str(world.suites),
                       "--digest", str(tmp_path / "digest.txt"), *extra],
                      runner=world, clock=clock, sleep=sleep)
    out = capsys.readouterr()
    return code, out.out, out.err


# ── classification ─────────────────────────────────────────────────────────

def test_checks_state():
    assert watch.checks_state([]) == ("none", [])
    assert watch.checks_state([run_("a"), run_("b", conclusion="SKIPPED")]) == ("green", [])
    assert watch.checks_state([run_("a"), run_("b", status="IN_PROGRESS", conclusion="")]) == ("pending", [])
    assert watch.checks_state([run_("a", conclusion="FAILURE"), run_("b", status="QUEUED", conclusion="")]) \
        == ("failing", ["a"])
    status = {"__typename": "StatusContext", "context": "ci/x", "state": "ERROR"}
    assert watch.checks_state([run_("a"), status]) == ("failing", ["ci/x"])


# ── the loop ───────────────────────────────────────────────────────────────

def test_first_run_takes_a_baseline_then_times_out_quietly(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(1, checks=[run_("a")])]          # already green: not news
    world.suite("old.json")
    code, out, err = main(world, clock, tmp_path, capsys, "--timeout", "300", "--interval", "60")
    assert code == watch.EXIT_QUIET
    assert out.startswith("quiet 5m:") and "1 open PR" in out
    assert "baseline" in err
    assert json.loads((tmp_path / "state.json").read_text())["prs"]["1"]["checks"] == "green"


def test_events_are_batched_through_the_settle_window_and_reported_once(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(7, checks=[run_("a", status="IN_PROGRESS", conclusion="")])]
    main(world, clock, tmp_path, capsys, "--timeout", "60")           # baseline

    def between(poll):
        if poll == 1:
            world.prs = [pr(7, checks=[run_("a")])]                   # CI finishes green
        if poll == 2:
            world.suite("new.json", machine="local-wsl", exit_code=1, summary="1 failed")
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "3300", "--settle", "90",
                        between=between)
    assert code == watch.EXIT_EVENTS
    lines = out.strip().splitlines()
    assert lines[0] == "PR #7 green at aaaaaaaa, merge state clean"
    assert lines[1] == "suite local-wsl cccccccc exit 1: 1 failed"
    assert lines[-1] == "mail: none pending"
    assert clock.t < 300                                              # did not wait out the timeout
    # Re-armed with nothing new: no repeat.
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "120")
    assert code == watch.EXIT_QUIET


def test_pull_request_transitions(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(1, checks=[run_("a", status="QUEUED", conclusion="")]), pr(2), pr(3)]
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.prs = [pr(1, checks=[run_("lint", conclusion="FAILURE")]),
                 pr(2, mergeable="CONFLICTING", state="DIRTY"),
                 pr(4, title="feat(x): new thing")]
    world.pr_states = {"3": "CLOSED"}
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert code == watch.EXIT_EVENTS
    assert out.splitlines()[:4] == ["PR #1 failing at aaaaaaaa: lint",
                                    "PR #2 conflicts with its base",
                                    "PR #4 opened: feat(x): new thing",
                                    "PR #3 closed: t"]


def test_a_new_head_reports_its_result_again(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(5, checks=[run_("a")])]
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.prs = [pr(5, head="b" * 40, checks=[run_("a", status="QUEUED", conclusion="")])]
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.prs = [pr(5, head="b" * 40, checks=[run_("a")])]
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0] == "PR #5 green at bbbbbbbb, merge state clean"


def test_master_runs_failing_and_recovering(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.runs = [{"databaseId": 1, "workflowName": "CI", "status": "completed", "conclusion": "success",
                   "headSha": "d" * 40, "event": "push", "attempt": 1}]
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.runs = [{"databaseId": 3, "workflowName": "CI", "status": "in_progress", "conclusion": "",
                   "headSha": "f" * 40, "event": "push"},
                  {"databaseId": 2, "workflowName": "CI", "status": "completed", "conclusion": "failure",
                   "headSha": "e" * 40, "event": "schedule"}]
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0] == "master CI failed at eeeeeeee (schedule)"
    world.runs = [{"databaseId": 4, "workflowName": "CI", "status": "completed", "conclusion": "cancelled",
                   "headSha": "f" * 40, "event": "push"}]
    # A cancelled run is not a pass and not news.
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.runs = [{"databaseId": 5, "workflowName": "CI", "status": "completed", "conclusion": "success",
                   "headSha": "9" * 40, "event": "push"}]
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0] == "master CI green again at 99999999 (push)"


def test_a_suite_lease_freeing_is_an_event(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.leases["full-suite@box"] = 1
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.leases["full-suite@box"] = 0
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0] == "full-suite@box free"


def test_an_unreadable_suite_result_is_retried_not_dropped(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    (world.suites / "half.json").write_text("{", encoding="utf-8")    # still being written
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.suite("half.json")
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0].startswith("suite remote cccccccc exit 0")


def test_pending_mail_rides_along_and_never_wakes(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    (tmp_path / "digest.txt").write_text(
        "17\nCoordination: 2 addressed messages pending (agent-origin, not user authority); ...\n"
        "- abc from peer (12345678, 10:00): hi\n", encoding="utf-8")
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "300")
    assert code == watch.EXIT_QUIET and "mail: 2 pending" in out


def test_gh_failing_repeatedly_is_a_setup_failure_not_silence(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.fail_gh = True
    code, _, err = main(world, clock, tmp_path, capsys, "--timeout", "3300")
    assert code == watch.EXIT_SETUP
    assert "last: HTTP 502" in err and "[" not in err
    assert clock.t < 3300


def test_a_transient_gh_failure_keeps_waiting(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(1, checks=[run_("a", status="QUEUED", conclusion="")])]
    main(world, clock, tmp_path, capsys, "--timeout", "60")

    def between(poll):
        world.fail_gh = poll == 1
        if poll == 2:
            world.prs = [pr(1, checks=[run_("a")])]
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0",
                        between=between)
    assert code == watch.EXIT_EVENTS and out.startswith("PR #1 green")


def test_each_exit_appends_one_ledger_line(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    ledger = (tmp_path / "state.ledger").read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 2 and all(" quiet " in line for line in ledger)


@pytest.mark.parametrize("bad", [["--timeout", "0"], ["--interval", "0"], ["--settle", "-1"]])
def test_bad_arguments_are_refused(tmp_path, capsys, bad):
    world, clock = World(tmp_path), Clock()
    with pytest.raises(SystemExit) as exc:
        main(world, clock, tmp_path, capsys, *bad)
    assert exc.value.code == 2


# ── review findings, 2026-10-14 ────────────────────────────────────────────

def _run(id_, conclusion, status="completed", attempt=1, sha="e"):
    return {"databaseId": id_, "attempt": attempt, "workflowName": "CI", "status": status,
            "conclusion": conclusion, "headSha": sha * 40, "event": "push"}


def test_rerunning_a_failed_master_run_is_not_a_false_recovery(tmp_path, capsys):
    """A re-run keeps its id and runs again; an older run must not stand in
    for it while it runs, and its result is news when it lands."""
    world, clock = World(tmp_path), Clock()
    world.runs = [_run(10, "failure"), _run(9, "success", sha="9")]
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.runs = [_run(10, "", status="in_progress", attempt=2), _run(9, "success", sha="9")]
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.runs = [_run(10, "success", attempt=2), _run(9, "success", sha="9")]
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0] == "master CI green again at eeeeeeee (push)"


def test_a_board_outage_never_reads_as_a_free_lease(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.leases["full-suite"] = 1
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.leases["full-suite"] = 0
    world.board_up = False                     # lease check says free, board unreachable
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.leases["full-suite"] = 1             # still running once the board is back
    world.board_up = True
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.leases["full-suite"] = 0             # the real end
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0] == "full-suite free"


def test_a_gh_timeout_counts_as_a_failed_poll(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(1, checks=[run_("a", status="QUEUED", conclusion="")])]
    main(world, clock, tmp_path, capsys, "--timeout", "60")

    def between(poll):
        world.gh_timeout = poll == 1
        if poll == 2:
            world.prs = [pr(1, checks=[run_("a")])]
    code, out, err = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0",
                          between=between)
    assert code == watch.EXIT_EVENTS and out.startswith("PR #1 green")
    assert "timed out" in err


def test_a_conflict_seen_through_unknown_is_not_news_again(tmp_path, capsys):
    """GitHub reports UNKNOWN while it recomputes mergeability after the base
    moves; a PR that was conflicting before is not newly conflicting."""
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(2, mergeable="CONFLICTING")]
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.prs = [pr(2, mergeable="UNKNOWN")]
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.prs = [pr(2, mergeable="CONFLICTING")]
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET


def test_output_is_utf8_whatever_the_console_code_page():
    """A background command's stdout is a file, which Python before 3.15
    encodes with the ANSI code page on Windows: a title with an arrow
    would crash every re-arm before the state is saved."""
    class Stream:
        def __init__(self):
            self.args = None

        def reconfigure(self, **kw):
            self.args = kw
    out, err = Stream(), Stream()
    watch._utf8_stdio(out, err)
    assert out.args == err.args == {"encoding": "utf-8", "errors": "replace"}


# ── Codex review findings, 2026-10-14 ──────────────────────────────────────

def test_a_failed_closure_lookup_keeps_tracking_until_the_state_is_known(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(8, title="fix: x")]
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.prs = []
    world.fail_view = True                       # the merge happened; the lookup fails
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.fail_view = False
    world.pr_states = {"8": "MERGED"}
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0] == "PR #8 merged: fix: x"


def test_a_pull_request_still_open_is_not_reported_closed(tmp_path, capsys):
    """Missing from the list but OPEN by number: the page was full or the
    list lagged; keep tracking it."""
    world, clock = World(tmp_path), Clock()
    world.prs = [pr(9)]
    main(world, clock, tmp_path, capsys, "--timeout", "60")
    world.prs = []
    world.pr_states = {"9": "OPEN"}
    assert main(world, clock, tmp_path, capsys, "--timeout", "120")[0] == watch.EXIT_QUIET
    world.prs = [pr(9, checks=[run_("a")])]
    code, out, _ = main(world, clock, tmp_path, capsys, "--timeout", "600", "--settle", "0")
    assert out.splitlines()[0] == "PR #9 green at aaaaaaaa, merge state clean"


def test_a_setup_failure_on_a_fresh_install_still_exits_2(tmp_path, capsys):
    world, clock = World(tmp_path), Clock()
    world.fail_gh = True
    state = tmp_path / "new" / "dir" / "state.json"
    code = watch.main(["--state", str(state), "--suite-results", str(world.suites),
                       "--digest", str(tmp_path / "digest.txt")],
                      runner=world, clock=clock, sleep=clock.sleep)
    assert code == watch.EXIT_SETUP
    assert "setup" in state.with_suffix(".ledger").read_text(encoding="utf-8")
