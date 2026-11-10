"""The forced real-data runner's refusal, classification and report, without daemons."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forced  # noqa: E402

PASS = {"status": "pass", "diffs": [], "seconds": 1.0}
DIFF = {"status": "diff", "diffs": ["DIFF [s] step 1 x: db /rows/public.entries"], "seconds": 1.0,
        "diffs_complete": True}


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
    """An answer is either the attempt itself, or a harness run (``exit``,
    ``log`` and the ``json`` its ``--out`` holds, None for none) that goes
    through the real ``invoke``."""

    def __init__(self, tmp: Path, answers: dict, stop_after: int | None = None):
        # Its own scratch root: the real invoke reaps processes marked under it.
        super().__init__(Path("rust-bin"), tmp, 60, tmp / "STOP", scratch=tmp / "scratch",
                         oom_kills=lambda: 0)
        self.answers, self.calls, self.stop_after = answers, [], stop_after

    def invoke(self, suite, name, label, base):
        self.calls.append((suite, name, label, base))
        answer = self.answers[name][label]
        if "log" not in answer:
            return dict(answer)
        self.answer = answer
        return super().invoke(suite, name, label, base)

    def _run(self, argv, env):
        out = Path(argv[argv.index("--out") + 1])
        written = self.answer.get("json")
        if written is not None:
            out.write_text(written if isinstance(written, str) else json.dumps(written), encoding="utf-8")
        return self.answer.get("exit", 1), self.answer["log"]

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
    real = {"status": "diff", "diffs_complete": True, "diffs": [
        'DIFF [store-reaper] step 1 {"op": "tool"}: db /rows/public.episodes/rows: row only in python: [1]']}
    same = {"status": "diff", "diffs_complete": True, "diffs": [
        'DIFF [store-reaper] step 1 {"op": "x"}: db /rows/public.episodes/rows: row only in rust: [2]']}
    elsewhere = {"status": "diff", "diffs": ['DIFF [store-reaper] step 2 {}: /json/results: python 1 vs rust 2']}
    broken = {"status": "error", "diffs": [], "error_tail": ["Traceback", "OSError: port in use"]}
    assert forced.signatures(real) == {("1", "/rows/public.episodes/rows")}
    assert forced.classify(real, real, same) == "preexisting"
    assert forced.classify(real, real, elsewhere) == "new"
    assert forced.classify(real, real, broken) == "new"  # an unrelated synthetic failure excuses nothing
    # A scenario no attempt could compare is an error, never preexisting or a pass.
    assert forced.classify(broken, broken, broken) == "error"


# The other suites' line formats (forced run, daemon box, 2026-11-09): each
# pair differs only in values and list indexes, so it is the same place.
SAME_PLACE = {
    "w2d db field": (
        "DB DIFF [default] public.retrieval_events[3].served[0].components.ce: python NoneType null vs rust float 0.12",
        "DB DIFF [default] public.retrieval_events[1].served[2].components.ce: python NoneType null vs rust float 0.5"),
    "w2d case": (
        "DIFF [rrf-pool] rrf 3: ['.entries[0].score: python 0.5 vs rust 0.6', '.entries[1].id: python 7 vs rust 8']",
        "DIFF [rrf-pool] rrf 3: ['.entries[4].score: python 0.1 vs rust 0.2', '.entries[2].id: python 1 vs rust 9']"),
    "w2d coverage": ("DB DIFF [mcp-stats] coverage stats.graduation: 1 < 3",
                     "DB DIFF [mcp-stats] coverage stats.graduation: 0 < 3"),
    "board state": (
        'STATE [wake] after step 13 (B to A anyone): coordination_wakes: row only in python: {"ring_at": "T13+30"}',
        'STATE [wake] after step 13 (B to A anyone): coordination_wakes: row only in rust: {"ring_at": "T13+60"}'),
    "board chain": ("CHAIN [console] python: created_at: value 1794232544.5 does not match the captured clock windows",
                    "CHAIN [console] python: created_at: value 12.0 does not match the captured clock windows"),
    "routes db": ('DB DIFF [tokens] /rows/public.slot_reads/rows: row only in python: ["box", "count", 4, 1.5]',
                  'DB DIFF [tokens] /rows/public.slot_reads/rows: row only in rust: ["box", "count", 4, 2.5]'),
    "routes error": ("DIFF [stamped] scenario error: ['ForeignKeyViolation: key (name)=(uses) (at run.py:12 in x)']",
                     "DIFF [stamped] scenario error: ['ForeignKeyViolation: key (name)=(other) (at run.py:12 in x)']"),
    "board step": ("DIFF [identity] #3 register: ['.json.agent_id: python 1 vs rust 2', '.headers.content-type: a vs b']",
                   "DIFF [identity] #3 register: ['.headers.content-type: c vs d', '.json.agent_id: python 5 vs rust 6']"),
    "w2d status": ("DIFF [mcp-get] get 2: ['status: python 200 vs rust 500', 'body bytes differ']",
                   "DIFF [mcp-get] get 2: ['status: python 404 vs rust 200', 'body bytes differ']"),
}
# Near misses: another field, case, step, table, clock, path or error type.
ELSEWHERE = {
    "w2d db field": "DB DIFF [default] public.retrieval_events[3].served[0].components.dense: python 0.4 vs rust 0.5",
    "w2d case": "DIFF [rrf-pool] rrf 4: ['.entries[0].score: python 0.5 vs rust 0.6']",
    "w2d coverage": "DB DIFF [mcp-stats] coverage trace.reranker_fired: 1 < 3",
    "board state": 'STATE [wake] after step 28 (B to A anyone): coordination_wakes: row only in rust: {"ring_at": "T28+0"}',
    "board chain": "CHAIN [console] python: acquired_at: value 12.0 does not match the captured clock windows",
    "routes db": 'DB DIFF [tokens] /rows/public.retrieval_events/rows: row only in rust: [1, "probe"]',
    "routes error": "DIFF [stamped] scenario error: ['DuplicateTable: relation already exists (at run.py:9 in x)']",
    "board step": "DIFF [identity] #4 register: ['.json.agent_id: python 1 vs rust 2']",
    "w2d status": "DIFF [mcp-get] get 2: ['content-type: a vs b']",
}


def test_each_case_item_is_a_place_of_its_own():
    """Review (Opus): a case line took only its first item's dotted path, so
    its other items had no place of their own. Every printed item counts,
    keys with a hyphen and items that are not paths included. (classify()
    calls a scenario preexisting only when every place is on the synthetic arm.)"""
    real = {"status": "diff", "diffs_complete": True, "diffs": [
        "DIFF [mcp-get] get 2: ['status: python 200 vs rust 500', '.json.total: python 1 vs rust 2']"]}
    synthetic = {"status": "diff", "diffs": ["DIFF [mcp-get] get 2: ['.json.total: python 3 vs rust 4']"]}
    assert ("get 2", "status") in forced.signatures(real)
    assert forced.classify(real, real, synthetic) == "new"   # status is not on the synthetic arm
    alone = {"status": "diff", "diffs_complete": True,
             "diffs": ["DIFF [mcp-get] get 2: ['status: python 200 vs rust 500']"]}
    assert forced.classify(alone, alone, synthetic) == "new"           # status is not shared


def test_a_new_place_does_not_hide_behind_a_shared_one():
    """Conductor decision, 2026-11-10: preexisting only when every real-bank
    place is also on the synthetic arm. Sharing one place used to excuse
    every other difference the real bank showed."""
    shared = 'DIFF [store-reaper] step 1 {"op": "tool"}: db /rows/public.episodes/rows: row only in python: [1]'
    extra = 'DIFF [store-reaper] step 3 {"op": "tool"}: db /rows/public.facts/rows: row only in rust: [2]'
    real = {"status": "diff", "diffs": [shared, extra], "diffs_complete": True}
    synthetic = {"status": "diff", "diffs": [shared.replace("[1]", "[9]")]}
    assert forced.classify(real, real, synthetic) == "new"
    # The near miss: every real place on the synthetic arm (which may differ
    # in more places) is preexisting.
    wider = {"status": "diff", "diffs": [shared, extra, 'DIFF [store-reaper] step 4 {}: db /rows/public.meta/rows: x']}
    assert forced.classify(real, real, wider) == "preexisting"


def test_a_scenario_error_keeps_its_harness_location():
    """Review (Opus): the same exception type raised elsewhere in the
    harness is another place."""
    one = {"status": "diff", "diffs_complete": True, "diffs": [
        "DIFF [stamped] scenario error: " + repr(["KeyError: x (case 'seed'; at run.py:12 in prepare_template)"])]}
    other = {"status": "diff", "diffs": [
        "DIFF [stamped] scenario error: ['KeyError: y (at run.py:40 in run_scenario)']"]}
    assert forced.classify(one, one, other) == "new"
    assert forced.signatures(one) == {("", "scenario error KeyError case 'seed'; run.py prepare_template")}
    quoted = {"status": "diff", "diffs": [
        "DIFF [stamped] scenario error: " + repr(["KeyError: x (case \"it's\"; at run.py:12 in prepare_template)"])]}
    assert forced.signatures(quoted) == {("", "scenario error KeyError case \"it's\"; run.py prepare_template")}
    # Review (Codex): a message with double quotes makes repr escape the
    # apostrophes around the case; the location must read the same.
    plain, dq = ({"status": "diff", "diffs_complete": True, "diffs": ["DIFF [stamped] scenario error: " + repr(
        [f"UndefinedTable: relation {name} (case 'seed'; at run.py:12 in prepare_template)"])]}
        for name in ("missing", '"missing"'))
    assert forced.signatures(plain) == forced.signatures(dq)
    assert forced.classify(plain, plain, dq) == "preexisting"


def test_side_items_and_long_items_keep_what_tells_them_apart():
    """Review (Opus): "python: ..." items took the side as their key, and a
    four-word cap merged items differing later (run.py's future-schema
    refusal checks name the header last)."""
    a = {"status": "diff", "diffs": ["DIFF [schema] refusal: " + repr(
        ["python: stats clock 5 outside window", "future-schema refusal has incorrect content-type"])]}
    b = {"status": "diff", "diffs": ["DIFF [schema] refusal: " + repr(
        ["python: snapshot_at 3 outside window", "future-schema refusal has incorrect cache-control"])]}
    assert not forced.signatures(a) & forced.signatures(b)


def test_a_raw_check_keeps_its_path_and_no_value():
    """Review (Opus): run.py's "raw <path>: string not written as
    json.dumps writes it: <first 80 characters>" fell to the word fallback,
    whose longer cap could carry those characters (a cut quote is not
    stripped). It keeps its path; text after a ": " never counts."""
    line = "DIFF [w1a] stats: " + repr(['raw .json.text: string not written as json.dumps writes it: "half a quot'])
    assert forced.signatures({"diffs": [line]}) == {("stats", "raw .json.text")}
    words = "DIFF [w1a] stats: " + repr(["refusal body differs: Mostly Bank Words here", "flag is true"])
    assert forced.signatures({"diffs": [words]}) == {("stats", "refusal body differs"), ("stats", "flag is")}


def test_a_w2e_response_line_names_its_response_path_without_values():
    """Review (Opus): w2e response lines fell back to their first 120
    characters. With a long op that cut came before the response path, so
    every response difference at a step was one place; with a short op it
    carried the values. The place is now the step and the response path."""
    short = 'DIFF [dream-rollback] step 19 {"op": "tool"}: .value.record.age: python "a" vs rust "b"'
    long_op = json.dumps({"op": "tool", "name": "memory_fact_get", "args": {"entity": "Bob", "attribute": "drink"}})
    long = f'DIFF [dream-rollback] step 19 {long_op[:90]}: .value.record.age: python "x" vs rust "y"'
    assert forced.signatures({"diffs": [short]}) == forced.signatures({"diffs": [long]})         == {("19", ".value.record.age")}
    other = f'DIFF [dream-rollback] step 19 {long_op[:90]}: .value.record.value: python "x" vs rust "y"'
    assert forced.signatures({"diffs": [other]}) != forced.signatures({"diffs": [long]})


def test_a_w2e_db_field_line_names_its_field_without_values():
    line = 'DIFF [recall] step 9 {"op": "tool"}: db retrieval_events[3].served[0].components.surprise: python 1 vs rust 2'
    same = 'DIFF [recall] step 9 {"op": "tool"}: db retrieval_events[1].served[2].components.surprise: python 7 vs rust 8'
    assert forced.signatures({"diffs": [line]}) == forced.signatures({"diffs": [same]})         == {("9", "db retrieval_events[].served[].components.surprise")}


def test_a_w2e_expect_line_names_its_side_and_query_without_values():
    line = 'DIFF [s] step 3 {"op": "tool", "name": "memory_dream"}: expect python: SELECT count(*) = 4 FROM outcome_signals'
    same = 'DIFF [s] step 3 {"op": "tool", "name": "memory_dream"}: expect python: SELECT count(*) = 9 FROM outcome_signals'
    assert forced.signatures({"diffs": [line]}) == forced.signatures({"diffs": [same]})         == {("3", "expect python SELECT count(*) = FROM outcome_signals")}


def test_a_new_response_path_does_not_hide_behind_a_shared_one():
    shared = 'DIFF [s] step 2 {"op": "tool"}: .value.a: python 1 vs rust 2'
    new_path = 'DIFF [s] step 2 {"op": "tool"}: .value.b: python 1 vs rust 2'
    real = {"status": "diff", "diffs": [shared, new_path], "diffs_complete": True}
    assert forced.classify(real, real, {"status": "diff", "diffs": [shared]}) == "new"
    assert forced.classify(real, real, {"status": "diff", "diffs": [new_path, shared]}) == "preexisting"


@pytest.mark.parametrize("kind", sorted(SAME_PLACE))
def test_every_suites_lines_find_the_same_place_without_their_values(kind):
    """w2d, board and routes lines held no step or db path that signatures()
    read, so it fell back to the line's first 120 characters, values and all:
    a synthetic arm differing in the same place never overlapped, and three
    w2d scenarios read as new (forced run, 2026-11-09)."""
    real, synthetic = ({"status": "diff", "diffs": [line], "diffs_complete": True}
                       for line in SAME_PLACE[kind])
    assert forced.signatures(real) == forced.signatures(synthetic), kind
    assert forced.classify(real, real, synthetic) == "preexisting", kind
    elsewhere = {"status": "diff", "diffs": [ELSEWHERE[kind]]}
    assert forced.classify(real, real, elsewhere) == "new", kind


def test_a_signature_holds_no_value():
    """Signatures name places, never values: no token in which a pair's two
    lines differ (their values and indexes) reaches either signature."""
    for kind, (real, synthetic) in SAME_PLACE.items():
        a, b = (set(re.findall(r"[\w.]+", line)) for line in (real, synthetic))
        places = " ".join(w for _, w in forced.signatures({"diffs": [real, synthetic]}))
        assert not (a ^ b) & set(re.findall(r"[\w.]+", places)), kind


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
    ("w2e", "judge-merge-tiebreak", "quiet"), ("w2e", "curation-auto-fold", "curation"),
    ("w2e", "sweep-analyzer-tick", "quiet"), ("w2e", "console-dream-run-gates", "quiet"),
    ("w2e", "dream-deep-apply", "main"), ("w2e", "curation-shadow", "curation"),
    ("board", "judge-merge-x", "main"), ("board", "curation-shadow", "main"),
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
    assert sorted(forced.REAL_BANK_EXCLUDED) == [
        "w2d_search/hide-superseded", "w2d_search/mcp-stats", "w2e/candidate-auto",
        "w2e/sweep-analyzer-tick"]


def test_the_maintainers_real_bank_exclusions_have_their_reasons():
    """Maintainer decision D2, 2026-11-10: three more scenarios whose premise
    cannot hold on a real bank. candidate-auto-reopen is not excluded: it
    stays an error unless both real attempts hit the accepted OOM."""
    for key, word in (("w2d_search/hide-superseded", "ranking"), ("w2d_search/mcp-stats", "graduation"),
                      ("w2e/candidate-auto", "judged first")):
        assert word in forced.REAL_BANK_EXCLUDED[key] and "2026-11-10" in forced.REAL_BANK_EXCLUDED[key]
    assert "w2e/candidate-auto-reopen" not in forced.REAL_BANK_EXCLUDED


COMMUNITY_ROWS = "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.communities/rows: row only in python: [1]"
COMMUNITY_MISSING = "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.communities/base_missing/count: python 1 vs rust 0"
MEMBERSHIP = "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.entity_communities/rows: row only in rust: [2]"
NODE_COMMUNITY = "DIFF [graph-writes] step 36 {\"op\": \"tool\"}: .value.nodes[3].community: python 4 vs rust 5"
INSIGHT = "DIFF [graph-writes] step 36 {\"op\": \"tool\"}: .value.graph_insight.communities: python 9 vs rust 8"
ROUTES_COMMUNITY = "DB DIFF [reaper] /rows/public.communities/rows: row only in rust: [7, \"x\", 1, 0.5, 2.0]"


def _community_rows(tmp_path, first, retry=None, **retry_fields):
    answers = {"graph-writes": {"real": {"status": "diff", "diffs": first, "seconds": 1.0, "exit": 1,
                                         "diffs_complete": True},
                                "real-retry": {"status": "diff", "diffs": retry or first, "seconds": 1.0,
                                               "exit": 1, "diffs_complete": True, **retry_fields},
                                "synthetic": PASS}}
    report, _ = forced.run(FakeRunner(tmp_path, answers), ["w2e"], None, names=lambda s: list(answers))
    return report["suites"]["w2e"]["rows"][0]


def test_a_difference_confined_to_communities_is_accepted(tmp_path):
    """Maintainer decision D1, 2026-11-10: on a real bank's copy the two
    daemons' community partitions differ by Louvain's visiting order (Python
    seed.shuffle, Rust graph order; #1417's divergence entry). A scenario
    whose every difference is in the community tables or a community field
    is accepted, with the reason recorded."""
    row = _community_rows(tmp_path, [COMMUNITY_ROWS, COMMUNITY_MISSING, MEMBERSHIP, NODE_COMMUNITY, INSIGHT])
    assert (row["class"], row.get("accepted_as")) == ("accepted", "community-partition")
    row = _community_rows(tmp_path, [ROUTES_COMMUNITY])
    assert row["class"] == "accepted"
    assert "visiting order" in forced.ACCEPTED_DIVERGENCES["community-partition"]
    assert "2026-11-10" in forced.ACCEPTED_DIVERGENCES["community-partition"]


@pytest.mark.parametrize("other", [
    "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.facts/rows: row only in python: [1]",
    "DIFF [graph-writes] step 36 {\"op\": \"tool\"}: .value.confirmed: only in rust",
    "DIFF [graph-writes] step 36 {\"op\": \"tool\"}: .value.community_count: python 1 vs rust 2",
    "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.communities_backup/rows: row only in rust: [1]",
])
def test_community_plus_any_other_difference_stays_new(tmp_path, other):
    """The confinement is exact: one difference anywhere else, in the first
    attempt or the retry, keeps the scenario new."""
    assert _community_rows(tmp_path, [COMMUNITY_ROWS, NODE_COMMUNITY, other])["class"] == "new"
    assert _community_rows(tmp_path, [COMMUNITY_ROWS], retry=[COMMUNITY_ROWS, other])["class"] == "new"


def test_the_summary_lists_the_accepted_divergences(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: ({"suites": {}, "not_forced": {},
                                                                 "complete": True}, {}))
    forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e", "--only", "store-defaults"])
    summary = json.loads((tmp_path / "o" / "summary.json").read_text())
    assert summary["accepted_divergences"] == forced.ACCEPTED_DIVERGENCES


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


@pytest.mark.parametrize("name", ["curation-shadow", "curation-auto", "curation-auto-distinct",
                                  "curation-auto-distinct-reopen", "curation-auto-fold"])
def test_the_curation_scenarios_take_the_curation_base(name):
    """The curation judge takes the 8 most similar pairs over every current
    lesson and world fact: on the real bank's copy (3,748 lessons, 411 world
    facts) real pairs outranked the scenario's, which never got judged
    (forced run, 2026-11-08). Conductor decision (c)."""
    which, why = forced.base_for("w2e", name)
    assert which == "curation" and "lesson" in why
    # Not also listed for the quiet copy, where it would land if the order flipped.
    assert not any(name.startswith(p) for p in forced.QUIET_PREFIXES["w2e"])


def test_a_curation_scenario_runs_its_real_attempts_on_the_curation_base(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    seen = []

    class Records(forced.Runner):
        def _run(self, argv, env):
            seen.append(env.get("PL_HARNESS_BASE_DATABASE"))
            return 0, ""

    runner = Records(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                     oom_kills=lambda: 0)
    assert runner.invoke("w2e", "curation-shadow", "real", base=True)["base"] == "curation"
    assert runner.invoke("w2e", "curation-shadow", "synthetic", base=False)["base"] is None
    assert seen == ["pl_cf_mir_seed_curation", None]


def test_the_summary_says_which_scenarios_used_the_curation_base_and_why(tmp_path):
    answers = {"curation-shadow": {"real": dict(PASS, base="curation")},
               "judge-merge-auto": {"real": dict(PASS, base="quiet")}}
    report, _ = forced.run(FakeRunner(tmp_path, answers), ["w2e"], None, names=lambda s: list(answers))
    used = forced.curation_base_use(report)
    assert used["scenarios"] == ["w2e/curation-shadow"] and used["database"] == "pl_cf_mir_seed_curation"
    assert forced.quiet_base_use(report)["scenarios"] == ["w2e/judge-merge-auto"]


def test_a_run_needing_the_curation_base_refuses_a_stale_or_missing_copy(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["curation-shadow"])
    monkeypatch.setattr(forced, "curation_comments", lambda: ("pl_cf_mir seed from new at u",
                                                              "curation copy of: pl_cf_mir seed from old at t"))
    monkeypatch.setattr(forced, "run", lambda *a, **k: pytest.fail("ran on a stale curation copy"))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e",
                        "--only", "curation-shadow"]) == 3


def test_a_run_needing_no_curation_scenario_skips_its_preflight(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    monkeypatch.setattr(forced, "curation_comments", lambda: pytest.fail("checked the curation copy"))
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: ({"suites": {}, "not_forced": {},
                                                                 "complete": True}, {}))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e",
                        "--only", "store-defaults"]) == 0


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
    # candidate-auto's real-bank arms are excluded since 2026-11-10 (D2), so
    # the rule is pinned on candidate-auto-reopen, which still runs them.
    err = {"status": "error", "diffs": [], "error_tail": ["IncompleteRead"], "seconds": 1.0}
    answers = {"dream-deep-dry": {"real": err, "real-retry": err, "synthetic": PASS},
               "candidate-auto-reopen": {"real": err, "real-retry": err, "synthetic": PASS}}
    rows, report = _rows_for(tmp_path, answers, {("dream-deep-dry", "real"): 1,
                                                 ("dream-deep-dry", "real-retry"): 1,
                                                 ("candidate-auto-reopen", "real"): 1,
                                                 ("candidate-auto-reopen", "real-retry"): 1})
    assert rows["candidate-auto-reopen"]["class"] == "accepted"
    # Near miss: an OOM in a scenario not listed.
    assert rows["dream-deep-dry"]["class"] == "error"
    counted = forced.counts(report)["w2e"]
    assert counted["accepted"] == 1 and counted["error"] == 1
    assert "Python" in forced.ACCEPTED_OOM["w2e/candidate-auto-reopen"]
    # Near miss: the listed one whose real retry failed without an OOM (the
    # first attempt's and the small synthetic arm's OOM do not excuse it).
    rows, _ = _rows_for(tmp_path, {"candidate-auto-reopen": answers["candidate-auto-reopen"]},
                        {("candidate-auto-reopen", "real"): 1, ("candidate-auto-reopen", "synthetic"): 1})
    assert rows["candidate-auto-reopen"]["class"] == "error"


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


def test_an_empty_only_refuses_instead_of_running_everything(tmp_path, monkeypatch, capsys):
    """``--only`` given no names (a list that came out empty, as a broken
    name-computing step yields) used to read as no filter and plan every
    scenario. It now refuses, naming the cause and the fix."""
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults", "judge-merge-auto"])
    monkeypatch.setattr(forced, "base_comments", lambda: pytest.fail("got past the refusal"))
    monkeypatch.setattr(forced, "run", lambda *a, **k: pytest.fail("ran with an empty --only"))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e", "--only"]) == 2
    err = capsys.readouterr().err
    assert "--only" in err and "no scenario" in err and "omit --only" in err
    assert not (tmp_path / "o").exists()


def test_no_only_still_plans_every_scenario(tmp_path, monkeypatch):
    """The near miss: leaving ``--only`` out is the legitimate way to run all."""
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    seen = []

    def run(runner, suites, only, **_):
        seen.append(only)
        return {"suites": {}, "not_forced": {}, "complete": True}, {}

    monkeypatch.setattr(forced, "run", run)
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e"]) == 0
    assert seen == [None]


def test_an_empty_suites_refuses_instead_of_an_empty_complete_run(tmp_path, monkeypatch, capsys):
    """``--suites`` given no names planned nothing and reported the run complete."""
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "run", lambda *a, **k: pytest.fail("ran with an empty --suites"))
    assert forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites"]) == 2
    err = capsys.readouterr().err
    assert "--suites" in err and "omit --suites" in err
    assert not (tmp_path / "o").exists()


def test_a_real_attempt_with_no_places_is_never_preexisting():
    """An errored first attempt has no places, and the empty set is a subset
    of any synthetic arm's: it must not read as preexisting."""
    errored = {"status": "error", "diffs": [], "error_tail": ["x"]}
    retried = {"status": "diff", "diffs": ["DIFF [s] step 1 {}: db /rows/public.facts/rows: x"],
               "diffs_complete": True}
    synthetic = {"status": "diff", "diffs": ["DIFF [s] step 1 {}: db /rows/public.facts/rows: y"]}
    assert forced.classify(errored, retried, synthetic) == "new"


