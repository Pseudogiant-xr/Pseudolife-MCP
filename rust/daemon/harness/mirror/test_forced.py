"""The forced real-data runner's refusal, classification and report, without daemons."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forced  # noqa: E402

PASS = {"status": "pass", "diffs": [], "seconds": 1.0}
DIFF = {"status": "diff", "diffs": ["DIFF [s] step 1 x: db /rows/public.entries"], "seconds": 1.0}


def test_outcome_reads_the_harness_output():
    assert forced.outcome(0, "ok\n")["status"] == "pass"
    got = forced.outcome(1, "DIFF [a] step 1 {}: x\nDB DIFF [a] y\nnoise\n")
    assert got["status"] == "diff" and len(got["diffs"]) == 2
    assert forced.outcome(1, "Traceback\nboom\n") == {"status": "error", "diffs": [],
                                                       "error_tail": ["Traceback", "boom"]}
    # Exit 0 with a printed difference is still a difference.
    assert forced.outcome(0, "DIFF [a] step 1 {}: x\n")["status"] == "diff"
    assert forced.outcome(None, "TIMEOUT after 1s")["status"] == "error"


@pytest.mark.parametrize("first,retry,synthetic,want", [
    (PASS, None, None, "pass"),
    (DIFF, PASS, None, "transient"),
    (DIFF, DIFF, DIFF, "preexisting"),
    (DIFF, DIFF, PASS, "new"),
    ({"status": "error", "diffs": []}, {"status": "error", "diffs": []}, PASS, "error"),
    ({"status": "error", "diffs": []}, {"status": "error", "diffs": []}, DIFF, "error"),
])
def test_classification(first, retry, synthetic, want):
    assert forced.classify(first, retry, synthetic) == want


class FakeRunner(forced.Runner):
    def __init__(self, tmp: Path, answers: dict, stop_after: int | None = None):
        super().__init__(Path("rust-bin"), tmp, 60, tmp / "STOP")
        self.answers, self.calls, self.stop_after = answers, [], stop_after

    def invoke(self, suite, name, label, base):
        self.calls.append((suite, name, label, base))
        return dict(self.answers[name][label])

    def blocked(self):
        done = len({(s, n) for s, n, _, _ in self.calls})
        return "STOP file present" if self.stop_after is not None and done >= self.stop_after else None


ANSWERS = {"clean": {"real": PASS}, "flaky": {"real": DIFF, "real-retry": PASS},
           "old": {"real": DIFF, "real-retry": DIFF, "synthetic": DIFF},
           "fresh": {"real": DIFF, "real-retry": DIFF, "synthetic": PASS}}


def test_run_classifies_each_scenario_and_reruns_only_what_differs(tmp_path):
    runner = FakeRunner(tmp_path, ANSWERS)
    report, private = forced.run(runner, ["w2e"], None, names=lambda suite: list(ANSWERS))
    rows = {r["scenario"]: r["class"] for r in report["suites"]["w2e"]["rows"]}
    assert rows == {"clean": "pass", "flaky": "transient", "old": "preexisting", "fresh": "new"}
    assert [c for c in runner.calls if c[1] == "clean"] == [("w2e", "clean", "real", True)]
    # The synthetic rerun is the only one without the real-bank base.
    assert [c[3] for c in runner.calls if c[1] == "fresh"] == [True, True, False]
    assert set(private) == {"w2e/flaky", "w2e/old", "w2e/fresh"} and report["complete"]
    counts = forced.counts(report)["w2e"]
    assert counts == {"planned": 4, "run": 4, "pass": 1, "transient": 1, "preexisting": 1, "new": 1, "accepted": 0, "excluded": 0,
                      "error": 0, "differences": 3, "errors_with_oom": None, "new_scenarios": ["fresh"]}
    assert "DIFF" not in json.dumps(counts)  # counts and names only


def test_a_stop_ends_the_run_incomplete(tmp_path):
    runner = FakeRunner(tmp_path, ANSWERS, stop_after=1)
    report, _ = forced.run(runner, ["w2e"], None, names=lambda suite: list(ANSWERS))
    assert not report["complete"] and report["stopped"].startswith("before w2e/flaky")
    assert len(report["suites"]["w2e"]["rows"]) == 1


@pytest.mark.parametrize("host,base", [("127.0.0.1:5433", "pl_cf_mir_seed"), ("pseudolife-pg:5432", "pl_cf_mir_seed"),
                                       ("mirror-pg:5432", ""), ("mirror-pg:5432", "pseudolife_memory")])
def test_the_runner_refuses_a_live_or_missing_base_before_anything(tmp_path, monkeypatch, host, base):
    monkeypatch.setenv("PSEUDOLIFE_TEST_PG_HOST_PORT", host)
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", base)
    monkeypatch.setattr(forced, "run", lambda *a, **k: pytest.fail("ran past the refusal"))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "out")]) == 3
    assert not (tmp_path / "out").exists()


def test_every_suite_names_a_live_harness_script():
    for suite, (script, directory, _) in forced.SUITES.items():
        assert (forced.HARNESS / script).is_file() and directory.is_dir(), suite


def test_preexisting_needs_the_synthetic_run_to_differ_in_the_same_place():
    real = {"status": "diff", "diffs": [
        'DIFF [store-reaper] step 1 {"op": "tool"}: db /rows/public.episodes/rows: row only in python: [1]']}
    same = {"status": "diff", "diffs": [
        'DIFF [store-reaper] step 1 {"op": "x"}: db /rows/public.episodes/rows: row only in rust: [2]']}
    elsewhere = {"status": "diff", "diffs": ['DIFF [store-reaper] step 2 {}: /json/results: python 1 vs rust 2']}
    broken = {"status": "error", "diffs": [], "error_tail": ["Traceback", "OSError: port in use"]}
    assert forced.signatures(real) == {("1", "/rows/public.episodes/rows")}
    assert forced.classify(real, real, same) == "preexisting"
    assert forced.classify(real, real, elsewhere) == "new"
    assert forced.classify(real, real, broken) == "new"  # an unrelated synthetic failure excuses nothing
    # A scenario no attempt could compare is an error, never preexisting or a pass.
    assert forced.classify(broken, broken, broken) == "error"


def test_every_harness_summary_line_counts_and_progress_lines_do_not():
    log = ("DIFF refusals/oversize-body: ['status 413 vs 400']\n"
           "STATE [identity] /rows/public.coordination_agents: x\n"
           "CHAIN [identity] python chain broken at 3\n"
           "  DB DIFF /rows/public.entries: repeated progress line\n")
    got = forced.outcome(1, log)
    assert got["status"] == "diff" and len(got["diffs"]) == 3


def test_an_unknown_scenario_is_refused_not_reported_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["clean"])
    monkeypatch.setattr(forced, "run", lambda *a, **k: pytest.fail("ran an empty selection"))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e",
                        "--only", "cleen"]) == 2


def test_an_unset_host_is_checked_as_the_harnesses_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_TEST_PG_HOST_PORT", raising=False)
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    monkeypatch.setattr(forced, "run", lambda *a, **k: pytest.fail("ran past the refusal"))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "out")]) == 3  # 127.0.0.1:5433


def test_a_suite_that_cannot_list_its_scenarios_does_not_end_the_run(tmp_path):
    def names(suite):
        if suite == "board":
            raise forced.subprocess.CalledProcessError(1, "x")
        return ["clean"]
    report, _ = forced.run(FakeRunner(tmp_path, ANSWERS), ["board", "w2e"], None, names=names)
    assert report["suites"]["board"]["error"] and report["suites"]["w2e"]["rows"][0]["class"] == "pass"


@pytest.mark.skipif(not hasattr(forced.os, "killpg"), reason="the runner is Linux; process groups are POSIX")
def test_a_timeout_stops_the_whole_group_and_sweeps(tmp_path, monkeypatch):
    script = tmp_path / "slow.py"
    pidfile = tmp_path / "child.pid"
    script.write_text(
        "import subprocess, sys, time, os\n"
        "assert 'PL_HARNESS_BASE_DATABASE' not in os.environ\n"
        "assert 'PL_HARNESS_STUB_DEFAULT_CONTENT' not in os.environ\n"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "print('DIFF [slow] step 1 {}: started', flush=True)\n"
        "time.sleep(600)\n")
    monkeypatch.setitem(forced.SUITES, "w2e", (str(script), tmp_path, ""))
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    monkeypatch.setenv("PL_HARNESS_STUB_DEFAULT_CONTENT", "[]")
    swept = []
    runner = forced.Runner(Path("rust-bin"), tmp_path / "out", 3, tmp_path / "STOP",
                           sweep=lambda: swept.append(1) or 2, scratch=tmp_path / "scratch")
    got = runner.invoke("w2e", "slow", "synthetic", base=False)
    assert got["exit"] is None and got["status"] == "diff" and swept == [1]
    log = (tmp_path / "out" / "w2e" / "synthetic" / "slow.log").read_text()
    assert "TIMEOUT after 3s" in log and "swept 2 leftover" in log
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            forced.os.kill(child, 0)
        except ProcessLookupError:
            break
        forced.time.sleep(0.1)
    else:
        pytest.fail("the harness's child outlived the timeout")


def _alive(pid: int) -> bool:
    try:
        forced.os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # gone between the signal check and this read: dead too
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _wait_dead(pid: int) -> bool:
    for _ in range(50):
        if not _alive(pid):
            return True
        forced.time.sleep(0.1)
    return False


@pytest.mark.skipif(not hasattr(forced.os, "killpg"), reason="the runner is Linux; process groups are POSIX")
def test_a_harness_that_exits_leaves_none_of_its_processes_behind(tmp_path, monkeypatch):
    """A harness that raised (a daemon refused, a seed collided) may leave a
    daemon its cleanup lost: 2.9 GiB each, which pushed later scenarios past
    the runner's cap (83 OOM kills, daemon box, 2026-11-07)."""
    script, pidfile = tmp_path / "quits.py", tmp_path / "child.pid"
    script.write_text(
        "import subprocess, sys\n"
        # Its own output, as a daemon's goes to its log: the harness's pipe
        # closes when the harness exits, so no timeout stands in for the stop.
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "raise SystemExit(1)\n")
    monkeypatch.setitem(forced.SUITES, "w2e", (str(script), tmp_path, ""))
    runner = forced.Runner(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP",
                           scratch=tmp_path / "scratch")
    got = runner.invoke("w2e", "quits", "real", base=True)
    assert got["status"] == "error" and got["exit"] == 1 and got["seconds"] < 30
    assert _wait_dead(int(pidfile.read_text())), "the harness's daemon outlived its scenario"


