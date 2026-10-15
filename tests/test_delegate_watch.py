"""pseudolife-mcp delegate-watch: wake a delegate session with board mail,
only at decision points.

The watcher polls GitHub, the suite results directory and the suite leases,
and sends the owning session one board message per batch of events. These
tests drive it with a fake command runner, a fake board, and a fake clock
and sleep, so nothing here touches the network or a daemon. Each call of
``main`` below is one bounded run (``--max-age``); state persists between
runs, as it does across a restart.
"""
import json
import os
from pathlib import Path

import httpx
import pytest

from pseudolife_memory import delegate_watch_cli as watch
from pseudolife_memory.coordination_identity import resolve_digest_path
from pseudolife_memory.lease_cli import _Board, _Refused, _Transient

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "delegate_watch_events.json"
OWNER = "0123456789abcdef0123456789abcdef"


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
        self.gh_missing = False
        self.fail_view = False
        self.pr_states = {}
        self.fail_gh = False
        self.calls = []
        self.suites = tmp_path / "suite-results"
        self.suites.mkdir(exist_ok=True)

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[0] == "gh":
            if self.gh_missing:
                raise FileNotFoundError(cmd[0])
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
            if cmd[1:3] == ["repo", "view"]:
                return 0, json.dumps({"name": "Example"}), ""
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


class FakeBoard:
    """The board calls the watcher makes, recorded. ``refuse[action]`` is a
    list of exceptions raised by the next calls of that action, in order."""

    def __init__(self, clock):
        self.clock = clock
        self.owner = {"agent_id": OWNER, "label": "claude-code", "park_reason": None}
        self.owner_present = True
        self.others = []
        self.holders = {}
        self.sent = []
        self.updates = []
        self.registers = []
        self.refuse = {}
        self.fail_after_send = []
        self.truncated = False
        self.agent_id = None
        self.closed = False

    @property
    def registered(self):
        return self.agent_id is not None

    def _maybe_refuse(self, action):
        queue = self.refuse.get(action)
        if queue:
            exc = queue.pop(0)
            if exc is not None:
                raise exc

    def register(self, *, task, project, label, status=""):
        self._maybe_refuse("register")
        self.registers.append({"task": task, "project": project, "label": label})
        self.agent_id = f"{len(self.registers):032x}"

    def forget(self):
        self.agent_id = None

    def agents_page(self, **_):
        self._maybe_refuse("agents")
        assert self.registered, "a board call without an address"
        return ([self.owner] if self.owner_present else []) + self.others, self.truncated

    def send(self, to, text, request_id, *, clears=None, urgent=False, **_):
        self._maybe_refuse("send")
        assert self.registered, "a board call without an address"
        self.sent.append({"t": self.clock(), "to": to, "text": text, "request_id": request_id,
                          "clears": clears, "urgent": urgent})
        if self.fail_after_send:
            raise self.fail_after_send.pop(0)        # recorded, but the reply was lost

    def update(self, *, status=None, task=None):
        self._maybe_refuse("update")
        assert self.registered, "a board call without an address"
        self.updates.append(status)

    def leases(self, name=None):
        self._maybe_refuse("leases")
        holder = self.holders.get(name)
        return ([{"name": name, "holder": {"agent_id": holder}}] if holder else []), False

    def close(self):
        self.closed = True

    # views
    def events(self):
        return [m for m in self.sent if m["clears"] == watch.CLEARS
                and not m["text"].startswith(("quiet ", "delegate-watch stopped:"))]

    def quiet(self):
        return [m for m in self.sent if m["text"].startswith("quiet ")]


class Harness:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.world = World(tmp_path)
        self.clock = Clock()
        self.board = FakeBoard(self.clock)
        self.state = tmp_path / "state.json"

    def main(self, *extra, between=None, max_age="600", owner=OWNER, state=None):
        """One bounded run; returns (exit code, event message texts of this
        run, stderr). ``between(poll)`` changes the world after each poll."""
        polls = {"n": 0}
        before = len(self.board.sent)

        def sleep(seconds):
            polls["n"] += 1
            if between:
                between(polls["n"])
            self.clock.sleep(seconds)

        argv = ["--state", str(state or self.state), "--suite-results", str(self.world.suites),
                "--max-age", max_age, *extra]
        if owner:
            argv += ["--owner", owner]
        code = watch.main(argv, runner=self.world, clock=self.clock, sleep=sleep, board=self.board,
                          environ={})
        texts = [m["text"] for m in self.board.sent[before:]
                 if m["clears"] == watch.CLEARS
                 and not m["text"].startswith(("quiet ", "delegate-watch stopped:"))]
        return code, texts, None

    def ledger(self):
        return (self.tmp / "state.ledger").read_text(encoding="utf-8").splitlines()


@pytest.fixture
def h(tmp_path, capsys):
    harness = Harness(tmp_path)
    harness.capsys = capsys
    return harness


def lines(texts):
    """The event lines of one run's messages, the reminder line dropped."""
    out = []
    for text in texts:
        body = text.splitlines()
        assert body[-1] == watch.REMINDER
        out += [line for line in body[:-1] if not line.endswith("more events follow")]
    return out