@pytest.mark.parametrize("diff, place", [
    ("entries[3].score: python 0.5 vs rust 0.6", "entries[].score"),
    ("surprise: python 1 vs rust 2", "surprise"),
    ("value.would_orphan: python true vs rust false", "value.would_orphan"),
    ("db retrieval_events[2].served[0].components.bm25: python 1 vs rust 2",
     "db retrieval_events[].served[].components.bm25"),
    ("candidate generation python: bank 5, watermark 4", "candidate generation python"),
])
def test_every_w2e_diff_shape_names_its_place_without_values(diff, place):
    """Review (Opus): compare_numeric, idle-clock, extractor, warmup and
    fingerprint lines still fell back to their first 120 characters. Every
    w2e diff now names the text before its first ": ", values removed."""
    long_op = json.dumps({"op": "tool", "name": "memory_recall",
                          "args": {"query": "a long query that runs past the ninety character cut", "top_k": 5}})
    assert len(long_op) > 90
    for op in ('{"op": "tool"}', long_op[:90]):
        assert forced.signatures({"diffs": [f"DIFF [s] step 7 {op}: {diff}"]}) == {("7", place)}


def test_an_expect_keeps_identifiers_and_drops_literals():
    """Review (Opus): digits inside identifiers (e2.id) are part of the
    query; quoted literals and numbers are values."""
    line = ('DIFF [s] step 2 {"op": "sql"}: expect rust: SELECT count(*) = 3 FROM edges e2 '
            "WHERE e2.id > 10 AND e2.kind = 'x'")
    assert forced.signatures({"diffs": [line]}) == {
        ("2", "expect rust SELECT count(*) = FROM edges e2 WHERE e2.id > AND e2.kind =")}