@pytest.mark.skipif(not hasattr(forced.os, "killpg"), reason="the runner is Linux; process groups are POSIX")
def test_a_leftover_in_its_own_session_is_stopped_by_the_run_marker(tmp_path):
    scratch = tmp_path / "scratch"
    mine = forced.subprocess.Popen(
        [forced.sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True,
        env=dict(forced.os.environ, PSEUDOLIFE_MCP_DATA_DIR=str(scratch / "board" / "identity" / "py" / "data")))
    other = forced.subprocess.Popen(
        [forced.sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True,
        env=dict(forced.os.environ, PSEUDOLIFE_MCP_DATA_DIR=str(tmp_path / "elsewhere" / "data")))
    try:
        runner = forced.Runner(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=scratch)
        assert runner.reap_leftovers() == [mine.pid]
        assert _wait_dead(mine.pid) and _alive(other.pid)  # only this run's marker
    finally:
        for p in (mine, other):
            p.kill()
            p.wait()


def test_each_attempt_records_the_oom_kills_it_saw(tmp_path):
    counts = iter([5, 7])

    class Counting(forced.Runner):
        def _run(self, argv, env):
            return 1, "Traceback\nConnectionRefusedError: [Errno 111] Connection refused\n"

    runner = Counting(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP",
                      scratch=tmp_path / "scratch", oom_kills=lambda: next(counts))
    got = runner.invoke("w2e", "x", "real", base=True)
    assert got["status"] == "error" and got["oom_kills"] == 2


def test_every_attempt_sweeps_the_databases_a_failed_harness_left(tmp_path):
    """A harness that raised never reached its own drops: board/identity's
    two copies of the bank outlived it (daemon box, 2026-11-07)."""
    swept = []

    class Quits(forced.Runner):
        def _run(self, argv, env):
            return 1, "Traceback\nConnectionRefusedError\n"

    runner = Quits(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                   sweep=lambda: swept.append(1) or 2, oom_kills=lambda: 0)
    runner.invoke("w2e", "x", "real", base=True)
    assert swept == [1]
    assert "swept 2 leftover disposable databases" in (tmp_path / "out" / "w2e" / "real" / "x.log").read_text()


@pytest.mark.parametrize("suite,name,want", [
    ("w2e", "judge-merge-tiebreak", "quiet"), ("w2e", "curation-auto-fold", "quiet"),
    ("w2e", "sweep-analyzer-tick", "quiet"), ("w2e", "console-dream-run-gates", "quiet"),
    ("w2e", "dream-deep-apply", "main"), ("w2e", "curation-shadow", "main"),
    ("board", "judge-merge-x", "main"),
])
def test_queue_driven_scenarios_take_the_quiet_base(suite, name, want):
    assert forced.base_for(suite, name)[0] == want


def test_a_quiet_scenario_runs_its_real_attempts_on_the_quiet_base(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    seen = []

    class Records(forced.Runner):
        def _run(self, argv, env):
            seen.append(env.get("PL_HARNESS_BASE_DATABASE"))
            return 0, ""

    runner = Records(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                     oom_kills=lambda: 0)
    assert runner.invoke("w2e", "judge-merge-auto", "real", base=True)["base"] == "quiet"
    assert runner.invoke("w2e", "store-defaults", "real", base=True)["base"] == "main"
    assert runner.invoke("w2e", "judge-merge-auto", "synthetic", base=False)["base"] is None
    assert seen == ["pl_cf_mir_seed_quiet", "pl_cf_mir_seed", None]


def test_the_summary_says_which_scenarios_used_the_quiet_base_and_why(tmp_path):
    answers = {"judge-merge-auto": {"real": dict(PASS, base="quiet")}, "clean": {"real": dict(PASS, base="main")}}
    report, _ = forced.run(FakeRunner(tmp_path, answers), ["w2e"], None, names=lambda s: list(answers))
    quiet = forced.quiet_base_use(report)
    assert quiet["scenarios"] == ["w2e/judge-merge-auto"] and "backlog" in quiet["why"]


def _main_env(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_TEST_PG_HOST_PORT", "mirror-pg:5432")
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")


def test_a_run_needing_the_quiet_base_refuses_a_stale_or_missing_copy(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "base_comments", lambda: ("pl_cf_mir seed from new at u",
                                                          "quiet copy of: pl_cf_mir seed from old at t"))
    monkeypatch.setattr(forced, "run", lambda *a, **k: pytest.fail("ran on a stale quiet copy"))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e"]) == 3


def test_a_run_needing_no_quiet_scenario_skips_the_preflight(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    monkeypatch.setattr(forced, "base_comments", lambda: pytest.fail("checked the quiet copy"))
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: ({"suites": {}, "not_forced": {},
                                                                 "complete": True}, {}))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e",
                        "--only", "store-defaults"]) == 0


def test_a_run_of_only_real_bank_excluded_scenarios_needs_no_quiet_copy(tmp_path, monkeypatch):
    """Review (Codex, Opus): sweep-analyzer-tick matches a quiet prefix, but
    its real-bank arms never run, so a missing quiet copy must not refuse it;
    with a quiet scenario beside it the preflight still runs (near miss)."""
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names",
                        lambda suite: ["sweep-analyzer-tick", "judge-merge-auto"])
    monkeypatch.setattr(forced, "base_comments", lambda: ("pl_cf_mir seed from new at u", None))
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: ({"suites": {}, "not_forced": {},
                                                                 "complete": True}, {}))
    out = ["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e", "--only"]
    assert forced.main(out + ["sweep-analyzer-tick"]) == 0
    assert forced.main(out + ["sweep-analyzer-tick", "judge-merge-auto"]) == 3