# ── pure classification, from the shared fixture ───────────────────────────

FIXTURE = json.loads(FIXTURES.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURE["checks_state"], ids=lambda c: c["name"])
def test_fixture_checks_state(case):
    state, failed = watch.checks_state(case["rollup"])
    assert [state, failed] == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["pr_snapshot"], ids=lambda c: c["name"])
def test_fixture_pr_snapshot(case):
    assert watch.pr_snapshot(case["old"], case["prs"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["pr_events"], ids=lambda c: c["name"])
def test_fixture_pr_events(case):
    assert watch.pr_events(case["old"], case["new"], case["closed"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["master_snapshot"], ids=lambda c: c["name"])
def test_fixture_master_snapshot(case):
    assert watch.master_snapshot(case["old"], case["runs"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["master_events"], ids=lambda c: c["name"])
def test_fixture_master_events(case):
    assert watch.master_events(case["old"], case["new"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["suite_events"], ids=lambda c: c["name"])
def test_fixture_suite_events(case):
    assert watch.suite_events(case["old"], case["new"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["lease_events"], ids=lambda c: c["name"])
def test_fixture_lease_events(case):
    assert watch.lease_events(case["old"], case["new"]) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["quiet_line"], ids=lambda c: c["name"])
def test_fixture_quiet_line(case):
    assert watch.quiet_line(case["minutes"], case["prs"]) == case["expected"]


def test_pure_functions_do_not_mutate_their_input():
    case = next(c for c in FIXTURE["pr_events"] if c["name"].startswith("closed"))
    old, new = json.loads(json.dumps(case["old"])), json.loads(json.dumps(case["new"]))
    watch.pr_events(old, new, case["closed"])
    assert (old, new) == (case["old"], case["new"])


def test_checks_state():
    assert watch.checks_state([]) == ("none", [])
    assert watch.checks_state([run_("a"), run_("b", conclusion="SKIPPED")]) == ("green", [])
    assert watch.checks_state([run_("a"), run_("b", status="IN_PROGRESS", conclusion="")]) == ("pending", [])
    assert watch.checks_state([run_("a", conclusion="FAILURE"), run_("b", status="QUEUED", conclusion="")]) \
        == ("failing", ["a"])
    status = {"__typename": "StatusContext", "context": "ci/x", "state": "ERROR"}
    assert watch.checks_state([run_("a"), status]) == ("failing", ["ci/x"])


# ── transport: board mail to the owner ─────────────────────────────────────

def test_events_go_to_the_owner_as_one_message_that_clears_the_park(h):
    h.world.prs = [pr(7, checks=[run_("a", status="IN_PROGRESS", conclusion="")])]
    h.main(max_age="60")                                              # baseline

    def between(poll):
        if poll == 1:
            h.world.prs = [pr(7, checks=[run_("a")]), pr(8, title="feat: y")]
    h.main("--settle", "0", between=between)
    events = h.board.events()
    assert len(events) == 1
    message = events[0]
    assert message["to"] == OWNER and message["clears"] == "decision events"
    assert message["urgent"] is False and len(message["request_id"]) >= 16
    assert message["text"].splitlines() == [
        "PR #7 green at aaaaaaaa, merge state clean", "PR #8 opened: feat: y",
        "Delegate: raw output goes to a subagent; a quiet wake gets one line."]
    assert h.board.registers[0]["label"] == "delegate-watch"
    assert h.board.registers[0]["task"] == f"decision events for {OWNER[:8]}"


def test_each_message_has_a_fresh_request_id(h):
    h.world.prs = [pr(1)]
    h.main(max_age="60")

    def between(poll):
        if poll == 1:
            h.world.prs = [pr(1), pr(2)]
        if poll == 3:
            h.world.prs = [pr(1), pr(2), pr(3)]
    h.main("--settle", "0", between=between)
    ids = [m["request_id"] for m in h.board.events()]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_status_is_updated_every_poll_to_stay_listed(h):
    h.world.prs = [pr(1), pr(2)]
    h.main(max_age="300", between=None)
    assert len(h.board.updates) >= 5
    assert all(status.startswith("watching 2 open PRs; ") for status in h.board.updates)


def test_a_long_batch_is_cut_to_the_boards_limit_and_keeps_the_rest():
    found = [f"PR #{n} opened: " + "x" * 70 for n in range(200)]
    text, kept, rest = watch.next_batch(found)
    assert len(text.encode("utf-8")) <= watch.MAX_TEXT_BYTES
    assert text.splitlines()[-1] == watch.REMINDER
    assert text.splitlines()[-2] == f"{len(rest)} more events follow"
    assert kept + rest == found and kept and rest
    text, kept, rest = watch.next_batch(found[:3])
    assert rest == [] and text.splitlines() == found[:3] + [watch.REMINDER]


def test_an_oversized_batch_is_delivered_across_messages_with_nothing_lost(h):
    h.main(max_age="60")
    h.world.prs = [pr(n, title="x" * 70) for n in range(1, 101)]
    code, texts, _ = h.main("--settle", "0")
    assert code == 0 and len(texts) == 2
    assert all(len(t.encode("utf-8")) <= watch.MAX_TEXT_BYTES for t in texts)
    assert lines(texts) == [f"PR #{n} opened: " + "x" * 70 for n in range(1, 101)]
    assert sum(" sent " in line for line in h.ledger()) == 2
    assert "PR #100 opened" in "".join(h.ledger())


# ── the loop: behaviours carried from ops/delegate_watch.py ─────────────────

def test_first_run_takes_a_baseline_then_keeps_warm_quietly(h):
    h.world.prs = [pr(1, checks=[run_("a")])]          # already green: not news
    h.world.suite("old.json")
    code, texts, _ = h.main("--quiet-after", "300", "--interval", "60", max_age="330")
    assert code == 0 and texts == []
    quiet = h.board.quiet()
    assert [m["text"] for m in quiet] == ["quiet 5m: 1 open PR, 0 with checks running"]
    assert quiet[0]["clears"] == "decision events"
    assert "baseline" in h.capsys.readouterr().err
    assert json.loads(h.state.read_text())["prs"]["1"]["checks"] == "green"


def test_events_are_batched_through_the_settle_window_and_reported_once(h):
    h.world.prs = [pr(7, checks=[run_("a", status="IN_PROGRESS", conclusion="")])]
    h.main(max_age="60")                                              # baseline

    def between(poll):
        if poll == 1:
            h.world.prs = [pr(7, checks=[run_("a")])]                 # CI finishes green
        if poll == 2:
            h.world.suite("new.json", machine="local-wsl", exit_code=1, summary="1 failed")
    code, texts, _ = h.main("--settle", "90", between=between, max_age="3300")
    assert code == 0 and len(texts) == 1
    assert lines(texts) == ["PR #7 green at aaaaaaaa, merge state clean",
                            "suite local-wsl cccccccc exit 1: 1 failed"]
    assert h.board.events()[0]["t"] < 300                             # did not wait for anything else
    # Restarted with nothing new: no repeat.
    assert h.main(max_age="120")[1] == []


def test_pull_request_transitions(h):
    h.world.prs = [pr(1, checks=[run_("a", status="QUEUED", conclusion="")]), pr(2), pr(3)]
    h.main(max_age="60")
    h.world.prs = [pr(1, checks=[run_("lint", conclusion="FAILURE")]),
                   pr(2, mergeable="CONFLICTING", state="DIRTY"),
                   pr(4, title="feat(x): new thing")]
    h.world.pr_states = {"3": "CLOSED"}
    code, texts, _ = h.main("--settle", "0")
    assert lines(texts) == ["PR #1 failing at aaaaaaaa: lint",
                            "PR #2 conflicts with its base",
                            "PR #4 opened: feat(x): new thing",
                            "PR #3 closed: t"]


def test_a_new_head_reports_its_result_again(h):
    h.world.prs = [pr(5, checks=[run_("a")])]
    h.main(max_age="60")
    h.world.prs = [pr(5, head="b" * 40, checks=[run_("a", status="QUEUED", conclusion="")])]
    assert h.main(max_age="120")[1] == []
    h.world.prs = [pr(5, head="b" * 40, checks=[run_("a")])]
    assert lines(h.main("--settle", "0")[1]) == ["PR #5 green at bbbbbbbb, merge state clean"]


def test_master_runs_failing_and_recovering(h):
    h.world.runs = [{"databaseId": 1, "workflowName": "CI", "status": "completed", "conclusion": "success",
                     "headSha": "d" * 40, "event": "push", "attempt": 1}]
    h.main(max_age="60")
    h.world.runs = [{"databaseId": 3, "workflowName": "CI", "status": "in_progress", "conclusion": "",
                     "headSha": "f" * 40, "event": "push"},
                    {"databaseId": 2, "workflowName": "CI", "status": "completed", "conclusion": "failure",
                     "headSha": "e" * 40, "event": "schedule"}]
    assert lines(h.main("--settle", "0")[1]) == ["master CI failed at eeeeeeee (schedule)"]
    h.world.runs = [{"databaseId": 4, "workflowName": "CI", "status": "completed", "conclusion": "cancelled",
                     "headSha": "f" * 40, "event": "push"}]
    # A cancelled run is not a pass and not news.
    assert h.main(max_age="120")[1] == []
    h.world.runs = [{"databaseId": 5, "workflowName": "CI", "status": "completed", "conclusion": "success",
                     "headSha": "9" * 40, "event": "push"}]
    assert lines(h.main("--settle", "0")[1]) == ["master CI green again at 99999999 (push)"]


def test_a_suite_lease_freeing_is_an_event(h):
    h.world.leases["full-suite@box"] = 1
    h.main(max_age="60")
    h.world.leases["full-suite@box"] = 0
    assert lines(h.main("--settle", "0")[1]) == ["full-suite@box free"]


def test_an_unreadable_suite_result_is_retried_not_dropped(h):
    h.main(max_age="60")
    (h.world.suites / "half.json").write_text("{", encoding="utf-8")    # still being written
    assert h.main(max_age="120")[1] == []
    h.world.suite("half.json")
    assert lines(h.main("--settle", "0")[1])[0].startswith("suite remote cccccccc exit 0")


def test_gh_failing_repeatedly_tells_the_owner_once_then_exits_2(h):
    h.main(max_age="60")
    h.world.fail_gh = True
    code, _, _ = h.main(max_age="3300")
    assert code == watch.EXIT_SETUP
    err = h.capsys.readouterr().err
    assert "last: HTTP 502" in err and "[" not in err
    assert h.clock.t < 3300
    assert len(h.board.sent) == 1
    final = h.board.sent[0]
    # It clears the park: after a re-registration the owner's park_clear_by
    # names an old id, so only the need match can still ring it.
    assert final["clears"] == "decision events"
    assert final["to"] == OWNER and "gh failed on 5 polls in a row" in final["text"]
    assert final["text"].startswith("delegate-watch stopped:")


def test_gh_missing_tells_the_owner_and_exits_2(h):
    h.world.gh_missing = True
    code, _, _ = h.main()
    assert code == watch.EXIT_SETUP
    assert len(h.board.sent) == 1 and h.board.sent[0]["clears"] == "decision events"
    assert "gh is not installed" in h.board.sent[0]["text"]


def test_a_transient_gh_failure_keeps_waiting(h):
    h.world.prs = [pr(1, checks=[run_("a", status="QUEUED", conclusion="")])]
    h.main(max_age="60")

    def between(poll):
        h.world.fail_gh = poll == 1
        if poll == 2:
            h.world.prs = [pr(1, checks=[run_("a")])]
    code, texts, _ = h.main("--settle", "0", between=between)
    assert code == 0 and lines(texts)[0].startswith("PR #1 green")


def test_each_exit_appends_one_ledger_line(h):
    h.main(max_age="60")
    h.main(max_age="60")
    ledger = h.ledger()
    assert len(ledger) == 2 and all(" exit max age" in line for line in ledger)


def test_each_message_appends_one_ledger_line(h):
    h.world.prs = [pr(1)]
    h.main(max_age="60")
    h.world.prs = [pr(1), pr(2, title="feat: z")]
    h.main("--settle", "0", "--quiet-after", "300", max_age="400")
    ledger = h.ledger()
    assert any(" sent 1: PR #2 opened: feat: z" in line for line in ledger)
    assert any(" quiet 5m" in line for line in ledger)


@pytest.mark.parametrize("bad", [["--quiet-after", "0"], ["--interval", "0"], ["--settle", "-1"],
                                 ["--max-age", "0"], ["--max-age", "soon"]])
def test_bad_arguments_are_refused(h, bad):
    with pytest.raises(SystemExit) as exc:
        h.main(*bad)
    assert exc.value.code == 2


# ── review findings, 2026-10-14 ────────────────────────────────────────────

def _run(id_, conclusion, status="completed", attempt=1, sha="e"):
    return {"databaseId": id_, "attempt": attempt, "workflowName": "CI", "status": status,
            "conclusion": conclusion, "headSha": sha * 40, "event": "push"}


def test_rerunning_a_failed_master_run_is_not_a_false_recovery(h):
    """A re-run keeps its id and runs again; an older run must not stand in
    for it while it runs, and its result is news when it lands."""
    h.world.runs = [_run(10, "failure"), _run(9, "success", sha="9")]
    h.main(max_age="60")
    h.world.runs = [_run(10, "", status="in_progress", attempt=2), _run(9, "success", sha="9")]
    assert h.main(max_age="120")[1] == []
    h.world.runs = [_run(10, "success", attempt=2), _run(9, "success", sha="9")]
    assert lines(h.main("--settle", "0")[1]) == ["master CI green again at eeeeeeee (push)"]


def test_a_board_outage_never_reads_as_a_free_lease(h):
    h.world.leases["full-suite"] = 1
    h.main(max_age="60")
    h.world.leases["full-suite"] = 0
    h.world.board_up = False                     # lease check says free, board unreachable
    assert h.main(max_age="120")[1] == []
    h.world.leases["full-suite"] = 1             # still running once the board is back
    h.world.board_up = True
    assert h.main(max_age="120")[1] == []
    h.world.leases["full-suite"] = 0             # the real end
    assert lines(h.main("--settle", "0")[1]) == ["full-suite free"]


def test_a_gh_timeout_counts_as_a_failed_poll(h):
    h.world.prs = [pr(1, checks=[run_("a", status="QUEUED", conclusion="")])]
    h.main(max_age="60")

    def between(poll):
        h.world.gh_timeout = poll == 1
        if poll == 2:
            h.world.prs = [pr(1, checks=[run_("a")])]
    code, texts, _ = h.main("--settle", "0", between=between)
    assert code == 0 and lines(texts)[0].startswith("PR #1 green")
    assert "timed out" in h.capsys.readouterr().err


def test_a_conflict_seen_through_unknown_is_not_news_again(h):
    """GitHub reports UNKNOWN while it recomputes mergeability after the base
    moves; a PR that was conflicting before is not newly conflicting."""
    h.world.prs = [pr(2, mergeable="CONFLICTING")]
    h.main(max_age="60")
    h.world.prs = [pr(2, mergeable="UNKNOWN")]
    assert h.main(max_age="120")[1] == []
    h.world.prs = [pr(2, mergeable="CONFLICTING")]
    assert h.main(max_age="120")[1] == []


def test_output_is_utf8_whatever_the_console_code_page():
    """A detached watcher's stdout is a log file, which Python before 3.15
    encodes with the ANSI code page on Windows: a title with an arrow would
    crash the watcher before the state is saved."""
    class Stream:
        def __init__(self):
            self.args = None

        def reconfigure(self, **kw):
            self.args = kw
    out, err = Stream(), Stream()
    watch._utf8_stdio(out, err)
    assert out.args == err.args == {"encoding": "utf-8", "errors": "replace"}


# ── Codex review findings, 2026-10-14 ──────────────────────────────────────

def test_a_failed_closure_lookup_keeps_tracking_until_the_state_is_known(h):
    h.world.prs = [pr(8, title="fix: x")]
    h.main(max_age="60")
    h.world.prs = []
    h.world.fail_view = True                       # the merge happened; the lookup fails
    assert h.main(max_age="120")[1] == []
    h.world.fail_view = False
    h.world.pr_states = {"8": "MERGED"}
    assert lines(h.main("--settle", "0")[1]) == ["PR #8 merged: fix: x"]


def test_a_pull_request_still_open_is_not_reported_closed(h):
    """Missing from the list but OPEN by number: the page was full or the
    list lagged; keep tracking it."""
    h.world.prs = [pr(9)]
    h.main(max_age="60")
    h.world.prs = []
    h.world.pr_states = {"9": "OPEN"}
    assert h.main(max_age="120")[1] == []
    h.world.prs = [pr(9, checks=[run_("a")])]
    assert lines(h.main("--settle", "0")[1]) == ["PR #9 green at aaaaaaaa, merge state clean"]


def test_a_setup_failure_on_a_fresh_install_still_exits_2(h):
    h.world.fail_gh = True
    state = h.tmp / "new" / "dir" / "state.json"
    code, _, _ = h.main(state=state)
    assert code == watch.EXIT_SETUP
    assert "setup" in state.with_suffix(".ledger").read_text(encoding="utf-8")


# ── keep-warm and the owner's park ─────────────────────────────────────────

def test_no_quiet_mail_for_an_owner_parked_done(h):
    h.board.owner["park_reason"] = "done"
    code, _, _ = h.main("--quiet-after", "300", max_age="1000")
    assert code == 0 and h.board.quiet() == []


def test_quiet_mail_resumes_after_each_quiet_stretch(h):
    h.board.owner["park_reason"] = "waiting_peer"
    h.main("--quiet-after", "300", max_age="1000")
    assert [round(m["t"]) for m in h.board.quiet()] == [300, 600, 900]


def test_an_event_resets_the_quiet_clock(h):
    h.world.prs = [pr(1)]
    h.main(max_age="60")

    def between(poll):
        if poll == 3:                  # this run starts at t=60; the event lands at t=240
            h.world.prs = [pr(1), pr(2)]
    h.main("--settle", "0", "--quiet-after", "300", between=between, max_age="500")
    assert [round(m["t"]) for m in h.board.quiet()] == [540]


def test_the_owner_leaving_the_board_ends_the_watch(h):
    def between(poll):
        if poll == 2:
            h.board.owner_present = False
    code, _, _ = h.main(between=between, max_age="3000")
    assert code == 0 and h.clock.t < 300
    assert "exit owner gone" in h.ledger()[-1]


def test_a_cut_page_without_the_owner_is_not_proof_it_left(h):
    h.board.owner_present = False
    h.board.truncated = True
    code, _, _ = h.main(max_age="300")
    assert code == 0 and "exit max age" in h.ledger()[-1]


def test_a_done_owner_off_a_cut_page_still_gets_no_quiet_mail(h):
    h.board.owner["park_reason"] = "done"

    def between(poll):
        if poll == 1:
            h.board.owner_present = False
            h.board.truncated = True
    code, _, _ = h.main("--quiet-after", "300", between=between, max_age="1000")
    assert code == 0 and h.board.quiet() == []


def test_until_lease_exits_once_the_owner_no_longer_holds_it(h):
    h.board.holders["delegate:Example"] = OWNER

    def between(poll):
        if poll == 2:
            h.board.holders["delegate:Example"] = "f" * 32
    code, _, _ = h.main("--until-lease", "delegate:Example", between=between, max_age="3000")
    assert code == 0 and h.clock.t < 300
    assert "exit lease delegate:Example no longer held by the owner" in h.ledger()[-1]


def test_a_forgotten_address_registers_again_once(h):
    h.board.refuse["agents"] = [_Refused("gone", "instance_not_found")]
    h.world.prs = [pr(1)]
    h.main(max_age="60")
    assert len(h.board.registers) == 2
    assert "exit max age" in h.ledger()[-1]


def test_a_failed_registration_retry_is_retried_next_poll(h):
    h.world.prs = [pr(1)]
    h.main(max_age="60")
    h.board.refuse["agents"] = [_Refused("gone", "instance_not_found")]
    h.board.refuse["register"] = [None, _Transient("HTTP 503")]
    h.world.prs = [pr(1), pr(2, title="x")]
    code, texts, _ = h.main("--settle", "0")
    assert code == 0 and lines(texts) == ["PR #2 opened: x"]
    # first run, this run's start, the failed retry, the retry that worked
    assert len(h.board.registers) == 3


def test_a_second_refusal_after_registering_again_exits_2(h):
    h.board.refuse["agents"] = [_Refused("gone", "instance_not_found"),
                                _Refused("gone", "invalid_credential")]
    code, _, _ = h.main(max_age="600")
    assert code == watch.EXIT_SETUP and len(h.board.registers) == 2


def test_a_transient_board_failure_keeps_the_events_for_the_next_poll(h):
    h.world.prs = [pr(1)]
    h.main(max_age="60")
    h.world.prs = [pr(1), pr(2, title="x")]
    h.board.refuse["send"] = [_Transient("HTTP 503")]
    code, texts, _ = h.main("--settle", "0")
    assert code == 0 and lines(texts) == ["PR #2 opened: x"]


def test_a_send_whose_reply_was_lost_is_retried_with_the_same_request_id(h):
    h.world.prs = [pr(1)]
    h.main(max_age="60")
    h.world.prs = [pr(1), pr(2, title="x")]
    h.board.fail_after_send = [_Transient("timed out")]

    def between(poll):
        if poll == 1:                                # arrives while the first batch is pending
            h.world.prs = [pr(1), pr(2, title="x"), pr(3, title="y")]
    h.main("--settle", "0", between=between)
    first, retry, later = h.board.events()
    assert (retry["request_id"], retry["text"]) == (first["request_id"], first["text"])
    assert lines([first["text"]]) == ["PR #2 opened: x"]
    assert lines([later["text"]]) == ["PR #3 opened: y"]
    assert later["request_id"] != first["request_id"]


def test_the_pid_file_names_the_watcher_while_it_runs(h):
    seen = {}

    def between(poll):
        if poll == 1:
            seen.update(json.loads(h.state.with_suffix(".pid").read_text(encoding="utf-8")))
    h.main(between=between, max_age="120")
    assert seen["pid"] == os.getpid() and seen["owner"] == OWNER
    assert seen["agent_id"] == f"{1:032x}"
    assert not h.state.with_suffix(".pid").exists()
    assert h.board.closed


def test_a_second_watcher_on_the_same_state_exits_quietly(h, capsys):
    from pseudolife_memory.os_lock import OsLock

    lock = OsLock(h.state.with_suffix(".lock"))
    assert lock.acquire()
    try:
        code, _, _ = h.main(max_age="300")
    finally:
        lock.release()
    assert code == 0 and h.board.registers == [] and h.board.sent == []
    assert "another watcher holds" in capsys.readouterr().err


def test_the_state_lock_excludes_a_watcher_in_another_process(h, capsys):
    """Two starts racing past the pid check: the one whose lock is held by
    another process exits without registering."""
    import subprocess
    import sys

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from pathlib import Path; from pseudolife_memory.os_lock import OsLock; "
         "lock = OsLock(Path(sys.argv[1])); print(lock.acquire(), flush=True); sys.stdin.read()",
         str(h.state.with_suffix(".lock"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[1])
    try:
        assert holder.stdout.readline().strip() == "True"
        code, _, _ = h.main(max_age="300")
        assert code == 0 and h.board.registers == []
        assert "another watcher holds" in capsys.readouterr().err
    finally:
        holder.communicate("", timeout=30)
    code, _, _ = h.main(max_age="60")                   # free again: this one runs
    assert code == 0 and len(h.board.registers) == 1


# ── --stop ──────────────────────────────────────────────────────────────────

def test_stop_asks_the_running_watcher_to_exit_at_its_next_poll(h):
    def between(poll):
        if poll == 2:
            assert watch.main(["--stop", "--state", str(h.state)], environ={}) == 0
            assert h.state.with_suffix(".stop").exists()
    code, _, _ = h.main(between=between, max_age="3000")
    assert code == 0 and h.clock.t < 300
    assert "exit stopped" in h.ledger()[-1]
    assert not h.state.with_suffix(".stop").exists()


def test_stop_with_no_watcher_running_changes_nothing(h, capsys):
    assert watch.main(["--stop", "--state", str(h.state)], environ={}) == 0
    assert "no watcher running" in capsys.readouterr().out
    assert not h.state.with_suffix(".stop").exists()


def test_a_stale_stop_file_does_not_stop_a_new_watcher(h):
    h.state.with_suffix(".stop").write_text("", encoding="utf-8")
    code, _, _ = h.main(max_age="300")
    assert code == 0 and "exit max age" in h.ledger()[-1]


# ── owner resolution ───────────────────────────────────────────────────────

def _session(tmp_path, monkeypatch=None):
    env = {"CLAUDE_CODE_SESSION_ID": "session-1", "PSEUDOLIFE_DIGEST_DIR": str(tmp_path / "digests")}
    digest = resolve_digest_path("session-1", env=env)
    digest.parent.mkdir(parents=True, exist_ok=True)
    return env, digest


def test_owner_comes_from_the_session_agent_file(tmp_path):
    env, digest = _session(tmp_path)
    digest.with_suffix(".agent").write_text(OWNER + "\n", encoding="utf-8")
    assert watch.resolve_owner(None, env) == OWNER
    assert watch.default_state(env) == digest.with_suffix(".dwatch.json")


def test_owner_flag_wins_and_must_be_32_hex(tmp_path):
    env, _ = _session(tmp_path)
    assert watch.resolve_owner("f" * 32, env) == "f" * 32
    with pytest.raises(watch.OwnerError, match="32 lowercase hex"):
        watch.resolve_owner("F" * 32, env)


@pytest.mark.parametrize("body", ["", "not-an-id\n", OWNER.upper() + "\n", OWNER[:31] + "\n"])
def test_an_agent_file_without_an_id_is_refused(tmp_path, body):
    env, digest = _session(tmp_path)
    digest.with_suffix(".agent").write_text(body, encoding="utf-8")
    with pytest.raises(watch.OwnerError):
        watch.resolve_owner(None, env)


def test_a_missing_agent_file_or_session_is_refused(tmp_path):
    env, _ = _session(tmp_path)
    with pytest.raises(watch.OwnerError):
        watch.resolve_owner(None, env)
    with pytest.raises(watch.OwnerError, match="CLAUDE_CODE_SESSION_ID"):
        watch.resolve_owner(None, {})


def test_a_linked_agent_file_is_refused(tmp_path):
    env, digest = _session(tmp_path)
    target = tmp_path / "elsewhere.agent"
    target.write_text(OWNER + "\n", encoding="utf-8")
    try:
        os.symlink(target, digest.with_suffix(".agent"))
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")
    with pytest.raises(watch.OwnerError, match="regular file"):
        watch.resolve_owner(None, env)


def test_no_owner_exits_2_naming_the_flag(tmp_path, capsys):
    code = watch.main(["--state", str(tmp_path / "s.json")], environ={})
    assert code == watch.EXIT_SETUP
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "--owner" in err[0]


def test_the_default_state_without_a_session_is_per_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert watch.default_state({}, OWNER) == tmp_path / ".pseudolife-mcp" / "watch" / f"{OWNER}.json"
    assert watch.default_state({}, "f" * 32) != watch.default_state({}, OWNER)


def test_a_state_held_for_another_owner_is_refused(tmp_path, capsys):
    state = tmp_path / "w.json"
    watch._write_pid(state, agent_id="a" * 32, owner="f" * 32)

    def spawn(argv, log):
        raise AssertionError("must not start")
    code = watch.main(["--detach", "--owner", OWNER, "--state", str(state)], environ={}, spawn=spawn)
    assert code == watch.EXIT_SETUP
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "f" * 32 in err[0] and "--state" in err[0]


def test_stop_without_a_session_needs_the_owner_or_the_state(capsys):
    assert watch.main(["--stop"], environ={}) == watch.EXIT_SETUP
    assert "--owner" in capsys.readouterr().err


# ── --detach ───────────────────────────────────────────────────────────────

def test_detach_reports_one_already_running(tmp_path, capsys):
    state = tmp_path / "w.json"
    watch._write_pid(state, agent_id="a" * 32, owner=OWNER)          # this process, alive

    def spawn(argv, log):
        raise AssertionError("must not start a second watcher")
    code = watch.main(["--detach", "--owner", OWNER, "--state", str(state)], environ={}, spawn=spawn)
    assert code == 0
    out = capsys.readouterr().out
    assert "already running" in out and "a" * 32 in out


def test_detach_starts_the_watcher_and_prints_its_address(tmp_path, capsys):
    state = tmp_path / "w.json"
    # A pid file whose process start does not match is stale, not running.
    state.with_suffix(".pid").write_text(json.dumps(
        {"pid": os.getpid(), "process_start": "not-this-process", "agent_id": "b" * 32,
         "owner": OWNER, "started": 0}), encoding="utf-8")
    started = {}

    def spawn(argv, log):
        started.update(argv=argv, log=log)
        # A Windows venv's python.exe is a launcher: the watcher's own pid
        # differs from the one Popen returns. The nonce ties them.
        nonce = argv[argv.index("--nonce") + 1]
        state.with_suffix(".pid").write_text(json.dumps(
            {"pid": 5555, "agent_id": "c" * 32, "owner": OWNER, "started": 1, "process_start": None,
             "nonce": nonce}), encoding="utf-8")
        return 4242
    clock = Clock()
    code = watch.main(["--detach", "--owner", OWNER, "--state", str(state), "--interval", "30"],
                      environ={}, spawn=spawn, clock=clock, sleep=clock.sleep)
    assert code == 0
    argv = started["argv"]
    assert "--detach" not in argv and argv[argv.index("--interval") + 1] == "30"
    assert len(argv[argv.index("--nonce") + 1]) == 32
    assert argv[argv.index("--owner") + 1] == OWNER and argv[argv.index("--state") + 1] == str(state)
    assert argv[1:4] == ["-m", "pseudolife_memory.cli", "delegate-watch"]
    assert started["log"] == state.with_suffix(".log")
    out = capsys.readouterr().out.strip()
    assert out == (f"delegate-watch {'c' * 32} watching for {OWNER}; park with "
                   f"park_needs=\"decision events\" park_clear_by={'c' * 32}")


def test_detach_says_still_starting_while_the_child_runs_without_a_record(tmp_path, capsys):
    state = tmp_path / "w.json"
    clock = Clock()
    code = watch.main(["--detach", "--owner", OWNER, "--state", str(state)], environ={},
                      spawn=lambda argv, log: os.getpid(), clock=clock, sleep=clock.sleep)
    assert code == 0
    out = capsys.readouterr().out
    assert "still starting" in out and str(state.with_suffix(".log")) in out


def test_detach_that_lost_a_race_reports_the_winner(tmp_path, capsys):
    state = tmp_path / "w.json"

    def spawn(argv, log):
        watch._write_pid(state, agent_id="d" * 32, owner=OWNER)      # the other start won
        return 999999999                                              # ours exited at once
    clock = Clock()
    code = watch.main(["--detach", "--owner", OWNER, "--state", str(state)], environ={},
                      spawn=spawn, clock=clock, sleep=clock.sleep)
    assert code == 0
    out = capsys.readouterr().out
    assert "already running" in out and "d" * 32 in out


def test_detach_reports_a_child_that_never_came_up(tmp_path, capsys):
    state = tmp_path / "w.json"
    state.with_suffix(".log").write_text("delegate-watch: no board: no bearer token\n", encoding="utf-8")
    clock = Clock()
    code = watch.main(["--detach", "--owner", OWNER, "--state", str(state)], environ={},
                      spawn=lambda argv, log: 999999999, clock=clock, sleep=clock.sleep)
    assert code == watch.EXIT_SETUP
    err = capsys.readouterr().err
    assert "no bearer token" in err and str(state.with_suffix(".log")) in err
    assert clock.t <= watch.DETACH_WAIT


# ── the board client additions ─────────────────────────────────────────────

def _board(calls):
    def handler(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        if calls[-1][0] == "register":
            return httpx.Response(200, json={"agent_id": OWNER, "credential": "cred"})
        return httpx.Response(200, json={"ok": True})
    board = _Board("http://127.0.0.1:1", "tok", httpx.MockTransport(handler))
    board.register(task="t", project="p")
    return board


def test_board_send_carries_clears_and_urgent_only_when_set():
    calls = []
    board = _board(calls)
    board.send("x" * 32, "hi", "r1")
    board.send("x" * 32, "hi", "r2", clears="decision events")
    board.send("x" * 32, "hi", "r3", urgent=True)
    bodies = [body for action, body in calls if action == "send"]
    assert bodies == [{"to": "x" * 32, "text": "hi", "request_id": "r1"},
                      {"to": "x" * 32, "text": "hi", "request_id": "r2", "clears": "decision events"},
                      {"to": "x" * 32, "text": "hi", "request_id": "r3", "urgent": True}]


def test_board_agents_page_says_when_the_page_was_cut():
    def handler(request):
        action = request.url.path.rsplit("/", 1)[-1]
        if action == "register":
            return httpx.Response(200, json={"agent_id": OWNER, "credential": "cred"})
        return httpx.Response(200, json={"agents": [{"agent_id": "a" * 32}, "junk"], "truncated": True})
    board = _Board("http://127.0.0.1:1", "tok", httpx.MockTransport(handler))
    board.register(task="t", project="p")
    assert board.agents_page() == ([{"agent_id": "a" * 32}], True)
    assert board.agents() == [{"agent_id": "a" * 32}]


def test_board_update_posts_only_the_fields_given():
    calls = []
    board = _board(calls)
    board.update(status="watching 2 open PRs; no events yet")
    board.update(task="t2")
    assert [body for action, body in calls if action == "update"] == [
        {"status": "watching 2 open PRs; no events yet"}, {"task": "t2"}]


# ── dispatch ────────────────────────────────────────────────────────────────

def test_the_package_command_dispatches_here(monkeypatch, capsys):
    from pseudolife_memory.cli import main as cli_main

    monkeypatch.setattr("sys.argv", ["pseudolife-mcp", "delegate-watch", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: pseudolife-mcp delegate-watch") and "--detach" in out