def test_an_op_whose_text_holds_a_colon_still_finds_its_diff():
    """Review (Opus): the colon search took ": t" inside a stored note
    ("Completely unrelated: the tea kettle ...", w2e.py) for the start of the
    diff, so every difference at that step was one place. The printed op is
    now decoded to find where it ends, whole or cut at 90 characters."""
    short = json.dumps({"op": "tool", "name": "memory_store", "args": {"text": "unrelated: the kettle"}})
    long_op = json.dumps({"op": "tool", "name": "memory_store",
                          "args": {"text": "Completely unrelated: the tea kettle whistles at six every morning"}})
    assert len(short) <= 90 < len(long_op)
    for op in (short, long_op[:90]):
        one = f"DIFF [store] step 4 {op}: .value.stored: python true vs rust false"
        two = f"DIFF [store] step 4 {op}: .value.id: python 1 vs rust 2"
        assert forced.signatures({"diffs": [one]}) == {("4", ".value.stored")}
        assert forced.signatures({"diffs": [one]}) != forced.signatures({"diffs": [two]})


@pytest.mark.parametrize("errored", ["real", "real-retry"])
def test_a_community_difference_beside_an_errored_attempt_stays_new(tmp_path, errored):
    """Review (Opus confidence 90, Codex P1): with one real attempt showing
    only community differences and the other errored, the errored one was
    filtered out and the scenario accepted, hiding a crash or timeout. Both
    real attempts must complete with community-only differences."""
    diff = {"status": "diff", "diffs": [COMMUNITY_ROWS], "seconds": 1.0, "exit": 1, "diffs_complete": True}
    err = {"status": "error", "diffs": [], "error_tail": ["ConnectionRefusedError"], "seconds": 1.0, "exit": 2}
    answers = {"graph-writes": {"real": diff, "real-retry": diff, "synthetic": PASS}}
    answers["graph-writes"][errored] = err
    report, _ = forced.run(FakeRunner(tmp_path, answers), ["w2e"], None, names=lambda s: list(answers))
    row = report["suites"]["w2e"]["rows"][0]
    assert row["class"] == "new" and "accepted_as" not in row


