"""The forced real-data runner's refusal, classification and report, without daemons."""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import forced  # noqa: E402
# Imported before any test points PSEUDOLIFE_TEST_PG_HOST_PORT at the box's
# mirror: pgdisposable reads it once, and main's drops import it.
import pgdisposable  # noqa: E402
import database_gate  # noqa: E402
import dbstate  # noqa: E402


@pytest.fixture(autouse=True)
def _no_database_drops(monkeypatch):
    """main's dropper drains through pgdisposable; these tests have no bank,
    so a run's tombstone drops are recorded instead (the database tests at
    the end use the real ones)."""
    calls = []
    monkeypatch.setattr(forced, "drop_tombstones", lambda token: calls.append(token) or 0)
    return calls

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
                      "error": 0, "differences": 3, "not_forced": [], "errors_with_oom": None,
                      "new_scenarios": ["fresh"]}
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
                           sweep=lambda attempt: swept.append(1) or 2, scratch=tmp_path / "scratch")
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
                   sweep=lambda attempt: swept.append(1) or 2, oom_kills=lambda: 0)
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
        "routes/stamped", "w2d_search/hide-superseded", "w2d_search/mcp-stats", "w2e/candidate-auto",
        "w2e/candidate-auto-reopen", "w2e/search-after-restart", "w2e/sweep-analyzer-tick"]


def test_the_maintainers_real_bank_exclusions_have_their_reasons():
    """Maintainer decision D2, 2026-11-10: three more scenarios whose premise
    cannot hold on a real bank."""
    for key, word in (("w2d_search/hide-superseded", "ranking"), ("w2d_search/mcp-stats", "graduation"),
                      ("w2e/candidate-auto", "judged first")):
        assert word in forced.REAL_BANK_EXCLUDED[key] and "2026-11-10" in forced.REAL_BANK_EXCLUDED[key]


def test_candidate_auto_reopen_is_excluded_beside_candidate_auto(tmp_path):
    """The conductor's ruling A under the maintainer's delegation of
    2026-11-11 (combined run e4a09212c): candidate-auto-reopen has its
    sibling candidate-auto's accepted Python memory limit, so on a real
    bank's copy neither real attempt compares. Its real arms do not run and
    its synthetic arm must pass."""
    reason = forced.REAL_BANK_EXCLUDED["w2e/candidate-auto-reopen"]
    for word in ("w2e/candidate-auto", "Python memory limit", "e4a09212c", "settle timeout",
                 "OOM kill", "synthetic arm passed",
                 "accepted by the conductor under the maintainer's delegation of 2026-11-11"):
        assert word in reason
    answers = {"candidate-auto-reopen": {"synthetic": PASS},
               "candidate-shadow": {"real": PASS}}
    runner = FakeRunner(tmp_path, answers)
    report, private = forced.run(runner, ["w2e"], None, names=lambda s: list(answers))
    rows = {r["scenario"]: r for r in report["suites"]["w2e"]["rows"]}
    assert rows["candidate-auto-reopen"]["class"] == "excluded"
    assert [c for c in runner.calls if c[1] == "candidate-auto-reopen"] == [
        ("w2e", "candidate-auto-reopen", "synthetic", False)]
    # Near miss: its sibling that is not listed still runs its real arm.
    assert rows["candidate-shadow"]["class"] == "pass"
    assert ("w2e", "candidate-shadow", "real", True) in runner.calls
    # Near miss: a failing synthetic arm is an error, not an exclusion.
    answers = {"candidate-auto-reopen": {"synthetic": {"status": "diff", "diffs": ["DIFF [x] step 1: y"],
                                                       "seconds": 1.0}}}
    report, private = forced.run(FakeRunner(tmp_path, answers), ["w2e"], None, names=lambda s: list(answers))
    assert report["suites"]["w2e"]["rows"][0]["class"] == "error"
    assert private["w2e/candidate-auto-reopen"]["real"] is None


BIND_FIXTURE = ["security-remote-open", "security-remote-auth", "trust-bind"]


@pytest.mark.parametrize("jobs", [1, 2])
def test_the_bind_fixture_scenarios_are_not_forced_and_invoke_nothing(tmp_path, jobs):
    """The conductor's ruling G under the maintainer's delegation of
    2026-11-11 (remainder run e2f6057ee): the three routes scenarios that
    need the debug mutants bind fixture refuse before any listener on the
    release binary the forced runner uses, so no arm of theirs runs. They
    are listed as not forced, in no class."""
    answers = {"seeded": {"real": PASS}, "static-build": {"real": PASS}}
    workers = [FakeRunner(tmp_path / f"w{n}", answers) for n in range(jobs)]
    report, private = forced.run(workers[0], ["routes"], None,
                                 names=lambda s: ["seeded", *BIND_FIXTURE, "static-build"],
                                 workers=workers)
    calls = [c for w in workers for c in w.calls]
    assert not [c for c in calls if c[1] in BIND_FIXTURE]
    body = report["suites"]["routes"]
    assert body["not_forced"] == BIND_FIXTURE and body["planned"] == 2
    assert [r["scenario"] for r in body["rows"]] == ["seeded", "static-build"]
    assert report["complete"] and private == {}
    counted = forced.counts(report)["routes"]
    assert counted["not_forced"] == BIND_FIXTURE and counted["run"] == 2 and counted["pass"] == 2
    assert counted["preexisting"] == 0 and counted["error"] == 0
    # Near miss: another routes scenario, the debug binary's other user
    # included, still runs its real arm.
    assert ("routes", "seeded", "real", True) in calls and ("routes", "static-build", "real", True) in calls
    for name in BIND_FIXTURE:
        reason = report["not_forced"][f"routes/{name}"]
        for word in ("debug mutants bind fixture", "release image",
                     "conductor under the maintainer's delegation of 2026-11-11",
                     "run.py live --only security-remote-open security-remote-auth", "issue #1473"):
            assert word in reason, (name, word)
    # The harness-level entries stay beside them.
    assert set(forced.NOT_FORCED) <= set(report["not_forced"])


def test_only_naming_a_not_forced_scenario_runs_nothing_for_it(tmp_path):
    runner = FakeRunner(tmp_path, {"seeded": {"real": PASS}})
    report, _ = forced.run(runner, ["routes"], ["trust-bind", "seeded"],
                           names=lambda s: ["seeded", "trust-bind", "security-remote-open"])
    assert [c[1] for c in runner.calls] == ["seeded"]
    assert report["suites"]["routes"]["not_forced"] == ["trust-bind"]


def test_the_premises_a_real_bank_breaks_are_excluded_with_their_reasons():
    """Maintainer decision, 2026-11-10 (partial run 870fb28fc): stamped needs
    an unreferenced builtin relation to delete, and a real bank references
    them all; search-after-restart's narrowed access-count expect still fails
    on both daemons alike there."""
    for key, word in (("routes/stamped", "re-seeds builtins"), ("w2e/search-after-restart", "both daemons")):
        assert word in forced.REAL_BANK_EXCLUDED[key] and "2026-11-10" in forced.REAL_BANK_EXCLUDED[key]


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


DIGEST_PY = ("DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.meta/rows: row only in python: "
             "[\"graph_digest\", {\"communities\": [{\"id\": 1, \"size\": 2}], \"computed_at\": 1.0}]")
DIGEST_RS = ("DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.meta/rows: row only in rust: "
             "[\"graph_digest\", {\"communities\": [{\"id\": 1, \"size\": 3}], \"computed_at\": 2.0}]")
ROUTES_DIGEST = "DB DIFF [reaper] /rows/public.meta/rows: row only in rust: [\"graph_digest\", {\"computed_at\": 2.0}]"


def test_the_graph_digest_row_joins_the_community_acceptance(tmp_path):
    """Maintainer decision, 2026-11-10 (partial run 870fb28fc): meta's
    graph_digest row is the snapshot written with the partition
    (_refresh_graph_insight), so a scenario whose differences are the
    community tables and fields plus that row is accepted."""
    row = _community_rows(tmp_path, [COMMUNITY_ROWS, MEMBERSHIP, INSIGHT, DIGEST_PY, DIGEST_RS])
    assert (row["class"], row.get("accepted_as")) == ("accepted", "community-partition")
    assert _community_rows(tmp_path / "r", [ROUTES_COMMUNITY, ROUTES_DIGEST])["class"] == "accepted"
    assert "graph_digest" in forced.ACCEPTED_DIVERGENCES["community-partition"]


@pytest.mark.parametrize("other", [
    # Another meta row, and one whose key only starts like the digest's.
    "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.meta/rows: row only in python: [\"dream_cursor\", 5]",
    "DB DIFF [reaper] /rows/public.meta/rows: row only in rust: [\"graph_digest_backup\", {}]",
    # A meta row gone from the base names no key, so it is not the digest's.
    "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.meta/base_missing/count: python 1 vs rust 0",
    # The digest's key in another table is not the meta row.
    "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db /rows/public.facts/rows: row only in python: [\"graph_digest\", 1]",
    # One line carrying several meta rows: the digest's key in it does not
    # vouch for the others (dbstate prints one row per line).
    "DIFF [reaper] bank after idle close: ['/rows/public.meta/rows: row only in python: "
    "[\"graph_digest\", {}]', '/rows/public.meta/rows: row only in python: [\"dream_cursor\", 5]']",
])
def test_the_graph_digest_acceptance_covers_that_meta_row_only(tmp_path, other):
    """The near misses: with the digest row and community rows, one other
    place keeps the scenario new."""
    assert _community_rows(tmp_path, [COMMUNITY_ROWS, DIGEST_PY, DIGEST_RS, other])["class"] == "new"


def _observed(*items):
    """A routes session observation's case line (run.py prints a case's
    diff list as its repr): response-side bank paths, one per value."""
    return f"DIFF [session-reap] bank after idle close: {list(items)}"


RESPONSE_PARTITION = (".json.db.rows.public.entity_communities.rows[3][1]: python 5 vs rust 6",
                      ".json.db.rows.public.communities.rows[0][2]: python 4 vs rust 3",
                      ".json.db.rows.public.communities.rows: python [[1, 2]] vs rust [[1, 2], [2, 1]]")
RESPONSE_DIGEST = (".json.db.rows.public.meta.by_key.graph_digest.surprises[0].src: python 1 vs rust 2",
                   ".json.db.rows.public.meta.by_key.graph_digest.totals.communities: python 9 vs rust 8")


def test_the_response_side_partition_and_digest_are_accepted_like_the_bank_side(tmp_path):
    """Maintainer decision, 2026-11-10 (box check of the session scenarios):
    the observed bank's partition values (communities rows, a membership's
    community_id) and the graph_digest value, named by its key, are the same
    D1 difference on the response side. A routes DB line beside them too."""
    line = _observed(*RESPONSE_PARTITION, *RESPONSE_DIGEST)
    row = _community_rows(tmp_path, [line, ROUTES_COMMUNITY, ROUTES_DIGEST])
    assert (row["class"], row.get("accepted_as")) == ("accepted", "community-partition")
    assert _community_rows(tmp_path / "p", [_observed(*RESPONSE_PARTITION)])["class"] == "accepted"
    # As on the bank side, a community table's base_missing is the partition too.
    missing = _observed(".json.db.rows.public.communities.base_missing.count: python 0 vs rust 2788",
                        ".json.db.rows.public.entity_communities.base_missing.digest: python \"a\" vs rust \"b\"")
    assert _community_rows(tmp_path / "m", [missing])["class"] == "accepted"
    assert "response-side" in forced.ACCEPTED_DIVERGENCES["community-partition"]