def test_an_unmeasured_oom_count_stays_null(tmp_path):
    answers = {"x": {"real": {"status": "error", "diffs": [], "seconds": 1, "oom_kills": None},
                     "real-retry": {"status": "error", "diffs": [], "seconds": 1, "oom_kills": None},
                     "synthetic": {"status": "pass", "diffs": [], "seconds": 1, "oom_kills": None}}}
    report, _ = forced.run(FakeRunner(tmp_path, answers), ["w2e"], None, names=lambda s: ["x"])
    assert report["suites"]["w2e"]["rows"][0]["oom_kills"] is None
    assert forced.counts(report)["w2e"]["errors_with_oom"] is None  # not measured, not "none"


def test_each_run_reaps_only_under_its_own_marker(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    seen = []
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: seen.append(runner.scratch) or (
        {"suites": {}, "not_forced": {}, "complete": True}, {}))
    for _ in range(2):
        forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e", "--only", "store-defaults"])
    assert len(set(seen)) == 2 and all(p.parent == Path("/tmp/forced") for p in seen)


def test_the_summary_names_the_fixture_a_real_entity_stands_in_for(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: ({"suites": {}, "not_forced": {},
                                                                 "complete": True}, {}))
    forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e", "--only", "store-defaults"])
    notes = json.loads((tmp_path / "o" / "summary.json").read_text())["fixture_notes"]
    assert "release-checklist" in notes["w2e/dream-deep-exclusion"]