@pytest.mark.parametrize("cut", [{"exit": None}, {"exit": -9}, {"oom_kills": 1}])
def test_a_community_difference_from_an_attempt_that_did_not_finish_stays_new(tmp_path, cut):
    """Review (Opus, confidence 85): an attempt that timed out (exit None),
    was killed by a signal or by the OOM killer after printing community
    differences still reads as "diff". Accepting it would hide the cut."""
    row = _community_rows(tmp_path, [COMMUNITY_ROWS], **cut)
    assert row["class"] == "new" and "accepted_as" not in row


# ---- the printed DIFF lines are capped; --out holds every difference ----

GRAPH_OP = {"op": "tool", "name": "memory_graph_relate", "args": {"source": "Ada", "target": "Bob"}}


def _w2e_attempt(scenario: str, steps: list, exit: int = 1) -> dict:
    """A w2e harness run: its final --out JSON and the log it prints from it,
    as w2e.py prints (``s["diffs"][:6]`` per step)."""
    data = {"complete": True, "planned_scenarios": [scenario], "summary": {},
            "scenarios": [{"scenario": scenario, "mutant": None, "stats": {},
                           "steps": [{"step": i, "op": op, "diffs": diffs} for i, op, diffs in steps]}]}
    printed = [f"DIFF [{scenario}] step {s['step']} {json.dumps(s['op'])[:90]}: {d}"
               for s in data["scenarios"][0]["steps"] for d in s["diffs"][:6]]
    return {"exit": exit, "log": "\n".join(printed + ['{"scenarios": 1}']) + "\n", "json": data}