@pytest.mark.parametrize("other", [
    # Another meta key, and a meta value named only by its row index.
    ".json.db.rows.public.meta.by_key.dream_cursor: python 1 vs rust 2",
    ".json.db.rows.public.meta.rows[5][1].surprises[0].src: python 1 vs rust 2",
    # Columns outside the partition on those tables.
    ".json.db.rows.public.communities.rows[0][4]: python 1.0 vs rust 2.0",          # computed_at
    ".json.db.rows.public.entity_communities.rows[3][2]: python 1.0 vs rust 2.0",   # computed_at
    ".json.db.rows.public.entity_communities.rows[3][0]: python 7 vs rust 8",       # entity_id
    # Another table, and a non-bank field of the answer.
    ".json.db.rows.public.entity_sources.rows[0][4]: python 1.0 vs rust 2.0",
    ".json.events.session_end_dream_triggers: python 1 vs rust 0",
    # A key that only starts like the digest's.
    ".json.db.rows.public.meta.by_key.graph_digest_v2.totals: python 1 vs rust 2",
    # base_missing and a row-count line on a table outside the partition.
    ".json.db.rows.public.meta.base_missing.count: python 3 vs rust 4",
    ".json.db.rows.public.entity_sources.base_missing.count: python 0 vs rust 2751",
    ".json.db.rows.public.entity_sources.rows: python [[1]] vs rust [[1], [2]]",
])
def test_the_response_side_acceptance_is_confined_to_the_partition_and_digest(tmp_path, other):
    """The near misses: one other response-side value keeps it new."""
    line = _observed(*RESPONSE_PARTITION, *RESPONSE_DIGEST, other)
    assert _community_rows(tmp_path, [line])["class"] == "new"


def _observation(membership, digest, meta_by_key=True):
    """One side's session observation as background_sessions builds it:
    {"db": <state>}, meta keyed by key (keyed_meta) or as dbstate's rows."""
    meta = ({"columns": ["key", "value"], "by_key": {"graph_digest": digest}} if meta_by_key
            else {"columns": ["key", "value"], "rows": [["graph_digest", digest]]})
    return {"status": 200, "headers": {}, "json": {"db": {"rows": {
        "public.communities": {"columns": ["id", "label", "size", "cohesion", "computed_at"],
                               "rows": [[1, "a", len(membership), 0.5, "<computed_at>"]]},
        "public.entity_communities": {"columns": ["entity_id", "community_id", "computed_at"],
                                      "rows": [[7, membership[0], "<computed_at>"]]},
        "public.meta": meta}}}}


def test_the_comparators_own_output_for_an_observation_is_classified(tmp_path):
    """Review (Codex P2): the acceptance must match what run.py's comparator
    really prints for a session observation, not only hand-written paths.
    The keyed meta (background_sessions.keyed_meta, a companion change)
    names the digest; dbstate's positional rows do not, so they stay new."""
    sys.path.insert(0, str(forced.HARNESS))
    import run
    py = _observation([1, 2], {"totals": {"communities": 3}, "surprises": [{"src": 1}]})
    rs = _observation([2, 2, 2], {"totals": {"communities": 2}, "surprises": [{"src": 4}]})
    items = run.diff_values(py, rs)
    assert any(".json.db.rows.public.meta.by_key.graph_digest." in i for i in items), items
    line = f"DIFF [session-reap] bank after idle close: {items}"
    assert _community_rows(tmp_path, [line])["class"] == "accepted"
    positional = run.diff_values(_observation([1, 2], {"totals": {"communities": 3}}, False),
                                 _observation([2, 2, 2], {"totals": {"communities": 2}}, False))
    line = f"DIFF [session-reap] bank after idle close: {positional}"
    assert _community_rows(tmp_path / "p", [line])["class"] == "new"


def test_a_whole_row_on_a_community_table_is_the_partition(tmp_path):
    """Review (conductor): a whole-row place (rows[i], no column index), as
    diff_values prints it when one row's width differs, counts as partition
    on a community table and covers every column; on another table it
    stays a difference."""
    whole = ".json.db.rows.public.entity_communities.rows[3]: python [7, 1] vs rust [7, 1, 2.0]"
    assert _community_rows(tmp_path, [_observed(whole, *RESPONSE_DIGEST)])["class"] == "accepted"
    other = ".json.db.rows.public.entity_sources.rows[3]: python [7, \"x\"] vs rust [7, \"x\", 1]"
    assert _community_rows(tmp_path / "o", [_observed(whole, other)])["class"] == "new"


def test_a_response_side_digest_only_retry_keeps_it_new(tmp_path):
    """Each real attempt needs its own partition difference on the response
    side too: a retry whose observation differs only in the digest stays new."""
    first = _observed(*RESPONSE_PARTITION, *RESPONSE_DIGEST)
    assert _community_rows(tmp_path, [first], retry=[_observed(*RESPONSE_DIGEST)])["class"] == "new"
    assert _community_rows(tmp_path / "b", [first], retry=[first])["class"] == "accepted"


def test_a_response_side_digest_alone_is_not_accepted(tmp_path):
    assert _community_rows(tmp_path, [_observed(*RESPONSE_DIGEST)])["class"] == "new"
    assert _community_rows(tmp_path / "b", [_observed(*RESPONSE_DIGEST), ROUTES_DIGEST])["class"] == "new"


def test_the_digest_row_alone_is_not_a_community_difference(tmp_path):
    """The maintainer accepted the digest beside the partition it is written
    with. Alone it also holds god_nodes, which differ for another recorded
    reason (betweenness-above-betweenness-sample.md) even when the two
    partitions agree, so a digest-only attempt stays new."""
    assert _community_rows(tmp_path, [DIGEST_PY, DIGEST_RS])["class"] == "new"
    assert _community_rows(tmp_path / "m", [MEMBERSHIP, DIGEST_PY, DIGEST_RS])["class"] == "accepted"
    # Each real attempt needs its own partition difference: a retry that
    # differs only in the digest keeps the scenario new.
    assert _community_rows(tmp_path / "r", [MEMBERSHIP, DIGEST_PY, DIGEST_RS],
                           retry=[DIGEST_PY, DIGEST_RS])["class"] == "new"


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


def test_a_known_python_memory_limit_is_accepted_not_an_error(tmp_path, monkeypatch):
    """Maintainer decision (c), 2026-11-08: candidate-auto and -reopen
    exceed the runner's memory on Python at the harness's -1.0 floors."""
    # Both scenarios' real-bank arms are excluded (candidate-auto since
    # 2026-11-10, D2), so the rule is pinned on candidate-auto-reopen with
    # its exclusion lifted: the rule stays for when real arms run again.
    monkeypatch.delitem(forced.REAL_BANK_EXCLUDED, "w2e/candidate-auto-reopen", raising=False)
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


# ---- background drops: a scenario's databases are renamed away and dropped
# ---- while the next scenario runs

def test_each_attempt_hands_its_drops_to_the_dropper_after_its_sweep(tmp_path):
    """The harness gets the run's token, and the dropper starts only after
    the sweep, which must not drop the tombstones itself (it waits)."""
    events, seen = [], {}

    class Records(forced.Runner):
        def _run(self, argv, env):
            seen.update(env)
            return 0, ""

    class Dropper:
        def kick(self):
            events.append("kick")

    runner = Records(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                     sweep=lambda attempt: events.append("sweep") or 0, oom_kills=lambda: 0,
                     drop_token="abcd1234ef", dropper=Dropper())
    runner.invoke("w2e", "x", "real", base=True)
    assert seen[forced.DROP_LATER_ENV] == "abcd1234ef"
    assert events == ["sweep", "kick"]


def test_without_a_dropper_the_harness_drops_as_before(tmp_path):
    seen = {}

    class Records(forced.Runner):
        def _run(self, argv, env):
            seen.update(env)
            return 0, ""

    Records(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
            oom_kills=lambda: 0).invoke("w2e", "x", "real", base=True)
    assert forced.DROP_LATER_ENV not in seen


def test_the_dropper_waits_for_its_last_pass_and_drains_before_returning():
    """No two passes overlap (a pass that listed the tombstones before the
    last scenario's renames would miss them), and the drain's own pass runs
    after every background one has ended."""
    import threading
    import time
    started, release, passes = threading.Event(), threading.Event(), []
    lock, running, most = threading.Lock(), [0], [0]

    def drop():
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
            passes.append(len(passes))
        if len(passes) == 1:
            started.set()
            release.wait(5)
        elif len(passes) == 2:
            time.sleep(0.3)  # still running when the drain starts its own pass
        with lock:
            running[0] -= 1

    dropper = forced.Dropper(drop)
    dropper.kick()
    assert started.wait(5)
    threading.Timer(0.3, release.set).start()
    dropper.kick()
    dropper.drain()
    assert passes == [0, 1, 2] and most[0] == 1 and running[0] == 0


def test_every_drop_ends_before_the_summary_is_written(tmp_path, monkeypatch, _no_database_drops):
    """A stopped run's too: the summary of a run that left databases behind
    would hide them."""
    _main_env(monkeypatch)
    out = tmp_path / "o"
    order = []

    def drops(token):
        order.append(("drops", (out / "summary.json").exists()))
        return 0
    monkeypatch.setattr(forced, "drop_tombstones", drops)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: (
        {"suites": {}, "not_forced": {}, "complete": False, "stopped": "before w2e/x: STOP file present"},
        {}))
    forced.main(["--rust-bin", "x", "--out", str(out), "--suites", "w2e", "--only", "store-defaults"])
    assert order == [("drops", False)]
    assert (out / "summary.json").exists()


def test_a_run_that_raises_still_drops_its_databases(tmp_path, monkeypatch, _no_database_drops):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])

    def boom(runner, *a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(forced, "run", boom)
    with pytest.raises(KeyboardInterrupt):
        forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e",
                     "--only", "store-defaults"])
    assert len(_no_database_drops) == 1


def _bench():
    if not pgdisposable.LOGIN_FILE.exists():
        database_gate.unavailable("no disposable-bank login here",
                                  "create it once on the daemon host with `pseudolife-mcp test-login "
                                  "create`, or point PSEUDOLIFE_TEST_PG_LOGIN_FILE at it")
    return pgdisposable


def _databases(pgdisposable, prefix: str) -> list[str]:
    with pgdisposable._admin() as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT datname FROM pg_database WHERE starts_with(datname, %s)", (prefix,)))


def test_a_drop_under_a_run_token_frees_the_name_and_leaves_a_tombstone(monkeypatch):
    """The name is free at once (the scenario may create it again), and the
    run's tombstone drop removes the database; without the token a drop
    drops, as it always has."""
    import uuid
    pgdisposable = _bench()
    pg = pgdisposable.Slice("mir")
    token = uuid.uuid4().hex[:12]
    tombs = pgdisposable.tombstone_prefix(token)
    name = pg.name("pl_cf_mir_droplater")
    try:
        pg.create(name)
        monkeypatch.setenv(pgdisposable.DROP_LATER_ENV, token)
        pg.drop(name)
        assert name not in _databases(pgdisposable, name)
        assert len(_databases(pgdisposable, tombs)) == 1
        pg.create(name)  # the name is free again
        pg.drop(name)
        assert len(_databases(pgdisposable, tombs)) == 2
        assert pgdisposable.drop_tombstones(token) == 2
        assert _databases(pgdisposable, tombs) == []
        monkeypatch.delenv(pgdisposable.DROP_LATER_ENV)
        pg.create(name)
        pg.drop(name)
        assert _databases(pgdisposable, name) == [] and _databases(pgdisposable, tombs) == []
    finally:
        monkeypatch.delenv(pgdisposable.DROP_LATER_ENV, raising=False)
        pg.drop(name)
        pgdisposable.drop_tombstones(token)


