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
    assert counts == {"planned": 4, "run": 4, "pass": 1, "transient": 1, "preexisting": 1, "new": 1,
                      "error": 0, "differences": 3, "new_scenarios": ["fresh"]}
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
                           sweep=lambda: swept.append(1) or 2)
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