def _row(tmp_path, answers):
    report, _ = forced.run(FakeRunner(tmp_path, answers), ["w2e"], None, names=lambda s: list(answers))
    return report["suites"]["w2e"]["rows"][0]


COMMUNITIES = [f"db /rows/public.communities/rows: row only in python: [{i}, \"c{i}\"]" for i in range(7)]
FACTS = "db /rows/public.facts/rows: row only in rust: [1, \"Ada\", \"drink\"]"
EPISODES = [f"db /rows/public.episodes/rows: row only in python: [{i}]" for i in range(6)]


def test_a_difference_the_printout_cut_keeps_a_community_scenario_new(tmp_path):
    """w2e prints 6 differences per step. With 7 community differences and a
    facts one at the same step, the facts line was never printed, so every
    printed difference was a community one and the scenario was accepted. The
    runner now reads every difference from the harness's --out JSON."""
    attempt = _w2e_attempt("graph-writes", [(35, GRAPH_OP, COMMUNITIES + [FACTS])])
    assert "public.facts" not in attempt["log"] and attempt["log"].count("public.communities") == 6
    row = _row(tmp_path, {"graph-writes": {"real": attempt, "real-retry": attempt, "synthetic": PASS}})
    assert row["class"] == "new" and "accepted_as" not in row
    assert row["differences"] == 8  # every difference, not the 6 printed