def test_a_database_still_in_use_is_dropped_at_once(monkeypatch):
    """A rename cannot end another session (FORCE can): the drop falls back
    to dropping, and leaves no tombstone behind."""
    import uuid
    import psycopg
    pgdisposable = _bench()
    pg = pgdisposable.Slice("mir")
    token = uuid.uuid4().hex[:12]
    name = pg.name("pl_cf_mir_dropbusy")
    try:
        dsn = pg.create(name)
        monkeypatch.setenv(pgdisposable.DROP_LATER_ENV, token)
        with psycopg.connect(dsn, autocommit=True) as held:
            held.execute("SELECT 1")
            started = forced.time.monotonic()
            pg.drop(name)
            # Review (Opus): a rename would wait 5 s for the session first.
            assert forced.time.monotonic() - started < 3
        assert _databases(pgdisposable, name) == []
        assert _databases(pgdisposable, pgdisposable.tombstone_prefix(token)) == []
    finally:
        monkeypatch.delenv(pgdisposable.DROP_LATER_ENV, raising=False)
        pg.drop(name)
        pgdisposable.drop_tombstones(token)


def test_the_sweep_leaves_the_runs_tombstones_to_its_dropper(monkeypatch):
    """Sweeping them would make every scenario wait for its drops again;
    another run's tombstones (a killed run's) are leftovers it sweeps."""
    import uuid
    pgdisposable = _bench()
    pg = pgdisposable.Slice("mir")
    live = pgdisposable.Slice("w2f")  # the sweep spares pl_cf_mir_* (the banks)
    mine, other = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
    names = [pg.name("pl_cf_mir_sweepmine"), pg.name("pl_cf_mir_sweepother")]
    leftover = live.name("pl_cf_w2f_sweeplive")
    try:
        for n in names:
            pg.create(n)
        live.create(leftover)
        monkeypatch.setenv(pgdisposable.DROP_LATER_ENV, mine)
        pg.drop(names[0])
        monkeypatch.setenv(pgdisposable.DROP_LATER_ENV, other)
        pg.drop(names[1])
        monkeypatch.delenv(pgdisposable.DROP_LATER_ENV)
        with pgdisposable._admin() as conn:  # read only: the bench is shared
            listed = set(forced.leftover_names(conn, spare=pgdisposable.tombstone_prefix(mine)))
        assert not any(n.startswith(pgdisposable.tombstone_prefix(mine)) for n in listed)
        assert any(n.startswith(pgdisposable.tombstone_prefix(other)) for n in listed)
        assert leftover in listed and not set(names) & listed
        # The run's drops are its own: another run's tombstone stays for its sweep.
        assert pgdisposable.drop_tombstones(mine) == 1
        assert len(_databases(pgdisposable, pgdisposable.tombstone_prefix(other))) == 1
    finally:
        monkeypatch.delenv(pgdisposable.DROP_LATER_ENV, raising=False)
        for n in names:
            pg.drop(n)
        live.drop(leftover)
        pgdisposable.drop_tombstones(mine)
        pgdisposable.drop_tombstones(other)


def test_a_stopped_run_leaves_none_of_its_tombstones(tmp_path, monkeypatch):
    """A tombstone an earlier scenario left is dropped before a stopped
    run's summary exists."""
    import uuid
    pgdisposable = _bench()
    pg = pgdisposable.Slice("mir")
    token = uuid.uuid4().hex[:12]  # its own: the bench is shared
    monkeypatch.setattr(forced, "new_drop_token", lambda: token)
    monkeypatch.setattr(forced, "drop_tombstones", pgdisposable.drop_tombstones)
    name = pg.name("pl_cf_mir_dropstop")
    try:
        pg.create(name)
        monkeypatch.setenv(pgdisposable.DROP_LATER_ENV, token)
        pg.drop(name)
        monkeypatch.delenv(pgdisposable.DROP_LATER_ENV)
        assert len(_databases(pgdisposable, pgdisposable.tombstone_prefix(token))) == 1
        _main_env(monkeypatch)
        monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
        (tmp_path / "STOP").write_text("")
        forced.main(["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e",
                     "--only", "store-defaults", "--stop-file", str(tmp_path / "STOP")])
        assert _databases(pgdisposable, pgdisposable.tombstone_prefix(token)) == []
        assert "STOP" in json.loads((tmp_path / "o" / "summary.json").read_text())["stopped"]
    finally:
        monkeypatch.delenv(pgdisposable.DROP_LATER_ENV, raising=False)
        pg.drop(name)
        pgdisposable.drop_tombstones(token)


@pytest.mark.skipif(not hasattr(forced.os, "killpg"), reason="the runner is Linux; process groups are POSIX")
def test_an_interrupted_run_stops_its_harness_before_the_drops_drain(tmp_path, monkeypatch):
    """Review (Codex): Ctrl-C reaches forced.py only (the harness has a
    session of its own), so the harness is stopped as a timeout stops it, and
    its cleanup (its renames) runs before the interrupt reaches main's drain."""
    script, marker = tmp_path / "harness.py", tmp_path / "cleaned"
    script.write_text(
        "import signal, sys, time\n"
        f"def cleanup(*_):\n    open({str(marker)!r}, 'w').write('renamed')\n    sys.exit(130)\n"
        "signal.signal(signal.SIGINT, cleanup)\n"
        "print('started', flush=True)\n"
        "time.sleep(600)\n")
    real = forced.subprocess.Popen.communicate
    calls = []

    def communicate(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            forced.time.sleep(1.0)  # the harness is up and has its handler
            raise KeyboardInterrupt
        return real(self, *a, **k)
    monkeypatch.setattr(forced.subprocess.Popen, "communicate", communicate)
    runner = forced.Runner(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s")
    with pytest.raises(KeyboardInterrupt):
        runner._run([sys.executable, str(script)], dict(forced.os.environ))
    assert marker.read_text() == "renamed"


def test_tombstone_drops_retry_a_session_force_cannot_end(monkeypatch):
    """Review (Codex): FORCE cannot end an autovacuum worker on a just-used
    bank; the tombstone drop retries it as every other drop does."""
    import psycopg
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    tries = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            if isinstance(query, str) and query.startswith("SELECT datname"):
                return type("R", (), {"fetchall": lambda s: [("pl_cf_gone_abcd1234_ffff",)]})()
            tries.append(1)
            if len(tries) == 1:
                raise psycopg.errors.InsufficientPrivilege("autovacuum worker")

    monkeypatch.setattr(pgdisposable, "_admin", lambda: Conn())
    monkeypatch.setattr(pgdisposable, "assert_disposable_database", lambda conn: "postgres")
    monkeypatch.setattr(pgdisposable.time, "sleep", lambda s: None)
    assert pgdisposable.drop_tombstones("abcd1234") == 1
    assert len(tries) == 2


def test_a_final_drop_that_fails_keeps_the_results_and_fails_the_run(tmp_path, monkeypatch):
    """Review (Opus): hours of results are not thrown away for one drop; the
    summary says which drops failed and the run exits non-zero."""
    _main_env(monkeypatch)

    def drops(token):
        raise RuntimeError("autovacuum held the database")
    monkeypatch.setattr(forced, "drop_tombstones", drops)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    monkeypatch.setattr(forced, "run", lambda runner, *a, **k: ({"suites": {}, "not_forced": {},
                                                                 "complete": True}, {}))
    out = tmp_path / "o"
    assert forced.main(["--rust-bin", "x", "--out", str(out), "--suites", "w2e",
                        "--only", "store-defaults"]) == 1
    summary = json.loads((out / "summary.json").read_text())
    assert "autovacuum held the database" in summary["drops_failed"]
    assert (out / "private.json").exists()


def test_a_session_that_arrives_after_the_check_still_gets_dropped(monkeypatch):
    """The activity check passed, then a session connected before the
    rename: the rename refuses (ObjectInUse) and the drop falls back to
    FORCE rather than fail."""
    import psycopg

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            text = query if isinstance(query, str) else ""
            if text.startswith("SELECT 1 FROM pg_database"):
                return type("R", (), {"fetchone": lambda s: (1,)})()
            if text.startswith("SELECT count(*) FROM pg_stat_activity"):
                return type("R", (), {"fetchone": lambda s: (0,)})()
            raise psycopg.errors.ObjectInUse("database is being accessed by other users")

    space = type("Space", (), {"_admin": staticmethod(lambda: Conn()),
                               "assert_disposable_database": staticmethod(lambda conn: "postgres")})()
    assert pgdisposable._rename_away(space, "pl_cf_mir_x_0123456789abcdef", "abcd1234") is False


# ---- --jobs N: scenarios side by side, each on its own worker

class SlowRunner(forced.Runner):
    """A worker whose answers take a set time, recording what it ran."""

    def __init__(self, tmp: Path, answers: dict, delays: dict, ran: list, stop=None):
        super().__init__(Path("rust-bin"), tmp, 60, tmp / "STOP")
        self.answers, self.delays, self.ran, self.stop_when = answers, delays, ran, stop

    def invoke(self, suite, name, label, base):
        self.ran.append((id(self), name, label))
        forced.time.sleep(self.delays.get(name, 0))
        return dict(self.answers[name][label])

    def blocked(self):
        return self.stop_when() if self.stop_when else None


def test_side_by_side_rows_keep_the_plan_order(tmp_path):
    """The first scenario finishes last; its row still comes first, every
    scenario runs once with its own retries on one worker, and the work is
    shared."""
    answers = {"a": {"real": PASS}, "b": {"real": DIFF, "real-retry": PASS}, "c": {"real": PASS},
               "d": {"real": DIFF, "real-retry": DIFF, "synthetic": DIFF}}
    ran = []
    workers = [SlowRunner(tmp_path, answers, {"a": 0.6}, ran) for _ in range(3)]
    report, private = forced.run(workers[0], ["w2e"], None, names=lambda s: list(answers),
                                 workers=workers)
    rows = report["suites"]["w2e"]["rows"]
    assert [r["scenario"] for r in rows] == ["a", "b", "c", "d"] and report["complete"]
    assert [r["class"] for r in rows] == ["pass", "transient", "pass", "preexisting"]
    by_scenario = {}
    for worker, name, label in ran:
        by_scenario.setdefault(name, set()).add(worker)
    assert all(len(w) == 1 for w in by_scenario.values()), "a scenario's attempts split over workers"
    assert len({w for w, _, _ in ran}) > 1
    assert sorted(n for _, n, label in ran if label == "real") == ["a", "b", "c", "d"]
    assert set(private) == {"w2e/b", "w2e/d"}


def test_a_stop_side_by_side_starts_nothing_more(tmp_path):
    answers = {n: {"real": PASS} for n in "abcdef"}
    ran = []

    def stop():
        return "STOP file present" if len(ran) >= 2 else None
    workers = [SlowRunner(tmp_path, answers, {n: 0.3 for n in "abcdef"}, ran, stop) for _ in range(2)]
    report, _ = forced.run(workers[0], ["w2e"], None, names=lambda s: list(answers), workers=workers)
    assert not report["complete"] and "STOP file present" in report["stopped"]
    assert [r["scenario"] for r in report["suites"]["w2e"]["rows"]] == ["a", "b"]
    assert len(ran) == 2


def test_a_worker_that_raises_ends_the_run_with_its_error(tmp_path):
    class Raises(SlowRunner):
        def invoke(self, suite, name, label, base):
            if name == "b":
                raise RuntimeError("worker broke")
            return super().invoke(suite, name, label, base)

    answers = {n: {"real": PASS} for n in "abcd"}
    ran = []
    workers = [Raises(tmp_path, answers, {}, ran) for _ in range(2)]
    with pytest.raises(RuntimeError, match="worker broke"):
        forced.run(workers[0], ["w2e"], None, names=lambda s: list(answers), workers=workers)


def test_one_job_keeps_the_one_at_a_time_path(tmp_path):
    """--jobs 1 plans each suite when it reaches it, as every run before: a
    stop in the first suite never lists the second."""
    listed = []

    def names(suite):
        listed.append(suite)
        return ["clean", "flaky"]
    runner = FakeRunner(tmp_path, ANSWERS, stop_after=1)
    report, _ = forced.run(runner, ["w2e", "board"], None, names=names, workers=[runner])
    assert listed == ["w2e"] and "stopped" in report


def test_main_gives_each_job_its_own_worker(tmp_path, monkeypatch):
    _main_env(monkeypatch)
    monkeypatch.setattr(forced, "scenario_names", lambda suite: ["store-defaults"])
    seen, swept = {}, []
    monkeypatch.setattr(forced, "sweep_leftovers",
                        lambda spare="", attempt="": swept.append(attempt) or 0)

    def capture(runner, *a, workers=None, **k):
        seen["workers"] = workers
        return {"suites": {}, "not_forced": {}, "complete": True}, {}
    monkeypatch.setattr(forced, "run", capture)
    args = ["--rust-bin", "x", "--out", str(tmp_path / "o"), "--suites", "w2e", "--only", "store-defaults"]
    assert forced.main(args + ["--jobs", "3"]) == 0
    ws = seen["workers"]
    assert len(ws) == 3 and len({w.scratch for w in ws}) == 3 and len({w.base_cache for w in ws}) == 1
    assert all(w.oom_kills() is None for w in ws) and len({id(w.dropper) for w in ws}) == 1
    assert swept == [""], "once every worker stopped, the run sweeps run-wide once"
    ws[1].sweep("0a1b2c3d")
    assert swept == ["", "0a1b2c3d"], "an attempt's sweep is scoped to its token"
    assert json.loads((tmp_path / "o" / "summary.json").read_text())["jobs"] == 3
    swept.clear()
    assert forced.main(args) == 0
    (one,) = seen["workers"]
    one.sweep("0a1b2c3d")
    assert swept == [""], "one at a time, the sweep is the run-wide one"
    assert forced.main(args + ["--jobs", "0"]) == 2


def test_each_attempt_names_its_databases_with_its_own_token(tmp_path):
    seen, swept = [], []

    class Records(forced.Runner):
        def _run(self, argv, env):
            seen.append(env[forced.ATTEMPT_ENV])
            return 0, ""

    runner = Records(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                     sweep=lambda attempt: swept.append(attempt) or 0, oom_kills=lambda: 0)
    runner.invoke("w2e", "x", "real", base=True)
    runner.invoke("w2e", "x", "real-retry", base=True)
    assert all(forced.re.fullmatch(r"[0-9a-f]{8}", a) for a in seen) and seen[0] != seen[1]
    assert swept == seen


def test_a_harness_suffix_starts_with_its_attempts_token():
    import subprocess
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from rust.cli_harness.run_names import RunNames; "
            "print(RunNames().name('pl_cf_w2e_x', max_length=40))")
    root = str(Path(__file__).resolve().parents[4])
    env = dict(forced.os.environ, PL_HARNESS_ATTEMPT="0a1b2c3d")
    names = [subprocess.run([sys.executable, "-c", code, root], env=env, capture_output=True,
                            text=True, check=True).stdout.strip() for _ in range(2)]
    suffixes = [n.rsplit("_", 1)[1] for n in names]
    assert all(s.startswith("0a1b2c3d") and len(s) == 16 for s in suffixes)
    assert suffixes[0] != suffixes[1], "two processes of one attempt share a name"
    env.pop("PL_HARNESS_ATTEMPT")
    other = subprocess.run([sys.executable, "-c", code, root], env=env, capture_output=True,
                           text=True, check=True).stdout.strip()
    assert len(other.rsplit("_", 1)[1]) == 16


def test_a_scoped_sweep_lists_only_its_attempts_databases():
    """Another worker's live copies are not leftovers; read only but for the
    two databases it makes (the bench is shared), named as two attempts'
    harnesses name theirs."""
    import secrets
    from psycopg import sql
    _bench()
    mine, theirs = secrets.token_hex(4), secrets.token_hex(4)
    names = [f"pl_cf_w2f_scoped_{mine}{secrets.token_hex(4)}",
             f"pl_cf_w2f_scoped_{theirs}{secrets.token_hex(4)}"]
    with pgdisposable._admin() as conn:
        pgdisposable.assert_disposable_database(conn)
        try:
            for n in names:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(n)))
            assert names[0] in forced.leftover_names(conn, attempt=mine)
            assert names[1] not in forced.leftover_names(conn, attempt=mine)
            assert set(names) <= set(forced.leftover_names(conn))
            with pytest.raises(ValueError):
                forced.leftover_names(conn, attempt="not-hex!")
        finally:
            for n in names:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(n)))