def test_sweep_analyzer_tick_skips_only_its_real_bank_arms(tmp_path):
    """Conductor decision (option 2), 2026-11-08: the scenario rests on one
    analyzer batch covering every entity, which a real bank cannot hold, so
    its real-bank arms do not run. Its synthetic arm still does, and every
    other scenario, the analyzer's sweep siblings included, runs as before."""
    answers = {"sweep-analyzer-tick": {"synthetic": PASS},
               "sweep-deep-tick": {"real": PASS},
               "sweep-dream-auto": {"real": DIFF, "real-retry": DIFF, "synthetic": PASS}}
    runner = FakeRunner(tmp_path, answers)
    report, private = forced.run(runner, ["w2e"], None, names=lambda s: list(answers))
    rows = {r["scenario"]: r for r in report["suites"]["w2e"]["rows"]}
    assert [c for c in runner.calls if c[1] == "sweep-analyzer-tick"] == [
        ("w2e", "sweep-analyzer-tick", "synthetic", False)]
    assert rows["sweep-analyzer-tick"]["class"] == "excluded"
    assert ("w2e", "sweep-deep-tick", "real", True) in runner.calls
    assert rows["sweep-deep-tick"]["class"] == "pass" and rows["sweep-dream-auto"]["class"] == "new"
    counted = forced.counts(report)["w2e"]
    assert counted["excluded"] == 1 and counted["pass"] == 1 and counted["new"] == 1
    assert "every entity" in forced.REAL_BANK_EXCLUDED["w2e/sweep-analyzer-tick"]
    assert list(forced.REAL_BANK_EXCLUDED) == ["w2e/sweep-analyzer-tick"]