def test_a_community_only_scenario_beyond_the_printout_is_still_accepted(tmp_path):
    """The positive control: all 7 differences are community ones, so the
    full list from --out still accepts it, and counts all 7."""
    attempt = _w2e_attempt("graph-writes", [(35, GRAPH_OP, COMMUNITIES)])
    row = _row(tmp_path, {"graph-writes": {"real": attempt, "real-retry": attempt, "synthetic": PASS}})
    assert (row["class"], row.get("accepted_as"), row["differences"]) == ("accepted", "community-partition", 7)


def test_a_real_bank_place_the_printout_cut_is_not_excused_as_preexisting(tmp_path):
    """The synthetic arm differs in the episodes rows; the real arm also in
    the facts rows, which its printout cut. Its printed places were all on
    the synthetic arm, so it read as preexisting."""
    real = _w2e_attempt("store-reaper", [(1, GRAPH_OP, EPISODES + [FACTS])])
    synthetic = _w2e_attempt("store-reaper", [(1, GRAPH_OP, EPISODES[:1])])
    assert "public.facts" not in real["log"]
    assert _row(tmp_path, {"store-reaper": {"real": real, "real-retry": real,
                                            "synthetic": synthetic}})["class"] == "new"
    # Near miss: the synthetic arm differs there too (also beyond its printout).
    wider = _w2e_attempt("store-reaper", [(1, GRAPH_OP, EPISODES + [FACTS])])
    assert _row(tmp_path, {"store-reaper": {"real": real, "real-retry": real,
                                            "synthetic": wider}})["class"] == "preexisting"