@pytest.mark.skipif(not hasattr(forced.os, "killpg"), reason="the runner is Linux; process groups are POSIX")
def test_an_interrupt_side_by_side_stops_every_running_harness(tmp_path):
    """Ctrl-C reaches forced.py only: each worker's running harness gets
    SIGINT (its cleanup renames its databases) before the interrupt reaches
    main's drain, and no further scenario starts."""
    import signal
    import threading
    script = tmp_path / "harness.py"
    script.write_text(
        "import signal, sys, time, os\n"
        "def cleanup(*_):\n"
        "    open(os.environ['MARK'] + '.' + str(os.getpid()), 'w').write('cleaned')\n"
        "    sys.exit(130)\n"
        "signal.signal(signal.SIGINT, cleanup)\n"
        "open(os.environ['MARK'] + '.start.' + str(os.getpid()), 'w').write('started')\n"
        "print('started', flush=True)\n"
        "time.sleep(600)\n")
    marks = tmp_path / "mark"

    class Real(forced.Runner):
        def invoke(self, suite, name, label, base):
            # Review (Opus, Codex): an interrupted harness exits non-zero, so
            # the attempt is an error and the scenario would go on to its retry.
            code, log = self._run([sys.executable, str(script)], dict(forced.os.environ, MARK=str(marks)))
            return {"status": "pass" if code == 0 else "error", "diffs": [], "seconds": 0, "exit": code}

    # A timeout far past the test's own bound: only the interrupt can stop them.
    workers = [Real(Path("rust-bin"), tmp_path / "out", 600, tmp_path / "STOP", scratch=tmp_path / f"s{n}")
               for n in range(2)]
    threading.Timer(1.5, lambda: signal.raise_signal(signal.SIGINT)).start()
    started = forced.time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        forced.run(workers[0], ["w2e"], None, names=lambda s: ["a", "b", "c", "d"], workers=workers)
    assert forced.time.monotonic() - started < 30
    assert len(list(tmp_path.glob("mark.start.*"))) == 2, "an interrupted run started another attempt"
    assert len([p for p in tmp_path.glob("mark.*") if ".start." not in p.name]) == 2


def test_an_interrupted_worker_starts_no_further_attempt(tmp_path):
    """Review (Opus, Codex): the interrupt lands while an attempt runs; its
    harness exits non-zero and the scenario would retry. A worker that was
    interrupted refuses to start another attempt, retry or synthetic."""
    runs = []

    class Interrupted(forced.Runner):
        def _run(self, argv, env):
            runs.append(argv)
            self.interrupt()  # arrives during this attempt (no harness to signal here)
            return 130, "KeyboardInterrupt\n"

    runner = Interrupted(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                         oom_kills=lambda: 0)
    with pytest.raises(forced.Cancelled):
        forced._scenario(runner, "w2e", "x")
    assert len(runs) == 1


def test_an_interrupt_before_an_attempt_starts_no_harness(tmp_path):
    """Between attempts, or before a worker's first: the interrupt leaves no
    harness to signal, and the next one never starts."""
    popened = []

    class Records(forced.Runner):
        pass

    runner = Records(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s",
                     oom_kills=lambda: 0)
    runner.interrupt()
    real_popen = forced.subprocess.Popen
    try:
        forced.subprocess.Popen = lambda *a, **k: popened.append(a) or pytest.fail("a harness started")
        with pytest.raises(forced.Cancelled):
            runner.invoke("w2e", "x", "real", base=True)
    finally:
        forced.subprocess.Popen = real_popen
    assert popened == []


def test_a_worker_whose_admission_check_raises_ends_the_run(tmp_path):
    """Review (Codex): an exception outside the scenario itself (the STOP
    or disk check) must not read as a complete run with rows missing."""
    class Broken(SlowRunner):
        def blocked(self):
            raise OSError("disk probe failed")

    answers = {n: {"real": PASS} for n in "ab"}
    workers = [Broken(tmp_path, answers, {}, []) for _ in range(2)]
    with pytest.raises(OSError, match="disk probe failed"):
        forced.run(workers[0], ["w2e"], None, names=lambda s: list(answers), workers=workers)


def test_a_copy_of_the_base_waits_out_a_reader(monkeypatch):
    """Review (Codex, Opus): with --jobs another worker's harness may be
    reading the shared base (its row hashes) when this one copies it; the
    copy gives up after about 5 s with ObjectInUse, so it is retried."""
    import psycopg
    calls = []

    class Conn:
        def execute(self, statement, params=None):
            calls.append(statement)
            if len(calls) < 3:
                raise psycopg.errors.ObjectInUse('source database "pl_cf_mir_seed" is being accessed by other users')

    monkeypatch.setattr(pgdisposable.time, "sleep", lambda s: None)
    pgdisposable._copy_retrying(Conn(), "CREATE DATABASE x TEMPLATE pl_cf_mir_seed")
    assert len(calls) == 3
    calls.clear()

    class Busy(Conn):
        def execute(self, statement, params=None):
            calls.append(statement)
            raise psycopg.errors.ObjectInUse("still in use")

    with pytest.raises(psycopg.errors.ObjectInUse):
        pgdisposable._copy_retrying(Busy(), "CREATE DATABASE x TEMPLATE pl_cf_mir_seed", attempts=3)
    assert len(calls) == 3


def test_an_interrupt_racing_the_start_still_stops_the_harness_starting(tmp_path):
    """The interrupt lands after invoke's check but before the harness
    starts: _run checks again under the lock that guards the running one."""
    runner = forced.Runner(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / "s")
    runner.cancelled = True
    with pytest.raises(forced.Cancelled):
        runner._run([sys.executable, "-c", "raise SystemExit('a harness started')"], dict(forced.os.environ))
    assert runner._proc is None