def test_an_excluded_scenario_whose_synthetic_arm_fails_is_an_error(tmp_path):
    """The near miss: the exclusion covers the real bank only, so a failing
    synthetic arm is not hidden behind it."""
    for synthetic in (DIFF, {"status": "error", "diffs": [], "seconds": 1.0}):
        runner = FakeRunner(tmp_path, {"sweep-analyzer-tick": {"synthetic": synthetic}})
        report, private = forced.run(runner, ["w2e"], None, names=lambda s: ["sweep-analyzer-tick"])
        assert report["suites"]["w2e"]["rows"][0]["class"] == "error"
        assert "w2e/sweep-analyzer-tick" in private


def test_the_summary_lists_the_real_bank_exclusions(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: ({"suites": {}, "not_forced": {},
                                                                 "complete": True}, {}))
    forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e", "--only", "store-defaults"])
    summary = json.loads((tmp_path / "o" / "summary.json").read_text())
    assert summary["real_bank_exclusions"] == forced.REAL_BANK_EXCLUDED
    assert summary["accepted_limits"] == forced.ACCEPTED_OOM


def test_an_attempt_that_does_not_pass_keeps_its_daemons_logs(tmp_path):
    """The harness traceback alone did not say which daemon died or why
    (board's ConnectionRefused, 2026-11-07): the daemons' own logs are kept,
    on the host with the private report."""
    class Logs(forced.Runner):
        def __init__(self, *a, status, **k):
            super().__init__(*a, **k)
            self.status = status

        def _run(self, argv, env):
            home = Path(env["PL_HARNESS_SCRATCH"]) / "w2f-x-python"
            home.mkdir(parents=True)
            (home / "daemon.log").write_text("Traceback: the python daemon's own error\n")
            return (0, "") if self.status == "pass" else (1, "Traceback\nConnectionRefusedError\n")

    for status in ("pass", "error"):
        out = tmp_path / status
        Logs(Path("rust-bin"), out, 60, tmp_path / "STOP", scratch=tmp_path / f"s-{status}",
             oom_kills=lambda: 0, status=status).invoke("w2e", "x", "real", base=True)
        kept = list((out / "w2e" / "real").glob("x.daemon-logs/*/daemon.log"))
        assert bool(kept) == (status == "error"), status
    assert "own error" in kept[0].read_text()