def test_the_synthetic_arms_full_list_counts_too(tmp_path):
    """The shared place is beyond the synthetic arm's printout: only its full
    list shows the real arm's one place on it."""
    real = _w2e_attempt("store-reaper", [(1, GRAPH_OP, [FACTS])])
    synthetic = _w2e_attempt("store-reaper", [(1, GRAPH_OP, EPISODES + [FACTS])])
    assert "public.facts" not in synthetic["log"]
    assert _row(tmp_path, {"store-reaper": {"real": real, "real-retry": real,
                                            "synthetic": synthetic}})["class"] == "preexisting"


@pytest.mark.parametrize("written", [
    None,                                                     # no --out file
    "{\"complete\": true, \"scenarios\": [",                  # cut mid-write
    {"complete": False, "planned_scenarios": ["x"], "scenarios": []},  # the progress write
    "stale",                                                  # another run's file
])
def test_without_the_full_list_nothing_is_accepted_or_preexisting(tmp_path, written):
    """With no complete --out JSON the printed lines may be cut, so they
    neither accept a community difference nor excuse a place as preexisting."""
    def without(attempt):
        if written == "stale":
            other = _w2e_attempt("graph-writes", [(2, GRAPH_OP, [FACTS])])["json"]
            return {**attempt, "json": other}
        return {**attempt, "json": written}

    community = without(_w2e_attempt("graph-writes", [(35, GRAPH_OP, COMMUNITIES[:2])]))
    row = _row(tmp_path / "c", {"graph-writes": {"real": community, "real-retry": community,
                                                 "synthetic": PASS}})
    assert row["class"] == "new" and "accepted_as" not in row and row["differences"] == 2
    real = without(_w2e_attempt("store-reaper", [(1, GRAPH_OP, EPISODES[:2])]))
    synthetic = _w2e_attempt("store-reaper", [(1, GRAPH_OP, EPISODES[:2])])
    assert _row(tmp_path / "p", {"store-reaper": {"real": real, "real-retry": real,
                                                  "synthetic": synthetic}})["class"] == "new"


def _rebuilt(tmp_path, suite, data, log):
    """Through the runner: the attempt's diffs and completeness."""
    path = tmp_path / f"{suite}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    printed = forced.outcome(1, log)["diffs"]
    return forced.full_diffs(suite, path, printed)


LONG_OP = {"op": "tool", "name": "memory_recall",
           "args": {"query": "what does Ada drink in the morning before the standup", "top_k": 5}}


def test_the_w2e_rebuild_is_every_step_diff_as_w2e_prints_it(tmp_path):
    seven = [f".value.results[{i}].score: python 0.{i} vs rust 0.{i + 1}" for i in range(7)]
    data = {"complete": True, "planned_scenarios": ["recall"], "summary": {"scenarios": 1},
            "scenarios": [{"scenario": "recall", "mutant": None, "stats": {}, "steps": [
                {"step": 0, "op": {"op": "init"}, "diffs": []},
                {"step": 3, "op": LONG_OP, "diffs": seven},
                {"step": 4, "op": {"op": "sql"}, "diffs": ["db /rows/public.facts/rows: row only in rust: [1]"]}]}]}
    cut = json.dumps(LONG_OP)[:90]
    assert len(json.dumps(LONG_OP)) > 90
    want = [f"DIFF [recall] step 3 {cut}: {d}" for d in seven] + [
        'DIFF [recall] step 4 {"op": "sql"}: db /rows/public.facts/rows: row only in rust: [1]']
    log = "\n".join(want[:6] + want[7:]) + "\n"           # w2e prints 6 per step
    assert _rebuilt(tmp_path, "w2e", data, log) == (want, None)
    assert forced.harness_lines("w2e", data, capped=True) == want[:6] + want[7:]