@pytest.mark.skipif(not hasattr(forced.os, "killpg"), reason="the runner is Linux; process groups are POSIX")
def test_an_interrupt_kills_a_harness_that_ignores_it(tmp_path):
    """As a timeout does: SIGINT, then SIGKILL to the group after the grace,
    so a harness stuck in its cleanup cannot outlive the run."""
    import threading
    script = tmp_path / "stubborn.py"
    script.write_text("import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                      "print('started', flush=True)\ntime.sleep(600)\n")
    runner = forced.Runner(Path("rust-bin"), tmp_path / "out", 600, tmp_path / "STOP", scratch=tmp_path / "s")
    result = {}
    worker = threading.Thread(target=lambda: result.update(
        out=runner._run([sys.executable, str(script)], dict(forced.os.environ))), daemon=True)
    worker.start()
    for _ in range(100):
        if runner._proc is not None:
            break
        forced.time.sleep(0.05)
    started = forced.time.monotonic()
    runner.interrupt(grace=1.0)
    worker.join(30)
    assert not worker.is_alive() and result["out"][0] is not None
    assert forced.time.monotonic() - started < 15


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


def test_an_earlier_runs_matching_out_file_is_not_this_attempts(tmp_path):
    """The attempt writes no --out JSON, but an earlier run left one at the
    same path whose capped lines match this log exactly. The runner deletes
    it before the harness starts, so the printed lines stay incomplete and
    the community-only scenario stays new."""
    attempt = _w2e_attempt("graph-writes", [(35, GRAPH_OP, COMMUNITIES[:2])])
    for label in ("real", "real-retry"):
        left = tmp_path / "w2e" / label / "graph-writes.json"
        left.parent.mkdir(parents=True)
        left.write_text(json.dumps(attempt["json"]), encoding="utf-8")
    silent = {**attempt, "json": None}
    row = _row(tmp_path, {"graph-writes": {"real": silent, "real-retry": silent, "synthetic": PASS}})
    assert row["class"] == "new" and "accepted_as" not in row
    # Control: with this attempt's own file the same scenario is accepted.
    row = _row(tmp_path / "own", {"graph-writes": {"real": attempt, "real-retry": attempt,
                                                   "synthetic": PASS}})
    assert row.get("accepted_as") == "community-partition"


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
    # And the --out writes HARNESS_OUT tells apart, whose keys it reads: the
    # final one, and routes' progress write ({"partial": ...}) it refuses.
    written = {
        "w2e.py": ["args.out.write_text(json.dumps({\"complete\": True, \"planned_scenarios\": names,",
                   "\"summary\": summary, \"scenarios\": results}, indent=1,"],
        "run.py": ["args.out.write_text(json.dumps({\"summary\": summary, \"scenarios\": results, "
                   "\"refusals\": refusals},", "json.dumps({\"partial\": results}"],
        "w2d_search.py": ["args.out.write_text(json.dumps({\"complete\": True, \"planned_scenarios\": names,",
                          "\"summary\": summary, \"results\": results}, indent=1)"],
        "board/run.py": ["args.out.write_text(json.dumps({\"complete\": True, \"planned_scenarios\": planned,",
                         "\"summary\": summary, \"results\": results}, indent=1,"],
    }
    for script in printed:
        source = (forced.HARNESS / script).read_text(encoding="utf-8")
        for line in printed[script] + written[script]:
            assert line in source, (script, line)


RECORDED = Path(__file__).parent / "fixtures" / "forced_out"

# Each recording's difference lines in full, counted without forced.py: from
# the JSON's own lists (fixtures/forced_out/README.md), and for board also its
# summary ("chain_problems": 24) and cut note ("+12 more problems").
FULL_LINES = {"w2e--console-graph-malformed": 4, "w2d_search--mcp-search-explain": 14,
              "w2d_search--rrf-pool": 352, "routes--seeded": 8, "routes--trust-bind": 1,
              "board--identity": 24}


def _difference_lists(suite: str, data: dict) -> list[tuple[bool, list]]:
    """Every difference list a harness wrote, and whether it prints whole on
    one line (a case's or step's list) rather than one line per item."""
    if suite == "w2e":
        return [(False, s["diffs"]) for r in data["scenarios"] for s in r["steps"]]
    lists = []
    for r in data["scenarios"] if suite == "routes" else data["results"]:
        lists += [(True, c["diffs"]) for c in r.get("cases", r.get("steps", []))]
        lists += [(False, r.get(key, [])) for key in ("db_diffs", "state_diffs", "chain")]
    if suite == "routes":
        lists += [(True, r["diffs"]) for r in data["refusals"]]
    return [(whole, items) for whole, items in lists if items]


def _recorded(path: Path):
    suite = path.name.split("--")[0]
    printed = forced.outcome(1, path.with_suffix(".log").read_text(encoding="utf-8"))["diffs"]
    full, why = forced.full_diffs(suite, path, printed)
    return suite, printed, full, why, json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("suite", list(forced.HARNESS_OUT))
def test_a_recorded_harness_run_rebuilds_to_the_lines_it_printed(suite):
    """Real harness runs on the synthetic bank, each its --out JSON and its
    log as the runner keeps them (fixtures/forced_out/README.md). The capped
    rebuild must be the log's DIFF lines in order; the full one must hold
    every difference the JSON lists, each case's list whole; and one nothing
    was cut from is the printed lines as a multiset."""
    cases = sorted(RECORDED.glob(f"{suite}--*.json"))
    assert cases, f"no recorded {suite} run"
    for path in cases:
        _, printed, full, why, data = _recorded(path)
        assert printed and why is None, (path.name, why)
        lists = _difference_lists(suite, data)
        assert len(full) == sum(1 if whole else len(items) for whole, items in lists), path.name
        assert len(full) == FULL_LINES[path.stem], path.name
        text = "\n".join(full)
        for whole, items in lists:
            if whole:  # its own line ends with the whole list
                assert any(line.endswith(f": {items}") for line in full), (path.name, str(items)[:80])
            else:
                for item in items:
                    assert str(item) in text, (path.name, str(item)[:80])
        if forced.harness_lines(suite, data, capped=True) == full:
            assert collections.Counter(full) == collections.Counter(printed), path.name


def test_the_recordings_hold_both_a_cut_and_a_whole_printout():
    kinds = collections.Counter()
    for path in RECORDED.glob("*--*.json"):
        suite, _, full, why, data = _recorded(path)
        assert why is None, (path.name, why)
        kinds["whole" if forced.harness_lines(suite, data, capped=True) == full else "cut"] += 1
    assert kinds["whole"] and kinds["cut"], kinds


def test_side_by_side_each_attempt_reads_its_own_out_json(tmp_path, monkeypatch):
    """Conductor (#1446's truncation fix with --jobs): the runner rebuilds an
    attempt's full differences from its harness's --out JSON, so at --jobs 2
    each worker must read its own scenario's file. Two recorded routes runs
    with different full counts (8 and 1) start together; each scenario's
    attempts keep their own count, from their own file."""
    import shutil
    import threading
    together = threading.Barrier(2, timeout=30)
    seen = []

    class Recorded(forced.Runner):
        first = True

        def _run(self, argv, env):
            name, out = argv[argv.index("--only") + 1], Path(argv[argv.index("--out") + 1])
            if self.first:  # both first attempts run at once
                self.first = False
                together.wait()
            seen.append((name, out))
            shutil.copy(RECORDED / f"routes--{name}.json", out)
            return 1, (RECORDED / f"routes--{name}.log").read_text(encoding="utf-8")

    # The recorded trust-bind run is used only as a one-line routes result:
    # lift ruling G so it runs.
    monkeypatch.setattr(forced, "NOT_FORCED_SCENARIOS", {})
    workers = [Recorded(Path("rust-bin"), tmp_path / "out", 60, tmp_path / "STOP", scratch=tmp_path / f"s{n}",
                        sweep=lambda attempt: 0, oom_kills=lambda: None) for n in range(2)]
    _, private = forced.run(workers[0], ["routes"], None, names=lambda s: ["seeded", "trust-bind"],
                            workers=workers)
    want = {"seeded": FULL_LINES["routes--seeded"], "trust-bind": FULL_LINES["routes--trust-bind"]}
    for name, count in want.items():
        for label in ("real", "real_retry", "synthetic"):
            attempt = private[f"routes/{name}"][label]
            assert attempt["diffs_complete"] and len(attempt["diffs"]) == count, (name, label)
    assert len({out for _, out in seen}) == len({(n, out.parent.name) for n, out in seen})


# ---- tie-order: the conductor's rulings B, C and D (2026-11-11) ----

SEARCH_OP = {"op": "tool", "name": "memory_search", "args": {"query": "q", "top_k": 30}}
SAME, OTHER, THIRD = "a" * 32, "b" * 32, "c" * 32


def w2e_line(step: int, diff: str) -> str:
    return f"DIFF [recall] step {step} {json.dumps(SEARCH_OP)[:90]}: {diff}"


def w2d_case(case: str, items: list[str]) -> str:
    return f"DIFF [default] {case}: {items}"


def pick(at, py, rs, md5_py=SAME, md5_rs=SAME, via=(None, None), found=(True, True), surprise=(0.5, 0.5),
         scores=({"score": 0.5}, {"score": 0.5}), ordered=False, tool="memory_search"):
    out = {"at": at, "kind": "pick", "python": py, "rust": rs,
           "text_md5": {"python": md5_py, "rust": md5_rs}, "ordered": ordered}
    if "served[" not in at:
        out["tool"] = tool
    if "served[" in at:
        out["via"] = {"python": via[0], "rust": via[1]}
        out["found"] = {"python": found[0], "rust": found[1]}
        out["surprise"] = {"python": surprise[0], "rust": surprise[1]}
        out["scores"] = {"python": scores[0], "rust": scores[1]}
    return out


def attempt(diffs, proofs, **extra):
    return {"status": "diff", "diffs": diffs, "seconds": 1.0, "exit": 1, "diffs_complete": True,
            "oom_kills": 0, "tie_proofs": proofs, **extra}


CRAFTED = ["'.entries[2].access_count: python 11 vs rust 9'", "'.entries[2].id: python 69 vs rust 65'",
           "'.entries[2].surprise_score: python 0.9964 vs rust 0.9828'",
           "'.entries[2].timestamp: python 1781769001.8 vs rust 1781768640.1'"]
CRAFTED_ITEMS = [c.strip("'") for c in CRAFTED]


def test_a_duplicate_pick_is_a_tie_only_when_its_texts_are_proven_equal():
    """Ruling B: a response position serving two ids whose recorded text
    hashes are equal is the recorded tie-order divergence."""
    lines = [w2d_case("crafted", CRAFTED_ITEMS)]
    proofs = {"case crafted": [pick(".entries[2]", 69, 65)]}
    assert forced._tie_only(attempt(lines, proofs))
    # Near misses: different texts; no hash on either side; no proof; a proof
    # for other ids than the line's; a field beside the counters and stamps.
    assert not forced._tie_only(attempt(lines, {"case crafted": [pick(".entries[2]", 69, 65, md5_rs=OTHER)]}))
    assert not forced._tie_only(attempt(lines, {"case crafted": [pick(".entries[2]", 69, 65, None, None)]}))
    assert not forced._tie_only(attempt(lines, {}))
    assert not forced._tie_only(attempt(lines, {"case crafted": [pick(".entries[2]", 69, 66)]}))
    assert not forced._tie_only(attempt(lines, {"case other": [pick(".entries[2]", 69, 65)]}))
    extra = [w2d_case("crafted", CRAFTED_ITEMS + [".entries[2].source: python \"a\" vs rust \"b\""])]
    assert not forced._tie_only(attempt(extra, proofs))
    # An access_count difference with no id line is not a pick.
    count_only = [w2d_case("crafted", [CRAFTED_ITEMS[0]])]
    assert not forced._tie_only(attempt(count_only, proofs))


def test_an_event_duplicate_pick_needs_both_served_items_found():
    """Ruling B on a retrieval event's served list (w2e's events_diff)."""
    at = "db retrieval_events[9582].served[25]"
    lines = [w2e_line(9, f"{at}.components.surprise: python 0.99637973 vs rust 0.9828261"),
             w2e_line(9, f"{at}.entry_id: python 69 vs rust 65")]
    assert forced._tie_only(attempt(lines, {"step 9": [pick(at, 69, 65)]}))
    assert not forced._tie_only(attempt(lines, {"step 9": [pick(at, 69, 65, found=(True, False))]}))
    # A score difference at the position is not a tie.
    scored = lines + [w2e_line(9, f"{at}.score: python 0.5 vs rust 0.6")]
    assert not forced._tie_only(attempt(scored, {"step 9": [pick(at, 69, 65)]}))
    # The same proof under another step does not vouch for this one.
    assert not forced._tie_only(attempt(lines, {"step 10": [pick(at, 69, 65)]}))


# A true near-tie: A and B within twice the tolerance on each side, each
# entry's score within the tolerance across the engines.
NEAR = {"a_py": 0.5001, "b_rs": 0.50008, "b_py": 0.5, "a_rs": 0.50003}


def swap(event=9583, p=666, a=1056, b=1816, via=("dense", "dense"), md5=(OTHER, THIRD),
         surprise=((1.0, 0.22029644), (0.22029644, 1.0)), scores=None):
    at = [f"db retrieval_events[{event}].served[{p}]", f"db retrieval_events[{event}].served[{p + 1}]"]
    (s0py, s0rs), (s1py, s1rs) = surprise
    lines = [w2e_line(9, f"{at[0]}.components.surprise: python {s0py} vs rust {s0rs}"),
             w2e_line(9, f"{at[0]}.entry_id: python {a} vs rust {b}"),
             w2e_line(9, f"{at[1]}.components.surprise: python {s1py} vs rust {s1rs}"),
             w2e_line(9, f"{at[1]}.entry_id: python {b} vs rust {a}")]
    sc = NEAR if scores is None else scores
    p0 = ({"score": sc["a_py"]}, {"score": sc["b_rs"]}) if sc else (None, None)
    p1 = ({"score": sc["b_py"]}, {"score": sc["a_rs"]}) if sc else (None, None)
    proofs = {"step 9": [pick(at[0], a, b, md5[0], md5[1], via, surprise=surprise[0], scores=p0),
                         pick(at[1], b, a, md5[1], md5[0], via, surprise=surprise[1], scores=p1)]}
    return lines, proofs


def test_an_adjacent_swap_of_distinct_entries_is_a_tie_order_difference():
    """Ruling C: two adjacent served positions exchanging two ids (texts
    differ), neither contiguity nor timeline, scores within the tolerance."""
    lines, proofs = swap()
    assert forced._tie_only(attempt(lines, proofs))
    # Rule B alone does not cover it: the rules stay separate.
    assert not forced._tie_only(attempt(lines, proofs), rules=("duplicate-pick",))
    assert forced._tie_only(attempt(lines, proofs), rules=("adjacent-swap",))
    # Near misses: a contiguity or timeline item; positions not adjacent; ids
    # that do not cross; a proof missing for one position.
    for via in (("contiguity", "dense"), ("dense", "timeline")):
        assert not forced._tie_only(attempt(*swap(via=via)))
    lines2 = [line.replace("served[667]", "served[668]") for line in lines]
    proofs2 = {"step 9": [proofs["step 9"][0], {**proofs["step 9"][1], "at": proofs["step 9"][1]["at"].replace(
        "served[667]", "served[668]")}]}
    assert not forced._tie_only(attempt(lines2, proofs2))
    crossed = lines[:3] + [lines[3].replace("rust 1056", "rust 2290")]
    assert not forced._tie_only(attempt(crossed, proofs))
    assert not forced._tie_only(attempt(lines, {"step 9": proofs["step 9"][:1]}))
    # Review (conductor's Opus, 2026-11-11): surprise belongs to the entry, so
    # at a swap it must cross; one that does not is not the swap's consequence.
    assert not forced._tie_only(attempt(*swap(surprise=((0.1, 0.9), (0.1, 0.9)))))
    assert not forced._tie_only(attempt(*swap(surprise=((1.0, 0.2), (0.21, 1.0)))))
    assert forced._tie_only(attempt(*swap(surprise=((1.0, 0.2), (0.200001, 1.0)))))
    assert not forced._tie_only(attempt(*swap(surprise=((None, 0.2), (0.2, None)))))
    # Codex's review (P1, 2026-11-11): events_diff compares by position, so a
    # swap across a real gap shows only ids. Python (A 0.9, B 0.1), Rust (B
    # 0.9, A 0.1) is a ranking change, not a tie; nor is a gap just over
    # twice the tolerance, an entry whose score moved more than the tolerance
    # across the engines, a missing score, or a component that differs.
    assert not forced._tie_only(attempt(*swap(scores={"a_py": 0.9, "b_rs": 0.9, "b_py": 0.1, "a_rs": 0.1})))
    assert not forced._tie_only(attempt(*swap(scores={"a_py": 0.50041, "b_rs": 0.5004, "b_py": 0.5,
                                                      "a_rs": 0.50039})))
    # At the edge: Python's gap 3e-4 (under twice the tolerance), each entry
    # within the tolerance across the engines, each side in its own order.
    assert forced._tie_only(attempt(*swap(scores={"a_py": 0.5003, "b_rs": 0.50015, "b_py": 0.5,
                                                  "a_rs": 0.50012})))
    assert not forced._tie_only(attempt(*swap(scores={"a_py": 0.5001, "b_rs": 0.5001, "b_py": 0.5,
                                                      "a_rs": 0.4998})))
    assert not forced._tie_only(attempt(*swap(scores={})))
    lines, proofs = swap()
    proofs["step 9"][1]["scores"]["python"] = {"score": 0.5, "dense": 0.9}
    assert not forced._tie_only(attempt(lines, proofs))
    assert forced.TIE_SCORE_TOL == dbstate.EVENT_SCORE_TOL
    assert set(forced.TIE_SCORE_KEYS) == set(dbstate.EVENT_FLOAT_KEYS) == set(tie_proofs.SCORE_KEYS)


def test_an_access_count_follow_on_is_exactly_the_extra_serves_of_a_tie_pick():
    """Ruling D: default's rerank case reports entry 67 one count higher on
    Rust because the crafted case served 67 on Rust and 70 on Python."""
    crafted = [w2d_case("crafted", [".entries[3].id: python 70 vs rust 67"])]
    rerank = [w2d_case("rerank on", [".entries[27].access_count: python 18 vs rust 19"])]
    proofs = {"case crafted": [pick(".entries[3]", 70, 67)],
              "case rerank on": [{"at": ".entries[27]", "kind": "count", "id": 67, "ordered": False, "tool": "memory_search"}]}
    assert forced._tie_only(attempt(crafted + rerank, proofs))
    assert not forced._tie_only(attempt(crafted + rerank, proofs), rules=("duplicate-pick",))
    # Near misses: a count off by more than the extra serves; the count before
    # the pick; an id no pick served; the count's id unproven.
    wrong = [w2d_case("rerank on", [".entries[27].access_count: python 18 vs rust 20"])]
    assert not forced._tie_only(attempt(crafted + wrong, proofs))
    assert not forced._tie_only(attempt(rerank + crafted, proofs))
    other = {**proofs, "case rerank on": [{"at": ".entries[27]", "kind": "count", "id": 2746, "ordered": False, "tool": "memory_search"}]}
    assert not forced._tie_only(attempt(crafted + rerank, other))
    unproven = {**proofs, "case rerank on": [{"at": ".entries[27]", "kind": "count", "id": None, "ordered": False, "tool": "memory_search"}]}
    assert not forced._tie_only(attempt(crafted + rerank, unproven))
    # Python's extra serve counts the other way.
    python_side = [w2d_case("rerank on", [".entries[27].access_count: python 29 vs rust 28"])]
    p70 = {**proofs, "case rerank on": [{"at": ".entries[27]", "kind": "count", "id": 70, "ordered": False, "tool": "memory_search"}]}
    assert forced._tie_only(attempt(crafted + python_side, p70))


def test_tie_order_is_accepted_per_scenario_and_composes_with_community(tmp_path):
    """Ruling F (2026-11-11): an attempt whose every line one accepted rule
    covers is accepted, listing every divergence used; recall-ungated in the
    targeted run had community-partition lines beside two duplicate picks."""
    lines, proofs = swap()
    tie = attempt(lines, proofs)
    padded = attempt(lines + [w2d_case("padded query", [".cortex: length python 5 vs rust 4"])], proofs)
    mixed = attempt(lines + [COMMUNITY_ROWS], proofs)
    unproven = attempt(lines + [COMMUNITY_ROWS], {})
    other_meta = attempt(lines + [COMMUNITY_ROWS, "DIFF [graph-writes] step 35 {\"op\": \"tool\"}: db "
                                  "/rows/public.meta/rows: row only in python: [\"dream_cursor\", 5]"], proofs)
    digest_only = attempt(lines + [DIGEST_PY], proofs)
    answers = {"recall": {"real": tie, "real-retry": tie, "synthetic": PASS},
               "recall-wide": {"real": padded, "real-retry": padded, "synthetic": PASS},
               "graph-writes": {"real": mixed, "real-retry": mixed, "synthetic": PASS},
               "graph-read": {"real": unproven, "real-retry": unproven, "synthetic": PASS},
               "graph-meta": {"real": other_meta, "real-retry": other_meta, "synthetic": PASS},
               "graph-digest": {"real": digest_only, "real-retry": digest_only, "synthetic": PASS},
               "graph-cut": {"real": mixed, "real-retry": {**mixed, "diffs_complete": False}, "synthetic": PASS},
               "recall-caps": {"real": tie, "real-retry": {**tie, "exit": None}, "synthetic": PASS}}
    report, _ = forced.run(FakeRunner(tmp_path, answers), ["w2e"], None, names=lambda s: list(answers))
    rows = {r["scenario"]: r for r in report["suites"]["w2e"]["rows"]}
    assert rows["recall"]["class"] == "accepted" and rows["recall"]["accepted_as"] == "tie-order"
    assert rows["graph-writes"]["class"] == "accepted"
    assert rows["graph-writes"]["accepted_as"] == "community-partition+tie-order"
    # Near misses: a line no rule proves (padded-query in an unmarked case);
    # a community line beside an unproven id difference; a proven tie beside
    # a meta row whose key is not a community key; the digest without a
    # partition line; a composed attempt whose retry is incomplete; a retry
    # that timed out.
    for name in ("recall-wide", "graph-read", "graph-meta", "graph-digest", "graph-cut", "recall-caps"):
        assert rows[name]["class"] == "new" and "accepted_as" not in rows[name], name
    assert "maintainer's delegation of 2026-11-11" in forced.ACCEPTED_DIVERGENCES["tie-order"]
    assert forced.TIE_RULES == ("duplicate-pick", "adjacent-swap", "access-count-follow-on")
    assert "padded-query" not in forced.ACCEPTED_DIVERGENCES


def test_the_runner_reads_the_proofs_the_harness_wrote(tmp_path):
    w2e_out = tmp_path / "w2e.json"
    w2e_out.write_text(json.dumps({"complete": True, "scenarios": [{"scenario": "recall", "steps": [
        {"step": 9, "diffs": ["x"], "tie_proofs": [pick("db e", 1, 2)]}, {"step": 10, "diffs": []}]}]}))
    assert forced.tie_proofs_of("w2e", w2e_out) == {"step 9": [pick("db e", 1, 2)]}
    w2d_out = tmp_path / "w2d.json"
    w2d_out.write_text(json.dumps({"complete": True, "results": [{
        "scenario": "default", "cases": [{"case": "crafted", "diffs": ["x"], "tie_proofs": [pick(".e", 1, 2)]},
                                         {"case": "plain", "diffs": []}],
        "db_diffs": ["y"], "db_tie_proofs": [pick("public.e", 3, 4)]}]}))
    assert forced.tie_proofs_of("w2d_search", w2d_out) == {"case crafted": [pick(".e", 1, 2)],
                                                           "db": [pick("public.e", 3, 4)]}
    assert forced.tie_proofs_of("routes", w2d_out) == {}


# ---- tie_proofs.py: what the harnesses record beside their lines ----

import tie_proofs  # noqa: E402


def test_response_proofs_hash_the_full_text_from_the_banks():
    """Codex's review (P2, 2026-11-11): a compact answer truncates ``text``,
    so equal previews are not equal entries. The hash is the full text's,
    read in the banks by id; with no bank reader a pick is unproved."""
    python = {"value": {"entries": [{"id": 1, "text": "x"}, {"id": 69, "text": "pre…", "access_count": 3}]}}
    rust = {"value": {"entries": [{"id": 65, "text": "pre…"}, {"id": 1, "text": "x"}]}}
    lines = [".value.entries[1].id: python 69 vs rust 65", ".value.entries[0].access_count: python 3 vs rust 4",
             "db retrieval_events[1].served[0].entry_id: python 1 vs rust 2", ".value.count: python 2 vs rust 3"]
    asked = []

    def banks(ids):
        asked.append(ids)
        return {69: SAME, 65: OTHER}  # equal previews, different full texts
    out = tie_proofs.response_proofs(lines, python, rust, text_md5=banks, tool="memory_search")
    assert out[0]["kind"] == "pick" and out[0]["at"] == ".value.entries[1]" and asked == [{65, 69}]
    assert out[0]["tool"] == "memory_search"
    assert out[0]["text_md5"] == {"python": SAME, "rust": OTHER} and out[0]["ordered"] is False
    assert "pre" not in json.dumps(out)
    # The count's position holds id 1 on Python but 65 on Rust: no id.
    assert out[1] == {"at": ".value.entries[0]", "kind": "count", "id": None, "ordered": False,
                      "tool": "memory_search"}
    assert len(out) == 2  # the db line and a non-entry line get none
    # No bank reader: the pick is unproved, never hashed from the preview.
    assert tie_proofs.response_proofs(lines[:1], python, rust)[0]["text_md5"] == {"python": None, "rust": None}
    rust["value"]["entries"] = [{"id": 1, "text": "x"}, {"id": 65, "text": "other"}]
    out = tie_proofs.response_proofs(lines[:2], python, rust, text_md5=banks)
    assert out[1] == {"at": ".value.entries[0]", "kind": "count", "id": 1, "ordered": False, "tool": None}


def test_rules_b_and_d_never_accept_in_an_ordered_response():
    """Codex's review (P2, 2026-11-11): memory_recent and a fired timeline
    are chronological, so a different entry at a position is never a tie."""
    python = {"entries": [{"id": 69, "text": "x"}]}
    rust = {"entries": [{"id": 65, "text": "x"}]}
    line = [".entries[0].id: python 69 vs rust 65"]
    same = lambda ids: {69: SAME, 65: SAME}  # noqa: E731
    assert tie_proofs.response_proofs(line, python, rust, same, ordered=True)[0]["ordered"] is True
    assert tie_proofs.response_proofs(line, python, rust, same)[0]["ordered"] is False
    stream = {"entries": [{"id": 69, "text": "x", "via": "contiguity"}]}
    assert tie_proofs.response_proofs(line, stream, rust, same)[0]["ordered"] is True
    lines = [w2d_case("recent n=1", line)]
    assert not forced._tie_only(attempt(lines, {"case recent n=1": [pick(".entries[0]", 69, 65, ordered=True)]}))
    assert forced._tie_only(attempt(lines, {"case recent n=1": [pick(".entries[0]", 69, 65)]}))
    # A proof without the flag (an older record) is not one.
    no_flag = {k: v for k, v in pick(".entries[0]", 69, 65).items() if k != "ordered"}
    assert not forced._tie_only(attempt(lines, {"case recent n=1": [no_flag]}))
    crafted = [w2d_case("crafted", [".entries[3].id: python 70 vs rust 67"])]
    rerank = [w2d_case("rerank on", [".entries[27].access_count: python 18 vs rust 19"])]
    proofs = {"case crafted": [pick(".entries[3]", 70, 67)],
              "case rerank on": [{"at": ".entries[27]", "kind": "count", "id": 67, "ordered": True,
                                  "tool": "memory_search"}]}
    assert not forced._tie_only(attempt(crafted + rerank, proofs))


def test_a_fired_timeline_orders_an_events_served_list():
    """Codex's review (P2, 2026-11-11): a fired timeline orders its served
    list by stream position even when every item is via dense."""
    def events(served, fired):
        cols = ["id", tie_proofs.EVENT_SERVED, "params"]
        return {"rows": {"public.retrieval_events": {"columns": cols,
                                                     "rows": [[7, served, {"timeline": {"fired": fired}}]]}}}
    py = [{"entry_id": 69, "via": "dense"}]
    rs = [{"entry_id": 65, "via": "dense"}]
    line = ["db retrieval_events[7].served[0].entry_id: python 69 vs rust 65"]
    md5 = lambda ids: {69: SAME, 65: SAME}  # noqa: E731
    assert tie_proofs.event_proofs(line, events(py, True), events(rs, True), md5)[0]["ordered"] is True
    assert tie_proofs.event_proofs(line, events(py, False), events(rs, False), md5)[0]["ordered"] is False
    at = "db retrieval_events[7].served[0]"
    lines = [w2e_line(9, f"{at}.entry_id: python 69 vs rust 65")]
    assert not forced._tie_only(attempt(lines, {"step 9": [pick(at, 69, 65, ordered=True)]}))
    swapped, proofs = swap()
    for q in proofs["step 9"]:
        q["ordered"] = True
    assert not forced._tie_only(attempt(swapped, proofs))


def test_event_proofs_read_via_by_id_and_hashes_from_both_banks():
    def events(served):
        return {"rows": {"public.retrieval_events": {"columns": ["id", "served"], "rows": [[7, served]]}}}
    py = events([{"entry_id": 69, "via": "dense", "score": 0.7, "components": {"surprise": 0.9, "dense": 0.6}},
                 {"entry_id": 3, "via": None}])
    rs = events([{"entry_id": 65, "via": "dense"}, {"entry_id": 3, "via": None}])
    asked = []

    def text_md5(ids):
        asked.append(ids)
        return {69: SAME, 65: SAME}
    lines = ["db retrieval_events[7].served[0].entry_id: python 69 vs rust 65",
             "db retrieval_events[7].served[0].components.surprise: python 1 vs rust 0.9"]
    out = tie_proofs.event_proofs(lines, py, rs, text_md5)
    assert out == [{"at": "db retrieval_events[7].served[0]", "kind": "pick", "python": 69, "rust": 65,
                    "text_md5": {"python": SAME, "rust": SAME}, "via": {"python": "dense", "rust": "dense"},
                    "surprise": {"python": 0.9, "rust": None},
                    "scores": {"python": {"score": 0.7, "dense": 0.6}, "rust": {"score": None}},
                    "ordered": False, "found": {"python": True, "rust": True}}]
    assert asked == [{65, 69}]
    # No pick line: no query at all.
    assert tie_proofs.event_proofs(lines[1:], py, rs, text_md5) == [] and len(asked) == 1
    w2d = tie_proofs.event_proofs(["public.retrieval_events[7].served[0].entry_id: python 69 vs rust 99"],
                                  py, rs, lambda ids: {69: SAME})
    assert w2d[0]["at"] == "public.retrieval_events[7].served[0]"
    assert w2d[0]["found"] == {"python": True, "rust": False} and w2d[0]["text_md5"]["rust"] is None


def test_bank_text_md5_keeps_a_hash_only_when_both_banks_agree():
    banks = {"py": {1: SAME, 2: OTHER}, "rs": {1: SAME, 2: THIRD}}

    class Conn:
        def __init__(self, dsn):
            self.rows = banks[dsn]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            assert "md5(text)" in sql and "text," not in sql
            wanted = params[0]

            class Result:
                @staticmethod
                def fetchall():
                    return [(i, self.rows[i]) for i in wanted if i in self.rows]
            return Result()
    read = tie_proofs.bank_text_md5({"python": "py", "rust": "rs"}, Conn)
    assert read({1, 2, 3}) == {1: SAME, 2: None, 3: None}
    assert read(set()) == {}


def test_a_proof_that_cannot_be_built_never_changes_the_outcome():
    """Review: a bank that cannot be read must leave the lines and the class
    alone (new), never turn a difference into an error."""
    def boom(ids):
        raise ConnectionError("no bank")
    lines = ["db retrieval_events[7].served[0].entry_id: python 69 vs rust 65"]
    out = tie_proofs.event_proofs(lines, None, None, boom)
    assert out[0]["unavailable"] == "ConnectionError" and out[0]["text_md5"] == {"python": None, "rust": None}
    assert tie_proofs.safely(lambda: 1 / 0) == [{"at": "", "kind": "unavailable", "why": "ZeroDivisionError"}]
    at = "db retrieval_events[7].served[0]"
    a = attempt([w2e_line(9, f"{at}.entry_id: python 69 vs rust 65")], {"step 9": out})
    assert not forced._tie_only(a)


def test_rule_b_reads_only_entries_lists():
    """Review: an id difference in another list (a fact's sources, a
    proposal list) is not a search entry, so B never covers it."""
    lines = [w2d_case("crafted", [".sources[0].id: python 69 vs rust 65"])]
    assert not forced._tie_only(attempt(lines, {"case crafted": [pick(".sources[0]", 69, 65)]}))
    lines = [w2e_line(10, ".value.entries[25].id: python 69 vs rust 65")]
    assert forced._tie_only(attempt(lines, {"step 10": [pick(".value.entries[25]", 69, 65)]}))


def test_count_and_pick_proofs_read_the_lists_as_the_comparator_aligned_them():
    """Codex's review (P2, 2026-11-11): w2d_search.compare realigns Rust's
    tied entries before diffing, so the printed path indexes the aligned
    list. Python [67, 1] against Rust [1, 67] at one score prints a count
    difference at entries[0]; read from the raw Rust body the proof found no
    shared id, and a valid follow-on stayed new."""
    import w2d_search

    def response(entries):
        return {"status": 200, "headers": {"content-type": "application/json"}, "json": {"entries": entries}}
    py = response([{"id": 67, "score": 0.5, "access_count": 18, "text": "x"},
                   {"id": 1, "score": 0.5, "access_count": 3, "text": "y"}])
    rs = response([{"id": 1, "score": 0.5, "access_count": 3, "text": "y"},
                   {"id": 67, "score": 0.5, "access_count": 19, "text": "x"}])
    bodies: dict = {}
    diffs, notes = w2d_search.compare(py, rs, aligned=bodies)
    assert diffs == [".entries[0].access_count: python 18 vs rust 19"] and notes
    assert tie_proofs.response_proofs(diffs, bodies["python"], bodies["rust"], tool="/api/search") == [
        {"at": ".entries[0]", "kind": "count", "id": 67, "ordered": False, "tool": "/api/search"}]
    assert bodies["ordered"] is False
    # The raw bodies would not have proved it.
    assert tie_proofs.response_proofs(diffs, py["json"], rs["json"])[0]["id"] is None
    # Without the argument the comparator behaves as before.
    assert w2d_search.compare(py, rs) == (diffs, notes)


def test_rule_c_refuses_a_component_none_on_one_side_or_a_bool():
    lines, proofs = swap()
    proofs["step 9"][0]["scores"]["python"] = {"score": 0.5001, "ce": None}
    for q, side in ((0, "rust"), (1, "python"), (1, "rust")):
        proofs["step 9"][q]["scores"][side] = {**proofs["step 9"][q]["scores"][side], "ce": 0.3}
    assert not forced._tie_only(attempt(lines, proofs))
    lines, proofs = swap()
    for q in (0, 1):
        for side in ("python", "rust"):
            proofs["step 9"][q]["scores"][side] = {**proofs["step 9"][q]["scores"][side], "margin": True}
    assert not forced._tie_only(attempt(lines, proofs))
    # A component None on every side is not recorded, so it does not block.
    lines, proofs = swap()
    for q in (0, 1):
        for side in ("python", "rust"):
            proofs["step 9"][q]["scores"][side] = {**proofs["step 9"][q]["scores"][side], "ce": None}
    assert forced._tie_only(attempt(lines, proofs))


def test_rule_b_and_d_read_only_search_answers():
    """Opus's sweep (2026-11-11): ruling B names search answers. An entries
    list in another tool's answer (memory_recent, a document search, a
    trace) is not one, and neither is a proof that names no tool."""
    lines = [w2e_line(10, ".value.entries[25].id: python 69 vs rust 65")]
    for tool in ("memory_search", "/api/search"):
        assert forced._tie_only(attempt(lines, {"step 10": [pick(".value.entries[25]", 69, 65, tool=tool)]}))
    for tool in ("memory_recent", "search_documents", "/api/trace", None):
        assert not forced._tie_only(attempt(lines, {"step 10": [pick(".value.entries[25]", 69, 65, tool=tool)]}))
    crafted = [w2d_case("crafted", [".entries[3].id: python 70 vs rust 67"])]
    rerank = [w2d_case("rerank on", [".entries[27].access_count: python 18 vs rust 19"])]
    proofs = {"case crafted": [pick(".entries[3]", 70, 67)],
              "case rerank on": [{"at": ".entries[27]", "kind": "count", "id": 67, "ordered": False,
                                  "tool": "/api/trace"}]}
    assert not forced._tie_only(attempt(crafted + rerank, proofs))


def test_a_served_list_holding_a_stream_ordered_item_is_ordered():
    """Opus's sweep (2026-11-11): align_ties treats a whole list holding a
    contiguity or timeline item as exact order, so the event proof does."""
    def events(served):
        cols = ["id", tie_proofs.EVENT_SERVED]
        return {"rows": {"public.retrieval_events": {"columns": cols, "rows": [[7, served]]}}}
    md5 = lambda ids: {69: SAME, 65: SAME}  # noqa: E731
    line = ["db retrieval_events[7].served[0].entry_id: python 69 vs rust 65"]
    py = [{"entry_id": 69, "via": "dense"}, {"entry_id": 3, "via": "contiguity"}]
    rs = [{"entry_id": 65, "via": "dense"}, {"entry_id": 3, "via": "contiguity"}]
    assert tie_proofs.event_proofs(line, events(py), events(rs), md5)[0]["ordered"] is True
    assert tie_proofs.event_proofs(line, events(py[:1]), events(rs[:1]), md5)[0]["ordered"] is False


def test_a_fired_trace_timeline_orders_its_entries_without_via():
    """Opus's sweep (2026-11-11): /api/trace entries carry no via, so for a
    fired trace timeline the comparator's own flag is the only signal; the
    proofs take it from compare."""
    import w2d_search

    def response(entries):
        return {"status": 200, "headers": {"content-type": "application/json"},
                "json": {"entries": entries, "trace": {"timeline": {"fired": True}}}}
    py = response([{"id": 69, "score": 0.5, "text": "x"}])
    rs = response([{"id": 65, "score": 0.5, "text": "x"}])
    bodies: dict = {}
    diffs, _ = w2d_search.compare(py, rs, aligned=bodies)
    assert bodies["ordered"] is True
    proofs = tie_proofs.response_proofs([d for d in diffs if ".entries[0].id" in d], bodies["python"],
                                        bodies["rust"], lambda ids: {69: SAME, 65: SAME},
                                        ordered=bodies["ordered"], tool="/api/search")
    assert proofs and proofs[0]["ordered"] is True


def test_a_timeline_fired_on_either_side_orders_a_search_answer():
    """Opus's review (2026-11-11): w2d read the timeline flag from Python's
    events only, and w2e response proofs never read the step's events. A
    timeline that fired on either side, even with no via timeline entry,
    orders the answer."""
    import w2d_search

    def state(*rows):
        cols = ["id", "query_text", "params"]
        return {"rows": {"public.retrieval_events": {"columns": cols, "rows": [list(r) for r in rows]}}}
    py = state((1, "q", {"timeline": {"fired": False}}))
    rs = state((1, "q", {"timeline": {"fired": True}}))
    assert w2d_search.fired_queries(py, rs) == {"q"} and w2d_search.fired_queries(py, None) == set()
    fired, seen = tie_proofs.new_events_fired(rs, set())
    assert fired is True and seen == {1}
    # The same event in a later step's dump is not that step's.
    assert tie_proofs.new_events_fired(rs, seen) == (False, {1})
    later = state((1, "q", {"timeline": {"fired": True}}), (2, "r", {"timeline": {"fired": False}}))
    assert tie_proofs.new_events_fired(later, seen) == (False, {1, 2})
    assert tie_proofs.new_events_fired(None, {5}) == (False, {5})


def test_a_w2e_step_names_its_call_for_rules_b_and_d():
    """Codex's review (P2, 2026-11-11): a w2e http step has no name, so a
    REST /api/search step could never be a search answer."""
    assert tie_proofs.step_tool({"op": "tool", "name": "memory_search"}) == "memory_search"
    assert tie_proofs.step_tool({"op": "http", "method": "GET", "path": "/api/search?q=x&top_k=3"}) == "/api/search"
    assert tie_proofs.step_tool({"op": "http", "method": "POST", "path": "/api/hook/session-end"}) == "/api/hook/session-end"
    assert tie_proofs.step_tool({"op": "init"}) is None and tie_proofs.step_tool(None) is None
    lines = [w2e_line(4, ".entries[2].id: python 69 vs rust 65")]
    tool = tie_proofs.step_tool({"op": "http", "path": "/api/search?q=x"})
    assert forced._tie_only(attempt(lines, {"step 4": [pick(".entries[2]", 69, 65, tool=tool)]}))


# ---- ruling E2: padded cases are skipped on real-bank arms (2026-11-11) ----

def test_a_real_bank_arm_skips_exactly_the_cases_whose_query_ends_in_whitespace(monkeypatch):
    """Ruling E2: on a copy of a real bank, a w2d_search case whose own query
    ends in whitespace is not sent; without a base (the synthetic arm, CI)
    every case runs."""
    import w2d_search
    scenarios = (w2d_search.Default(), w2d_search.McpSearch(), w2d_search.McpExplain(), w2d_search.Trace())
    cases = [c for scn in scenarios for c in scn.cases()]
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE", raising=False)
    assert w2d_search.real_bank_skips(cases) == set()
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    skipped = w2d_search.real_bank_skips(cases)
    assert skipped == {"padded query (cortex sees the raw q)", "padded query", "trace padded query", "trace padded q"}
    # Near misses: an unpadded case with the same words still runs, and so
    # do a blank query and a leading-space-only one.
    words = {"deploy the daemon", "what os does the homelab box run", "PLX-4471"}
    same_words = [c["name"] for c in cases if (w2d_search.case_query(c) or "").strip() in words
                  and c["name"] not in skipped]
    assert same_words and not set(same_words) & skipped
    assert not w2d_search.real_bank_skips([{"name": "blank", "path": "/api/search?q=+++"},
                                           {"name": "leading", "mcp": {"query": "  x"}}])
    assert "maintainer's delegation of 2026-11-11" in w2d_search.REAL_BANK_SKIP_REASON
    assert "synthetic arm" in w2d_search.REAL_BANK_SKIP_REASON
    # The explain scenario's real-bank floor drops by the one traced response
    # it skips, and only there.
    assert w2d_search.McpExplain.real_bank_require == {"trace.responses": 8}
    assert w2d_search.McpExplain.require["trace.responses"] == 9
    assert not getattr(w2d_search.Default, "real_bank_require", {})


def test_the_runner_reports_the_cases_a_real_arm_skipped(tmp_path):
    out = tmp_path / "w2d.json"
    out.write_text(json.dumps({"complete": True, "results": [{
        "scenario": "default", "cases": [], "db_diffs": [],
        "skipped_cases": [{"case": "padded query (cortex sees the raw q)", "reason": "r"}]}]}))
    assert forced.skipped_cases_of("w2d_search", out) == ["padded query (cortex sees the raw q)"]
    assert forced.skipped_cases_of("routes", out) == []
    assert forced.skipped_cases_of("w2d_search", tmp_path / "missing.json") == []
    passed = {**PASS, "skipped_cases": ["padded query (cortex sees the raw q)"]}
    report, _ = forced.run(FakeRunner(tmp_path, {"default": {"real": passed}}), ["w2d_search"], None,
                           names=lambda s: ["default"])
    row = report["suites"]["w2d_search"]["rows"][0]
    assert row["class"] == "pass" and row["skipped_cases"] == 1
    assert "maintainer's delegation of 2026-11-11" in forced.REAL_BANK_SKIPPED_CASES["w2d_search"]
    # Nothing skipped: no field.
    report, _ = forced.run(FakeRunner(tmp_path, {"default": {"real": PASS}}), ["w2d_search"], None,
                           names=lambda s: ["default"])
    assert "skipped_cases" not in report["suites"]["w2d_search"]["rows"][0]