def _logging_runner(tmp_path, status, text="Traceback: the python daemon's own error\n"):
    class Logs(forced.Runner):
        def _run(self, argv, env):
            home = Path(env["PL_HARNESS_SCRATCH"]) / "w2f-x-python"
            home.mkdir(parents=True, exist_ok=True)
            (home / "daemon.log").write_text(text)
            return (0, "") if status == "pass" else (1, "Traceback\nConnectionRefusedError\n")
    return Logs(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                oom_kills=lambda: 0)


def test_a_log_that_cannot_be_kept_never_ends_the_run(tmp_path, monkeypatch):
    def full(src, dst, *a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(forced.shutil, "copy2", full)
    monkeypatch.setattr(forced.shutil, "copyfileobj", full)
    got = _logging_runner(tmp_path, "error").invoke("w2e", "x", "real", base=True)
    assert got["status"] == "error"
    assert "kept no daemon log" in (tmp_path / "out" / "w2e" / "real" / "x.log").read_text()
    assert not (tmp_path / "s" / "w2e" / "x").exists()  # the scratch is still removed


def test_a_later_attempt_replaces_the_logs_an_earlier_one_kept(tmp_path):
    _logging_runner(tmp_path, "error").invoke("w2e", "x", "real", base=True)
    _logging_runner(tmp_path, "pass").invoke("w2e", "x", "real", base=True)
    assert not (tmp_path / "out" / "w2e" / "real" / "x.daemon-logs").exists()


def test_a_kept_log_is_its_tail(tmp_path):
    _logging_runner(tmp_path, "error", text="x" * (forced.KEPT_LOG_BYTES + 100) + "END\n").invoke(
        "w2e", "x", "real", base=True)
    kept = next((tmp_path / "out" / "w2e" / "real").glob("x.daemon-logs/*/daemon.log")).read_text()
    assert kept.startswith("[truncated") and kept.endswith("END\n") and len(kept) < forced.KEPT_LOG_BYTES + 200


@pytest.mark.parametrize("name", ["junk-auto", "junk-shadow", "junk-auto-reopen", "link-auto", "link-shadow",
                                  "link-two-vote", "link-two-vote-same-model", "link-auto-reopen",
                                  "hook-session-end-dream", "cli-episode-end-dream"])
def test_the_judge_and_session_end_dream_scenarios_take_the_quiet_base(name):
    """On the real bank's copy these judged or dreamed its backlog (276 and
    376 proposals pending a judge, 3,915 entries pending a dream): Rust's
    judges reached it before Python's, and the session-end dream expects no
    pending entry (forced run, 2026-11-07). Conductor decision, extending D1."""
    assert forced.base_for("w2e", name)[0] == "quiet"


@pytest.mark.parametrize("name", ["hook-session-end", "cli-episode-end", "linked-reads", "junkyard"])
def test_near_miss_names_keep_the_main_base(name):
    assert forced.base_for("w2e", name)[0] == "main"


# ---- real-bank accommodations: known limits and the dream REST timeout ----

def _rows_for(tmp_path, answers, oom):
    class Kills(FakeRunner):
        def invoke(self, suite, name, label, base):
            out = super().invoke(suite, name, label, base)
            return {**out, "oom_kills": oom.get((name, label), 0)}
    report, _ = forced.run(Kills(tmp_path, answers), ["w2e"], None, names=lambda s: list(answers))
    return {r["scenario"]: r for r in report["suites"]["w2e"]["rows"]}, report


def test_a_known_python_memory_limit_is_accepted_not_an_error(tmp_path):
    """Maintainer decision (c), 2026-11-08: candidate-auto and -reopen
    exceed the runner's memory on Python at the harness's -1.0 floors."""
    err = {"status": "error", "diffs": [], "error_tail": ["IncompleteRead"], "seconds": 1.0}
    answers = {"candidate-auto": {"real": err, "real-retry": err, "synthetic": PASS},
               "dream-deep-dry": {"real": err, "real-retry": err, "synthetic": PASS},
               "candidate-auto-reopen": {"real": err, "real-retry": err, "synthetic": PASS}}
    rows, report = _rows_for(tmp_path, answers, {("candidate-auto", "real"): 1,
                                                 ("candidate-auto", "real-retry"): 1,
                                                 ("dream-deep-dry", "real"): 1,
                                                 ("dream-deep-dry", "real-retry"): 1,
                                                 ("candidate-auto-reopen", "real"): 1,
                                                 ("candidate-auto-reopen", "synthetic"): 1})
    assert rows["candidate-auto"]["class"] == "accepted"
    # Near misses: an OOM in a scenario not listed, and a listed one whose
    # real retry failed without an OOM (the first attempt's and the small
    # synthetic arm's OOM do not excuse it).
    assert rows["dream-deep-dry"]["class"] == "error"
    assert rows["candidate-auto-reopen"]["class"] == "error"
    counted = forced.counts(report)["w2e"]
    assert counted["accepted"] == 1 and counted["error"] == 2
    assert "Python" in forced.ACCEPTED_OOM["w2e/candidate-auto"]


def test_real_bank_arms_get_the_longer_dream_rest_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    monkeypatch.delenv("PL_HARNESS_DREAM_REST_TIMEOUT", raising=False)
    seen = []

    class Records(forced.Runner):
        def _run(self, argv, env):
            seen.append(env.get("PL_HARNESS_DREAM_REST_TIMEOUT"))
            return 0, ""

    runner = Records(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                     oom_kills=lambda: 0)
    runner.invoke("w2e", "console-dream-run", "real", base=True)
    runner.invoke("w2e", "console-dream-run", "synthetic", base=False)
    assert seen == [str(forced.DREAM_REST_TIMEOUT), None]


def test_dream_rest_calls_take_the_timeout_and_others_keep_the_default(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import w2e_episodes
    from types import SimpleNamespace
    calls = []
    fake = SimpleNamespace(AUTH=("authorization", "x"), resolve_args=lambda a, d: a,
                           w1a=SimpleNamespace(call=lambda port, m, p, h, b, **kw: calls.append(
                               (p, kw.get("timeout"))) or {"status": 200, "json": {}}))
    monkeypatch.setattr(w2e_episodes, "_before_map", lambda dsn: {})
    monkeypatch.setenv("PL_HARNESS_DREAM_REST_TIMEOUT", "600")
    w2e_episodes.run_rest({"method": "POST", "path": "/api/dream/run"}, 1, "dsn", fake)
    w2e_episodes.run_rest({"method": "GET", "path": "/api/facts"}, 1, "dsn", fake)
    monkeypatch.delenv("PL_HARNESS_DREAM_REST_TIMEOUT")
    w2e_episodes.run_rest({"method": "POST", "path": "/api/dream/run"}, 1, "dsn", fake)
    assert calls == [("/api/dream/run", 600.0), ("/api/facts", 120.0), ("/api/dream/run", 120.0)]