def test_the_routes_rebuild_is_every_case_db_and_refusal_line(tmp_path):
    items = [f".json.items[{i}].id: python {i} vs rust {i + 1}" for i in range(5)]
    db = [f"/rows/public.slot_reads/rows: row only in python: [\"box\", {i}]" for i in range(21)]
    data = {"summary": {"cases": 2}, "scenarios": [{"scenario": "tokens", "cases": [
        {"case": "stats", "diffs": items, "declared": False},
        {"case": "health", "diffs": [], "declared": False}], "db_diffs": db}],
        "refusals": [{"case": "refusals/oversize-body", "diffs": ["status 413 vs 400"]},
                     {"case": "refusals/clean", "diffs": []}]}
    want = ([f"DIFF [tokens] stats: {items}"] + [f"DB DIFF [tokens] {d}" for d in db]
            + ["DIFF refusals/oversize-body: ['status 413 vs 400']"])
    log = "\n".join([f"DIFF [tokens] stats: {items[:4]}"] + want[1:21] + want[22:]) + "\n"
    assert _rebuilt(tmp_path, "routes", data, log) == (want, None)
    assert "python 4 vs rust 5" in want[0]                 # the item past the 4 printed
    # Its progress write is {"partial": [...]}, never the full list.
    assert _rebuilt(tmp_path, "routes", {"partial": data["scenarios"]}, log)[0] is None


def test_the_w2d_rebuild_is_every_case_and_db_line(tmp_path):
    items = [f".entries[{i}].score: python 0.{i} vs rust 0.{i + 1}" for i in range(6)]
    db = [f"public.retrieval_events[{i}].served[0].components.ce: python 0.1 vs rust 0.2" for i in range(21)]
    data = {"complete": True, "planned_scenarios": ["rrf-pool"], "summary": {},
            "results": [{"scenario": "rrf-pool", "declared": {}, "coverage": {}, "db_diffs": db,
                         "cases": [{"case": "rrf 3", "diffs": items, "notes": ["n"]},
                                   {"case": "rrf 4", "diffs": [], "notes": []}]}]}
    want = [f"DIFF [rrf-pool] rrf 3: {items}"] + [f"DB DIFF [rrf-pool] {d}" for d in db]
    log = "\n".join([f"DIFF [rrf-pool] rrf 3: {items[:5]}", "note [rrf-pool] rrf 3: n"] + want[1:21]) + "\n"
    assert _rebuilt(tmp_path, "w2d_search", data, log) == (want, None)
    assert _rebuilt(tmp_path, "w2d_search", {**data, "complete": False}, log)[0] is None


def test_the_board_rebuild_is_every_step_state_and_chain_line(tmp_path):
    items = [f".json.agents[{i}].status: python a vs rust b" for i in range(5)]
    state = [f"after step 13 (B to A anyone): coordination_wakes: row only in rust: {{\"n\": {i}}}"
             for i in range(13)]
    chain = [f"python: created_at: value {i}.5 does not match the captured clock windows" for i in range(13)]
    data = {"complete": True, "planned_scenarios": ["wake"], "summary": {},
            "results": [{"scenario": "wake", "state_diffs": state, "chain": chain,
                         "steps": [{"step": 3, "name": "register", "diffs": items},
                                   {"step": 4, "name": "list", "diffs": []}]}]}
    want = ([f"DIFF [wake] #3 register: {items}"] + [f"STATE [wake] {d}" for d in state]
            + [f"CHAIN [wake] {p}" for p in chain])
    log = "\n".join([f"DIFF [wake] #3 register: {items[:4]}"] + want[1:13] + want[14:26]
                    + ["  (CHAIN [wake]: +1 more problems; --out holds them all)"]) + "\n"
    assert _rebuilt(tmp_path, "board", data, log) == (want, None)


def test_the_rebuild_caps_are_the_harnesses_own_print_statements():
    """The rebuild repeats each harness's DIFF print and its cap; a harness
    that changes either must change the rebuild too (a mismatch at run time
    only marks the attempt incomplete)."""
    printed = {
        "w2e.py": ["for d in s[\"diffs\"][:6]:",
                   "print(f\"DIFF [{n}] step {s['step']} {json.dumps(s['op'])[:90]}: {d}\")"],
        "run.py": ["print(f\"DIFF [{r['scenario']}] {c['case']}: {c['diffs'][:4]}\")",
                   "for d in r[\"db_diffs\"][:20]:", "print(f\"DB DIFF [{r['scenario']}] {d}\")",
                   "print(f\"DIFF {r['case']}: {r['diffs']}\")"],
        "w2d_search.py": ["print(f\"DIFF [{r['scenario']}] {c['case']}: {c['diffs'][:5]}\")",
                          "for d in r[\"db_diffs\"][:20]:", "print(f\"DB DIFF [{r['scenario']}] {d}\")"],
        "board/run.py": ["print(f\"DIFF [{r['scenario']}] #{s['step']} {s['name']}: {s['diffs'][:4]}\")",
                         "for d in r[\"state_diffs\"][:12]:", "print(f\"STATE [{r['scenario']}] {d}\")",
                         "for p in r[\"chain\"][:12]:", "print(f\"CHAIN [{r['scenario']}] {p}\")"],
    }
    for script, lines in printed.items():
        source = (forced.HARNESS / script).read_text(encoding="utf-8")
        for line in lines:
            assert line in source, (script, line)
