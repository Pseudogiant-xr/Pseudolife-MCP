"""The forced real-data runner's refusal, classification and report, without daemons."""
from __future__ import annotations

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


@pytest.fixture(autouse=True)
def _no_database_drops(monkeypatch):
    """main's dropper drains through pgdisposable; these tests have no bank,
    so a run's tombstone drops are recorded instead (the database tests at
    the end use the real ones)."""
    calls = []
    monkeypatch.setattr(forced, "drop_tombstones", lambda token: calls.append(token) or 0)
    return calls

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
    real = {"status": "diff", "diffs": [
        "DIFF [mcp-get] get 2: ['status: python 200 vs rust 500', '.json.total: python 1 vs rust 2']"]}
    synthetic = {"status": "diff", "diffs": ["DIFF [mcp-get] get 2: ['.json.total: python 3 vs rust 4']"]}
    assert ("get 2", "status") in forced.signatures(real)
    assert forced.classify(real, real, synthetic) == "new"   # status is not on the synthetic arm
    alone = {"status": "diff", "diffs": ["DIFF [mcp-get] get 2: ['status: python 200 vs rust 500']"]}
    assert forced.classify(alone, alone, synthetic) == "new"           # status is not shared


def test_a_new_place_does_not_hide_behind_a_shared_one():
    """Conductor decision, 2026-11-10: preexisting only when every real-bank
    place is also on the synthetic arm. Sharing one place used to excuse
    every other difference the real bank showed."""
    shared = 'DIFF [store-reaper] step 1 {"op": "tool"}: db /rows/public.episodes/rows: row only in python: [1]'
    extra = 'DIFF [store-reaper] step 3 {"op": "tool"}: db /rows/public.facts/rows: row only in rust: [2]'
    real = {"status": "diff", "diffs": [shared, extra]}
    synthetic = {"status": "diff", "diffs": [shared.replace("[1]", "[9]")]}
    assert forced.classify(real, real, synthetic) == "new"
    # The near miss: every real place on the synthetic arm (which may differ
    # in more places) is preexisting.
    wider = {"status": "diff", "diffs": [shared, extra, 'DIFF [store-reaper] step 4 {}: db /rows/public.meta/rows: x']}
    assert forced.classify(real, real, wider) == "preexisting"


def test_a_scenario_error_keeps_its_harness_location():
    """Review (Opus): the same exception type raised elsewhere in the
    harness is another place."""
    one = {"status": "diff", "diffs": [
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
    plain, dq = ({"status": "diff", "diffs": ["DIFF [stamped] scenario error: " + repr(
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
    real = {"status": "diff", "diffs": [shared, new_path]}
    assert forced.classify(real, real, {"status": "diff", "diffs": [shared]}) == "new"
    assert forced.classify(real, real, {"status": "diff", "diffs": [new_path, shared]}) == "preexisting"


@pytest.mark.parametrize("kind", sorted(SAME_PLACE))
def test_every_suites_lines_find_the_same_place_without_their_values(kind):
    """w2d, board and routes lines held no step or db path that signatures()
    read, so it fell back to the line's first 120 characters, values and all:
    a synthetic arm differing in the same place never overlapped, and three
    w2d scenarios read as new (forced run, 2026-11-09)."""
    real, synthetic = ({"status": "diff", "diffs": [line]} for line in SAME_PLACE[kind])
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
    retried = {"status": "diff", "diffs": ["DIFF [s] step 1 {}: db /rows/public.facts/rows: x"]}
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
                     sweep=lambda: events.append("sweep") or 0, oom_kills=lambda: 0,
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
