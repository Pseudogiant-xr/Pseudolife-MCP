"""Numeric comparison and run ownership without starting real daemons."""
import argparse
import importlib
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    import pgdisposable as pg
    from rust.cli_harness.run_names import RunNames
    # The executable harness changes its name admission on import; restore
    # the shared module afterwards for the other harness test files.
    monkeypatch.setattr(pg, "DISPOSABLE_NAME", pg.DISPOSABLE_NAME)
    h = importlib.import_module("w2e")
    monkeypatch.setattr(pg, "DISPOSABLE_NAME", re.compile(r"pl_cf_w2e_[a-z0-9_]{1,40}"))
    monkeypatch.setattr(pg, "NAMES", RunNames())
    return h


class FixtureFailure(RuntimeError):
    pass


@pytest.mark.parametrize("python_embedding,rust_embedding", [
    ([1.0, 0.0], [1.0]), ([1.0], [1.0, 0.0]),
    ([], [1.0]), ([1.0], []), (None, [1.0]), ([1.0], None),
])
@pytest.mark.parametrize("calibrate", [False, True])
def test_embedding_shape_mismatch_is_a_difference(harness, python_embedding, rust_embedding,
                                                 calibrate):
    tolerance = {} if calibrate else json.loads(
        harness.TOLERANCE_FILE.read_text(encoding="utf-8"))
    stats = {}
    differences = harness.compare_numeric(
        {1: {"embedding": python_embedding, "surprise": None}},
        {1: {"embedding": rust_embedding, "surprise": None}}, tolerance, stats)
    assert len(differences) == 1 and "entries[1].embedding" in differences[0]
    assert not stats  # No tolerance statistics from incompatible shapes.


def test_tokenized_embeddings_still_report_dimension_mismatch(harness):
    def state(embedding):
        return {"rows": {"public.entries": {
            "columns": ["id", "embedding", "surprise"],
            "rows": [[1, embedding, None]],
        }}}

    python, rust = state("[1.0,0.0]"), state("[1.0]")
    a, b = harness.extract_numeric(python), harness.extract_numeric(rust)
    assert python == rust  # The separate numeric comparison owns this check.
    assert "length" in harness.compare_numeric(a, b, {}, {})[0]


@pytest.mark.parametrize("driver", ["w2e", "w2e_shutdown"])
def test_overlapping_invocations_preserve_each_others_homes(harness, monkeypatch, tmp_path, driver):
    module = importlib.import_module(driver)
    arguments = [driver, *(["live", "--only", "store-defaults"] if driver == "w2e" else []),
                 "--rust-bin", "candidate", "--out", str(tmp_path / "result.json")]
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(harness.daemons, "scratch_root", lambda: tmp_path)
    roots = []

    def overlap(*args, **kwargs):
        root = args[2]
        roots.append(root)
        marker = harness.daemons.make_home(root, "same-scenario", None) / f"run-{len(roots)}"
        marker.write_text("owned evidence", encoding="utf-8")
        if len(roots) == 1:
            with pytest.raises(FixtureFailure, match="after home creation"):
                module.main()
            assert marker.read_text(encoding="utf-8") == "owned evidence"
        raise FixtureFailure("after home creation")

    monkeypatch.setattr(harness, "run_scenario" if driver == "w2e" else "prepare_template", overlap)
    with pytest.raises(FixtureFailure, match="after home creation"):
        module.main()
    assert len(set(roots)) == 2
    assert all(root.parent == tmp_path for root in roots)
    assert (roots[1] / "same-scenario" / "run-2").read_text(encoding="utf-8") == "owned evidence"


@pytest.mark.parametrize("driver", ["w2e", "w2e_shutdown"])
def test_relative_binary_is_resolved_before_provisioning(harness, monkeypatch, tmp_path, driver):
    module = importlib.import_module(driver)
    monkeypatch.chdir(tmp_path)
    arguments = [driver, *(["live"] if driver == "w2e" else []),
                 "--rust-bin", "candidate", "--out", "result.json"]
    monkeypatch.setattr(sys, "argv", arguments)
    seen = []
    parse = argparse.ArgumentParser.parse_args

    def capture(parser, *args, **kwargs):
        value = parse(parser, *args, **kwargs)
        seen.append(value)
        return value

    def stop(*args, **kwargs):
        raise FixtureFailure("before provisioning")

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    monkeypatch.setattr(harness.daemons, "scratch_root", lambda: tmp_path)
    monkeypatch.setattr(harness, "run_scenario", stop)
    monkeypatch.setattr(harness, "prepare_template", stop)
    with pytest.raises(FixtureFailure, match="before provisioning"):
        module.main()
    assert seen[0].rust_bin == tmp_path / "candidate"


@pytest.mark.parametrize("mode,stage", [
    ("live", "template-create"), ("live", "seed-start"),
    ("live", "clone-create"), ("live", "arm-start"),
    ("golden", "snapshot"), ("golden", "arm-start"),
])
def test_failure_cleans_only_this_scenarios_databases(harness, monkeypatch, tmp_path, mode, stage):
    h = harness
    prior = h.pg.name("pl_cf_w2e_prior")
    present, dropped, daemons = {prior}, [], []
    creates, starts = 0, 0
    monkeypatch.setitem(h.SCENARIOS, "cleanup", {"config": None, "seed": [], "ops": []})
    monkeypatch.setattr(h, "GOLDENS", tmp_path)
    (tmp_path / "cleanup.json").write_text(json.dumps({"steps": []}), encoding="utf-8")

    def create(name, **kwargs):
        nonlocal creates
        creates += 1
        present.add(name)
        if (stage == "template-create" and creates == 1
                or stage == "clone-create" and creates == 3):
            raise FixtureFailure(stage)
        return name

    def drop(name):
        h.pg.NAMES.require_owned(name)
        dropped.append(name)
        present.discard(name)

    def dump(dsn):
        if stage == "snapshot":
            raise FixtureFailure(stage)
        return {}

    class Daemon:
        port = 1
        started = stopped = False

        def __init__(self, *args, **kwargs):
            daemons.append(self)

        def start(self, timeout):
            self.launch()
            return self.wait(timeout)

        def launch(self):  # daemons.start_all launches every arm, then waits
            self.started = True
            return self

        def wait(self, timeout, stages=None):
            nonlocal starts
            starts += 1
            if (stage == "seed-start" or stage == "arm-start"
                    and starts == (2 if mode == "live" else 1)):
                raise FixtureFailure(stage)
            return self

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(h.pg, "create", create)
    monkeypatch.setattr(h.pg, "drop", drop)
    monkeypatch.setattr(h.dbstate, "dump", dump)
    monkeypatch.setattr(h.daemons, "python_daemon", Daemon)
    monkeypatch.setattr(h.daemons, "rust_daemon", Daemon)
    monkeypatch.setattr(h.w1a, "wait_settled", lambda *args, **kwargs: None)
    monkeypatch.setattr(h, "Mcp", lambda port: SimpleNamespace(init=lambda: None))
    with pytest.raises(FixtureFailure, match=stage):
        h.run_scenario("cleanup", tmp_path / "candidate", tmp_path, mode, False, {})
    assert present == {prior}
    assert prior not in dropped
    assert all(d.stopped for d in daemons if d.started)


def test_fact_embeddings_are_compared_by_fact_id(harness):
    def state(embedding):
        return {"rows": {"public.facts": {
            "columns": ["id", "embedding", "asserted_at"],
            "rows": [[7, embedding, 1.0]],
        }}}

    python, rust = state("[1.0,0.0]"), state("[0.0,1.0]")
    a, b = harness.extract_fact_numeric(python), harness.extract_fact_numeric(rust)
    assert python == rust and a == {7: [1.0, 0.0]}
    tolerance = json.loads(harness.TOLERANCE_FILE.read_text(encoding="utf-8"))
    differences = harness.compare_numeric({"facts": a}, {"facts": b}, tolerance, {})
    assert len(differences) == 1 and differences[0].startswith("facts[7].embedding")
    assert harness.compare_numeric({"facts": a}, {"facts": dict(a)}, tolerance, {}) == []


def test_fact_clocks_and_the_supersession_log_keep_only_presence(harness, monkeypatch):
    monkeypatch.setattr(harness.w1a, "scrub_declared_rows", lambda state, before: {"state": state})
    state = {"rows": {
        "public.facts": {"columns": ["id", "tx_time", "hlc_phys", "hlc_logical", "superseded_at"],
                         "rows": [[1, 5.5, 1700000000000, 0, None]]},
        "public.meta": {"columns": ["key", "value"], "rows": [
            ["cortex_supersession_log", [{"timestamp": 9.5, "reason": "newer_wins"}]]]},
    }}
    normalized, _, _ = harness.normalize_state(state, {"rows": {}})
    assert normalized["rows"]["public.facts"]["rows"] == [[1, "<clock>", "<clock>", 0, None]]
    assert normalized["rows"]["public.meta"]["rows"] == [
        ["cortex_supersession_log", [{"timestamp": "<clock>", "reason": "newer_wins"}]]]


def test_fact_answers_compare_clocks_by_presence_and_age_by_one_step(harness):
    def answer(t, age, current_age):
        return {"isError": False, "value": {
            "action": "contested", "asserted_at": t, "superseded_at": None, "age": age,
            "current": {"tx_time": t, "age": current_age}}}

    assert harness.compare_response(answer(1.0, "just now", "1 minute ago"),
                                    answer(2.0, "just now", "just now"), {}, {}) == []
    assert harness.compare_response(answer(1.0, "just now", "59 minutes ago"),
                                    answer(2.0, "just now", "1 hour ago"), {}, {}) == []
    # Two steps apart, or a clock present on one side only, still differ.
    assert harness.compare_response(answer(1.0, "just now", "2 minutes ago"),
                                    answer(2.0, "just now", "just now"), {}, {})
    assert harness.compare_response(answer(1.0, "just now", "just now"),
                                    answer(None, "just now", "just now"), {}, {})


def test_fact_set_scenarios_reach_every_declared_mutant(harness):
    names = [n for n in harness.SCENARIOS if n.startswith("fact-set-")]
    assert names, "no fact_set scenarios"
    fact_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-fact-set-")]
    assert len(fact_mutants) == 4
    ops = [op for n in names for op in harness.SCENARIOS[n]["ops"]]
    assert all(op["name"] == "memory_fact_set" for op in ops)
    # SQL is seed-only: the compared operations all go through MCP, except a
    # declared external write (another process) in a declared scenario.
    assert all(op["op"] != "sql" or (op.get("external") and n in harness.EXTERNAL_WRITES)
               for n in harness.SCENARIOS for op in harness.SCENARIOS[n]["ops"])


def test_set_scenarios_reach_every_declared_mutant(harness):
    names = [n for n in harness.SCENARIOS if n.startswith("set-")]
    assert set(names) == {"set-add-branches", "set-add-aggregate", "set-cap"}
    set_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-set-")]
    assert set_mutants == ["mcp-set-add-no-convert", "mcp-set-add-no-cosine-dedup",
                           "mcp-set-remove-noop"]
    ops = [op for n in names for op in harness.SCENARIOS[n]["ops"]]
    assert {op["name"] for op in ops} == {"memory_set_add", "memory_set_remove"}
    assert all(set(op["args"]) == {"entity", "attribute", "member"} for op in ops)
    # The cap's hundred members are one seed statement, never compared ops.
    cap_seed = harness.SCENARIOS["set-cap"]["seed"]
    assert [op["op"] for op in cap_seed] == ["sql"]
    assert "generate_series(0, 99)" in cap_seed[0]["sql"]


def test_fact_resolve_scenarios_reach_every_declared_mutant(harness):
    names = [n for n in harness.SCENARIOS if n.startswith("fact-resolve-")]
    assert set(names) == {"fact-resolve-scalar", "fact-resolve-set-slot"}
    resolve_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-fact-resolve-")]
    assert resolve_mutants == ["mcp-fact-resolve-no-user-tier", "mcp-fact-resolve-reject-noop",
                               "mcp-fact-resolve-no-set-guard"]
    ops = [op for n in names for op in harness.SCENARIOS[n]["ops"]]
    resolves = [op for op in ops if op["name"] == "memory_fact_resolve"]
    assert all(set(op["args"]) == {"entity", "attribute", "accept"} for op in resolves)
    # Both answers are exercised at a set slot and at a scalar slot.
    accepts = {(n, op["args"]["accept"]) for n in names for op in harness.SCENARIOS[n]["ops"]
               if op["name"] == "memory_fact_resolve"}
    assert {("fact-resolve-scalar", True), ("fact-resolve-scalar", False),
            ("fact-resolve-set-slot", True), ("fact-resolve-set-slot", False)} <= accepts
    # Contenders are parked through the tools, never by SQL.
    assert all(op["op"] == "tool" for n in names for op in harness.SCENARIOS[n]["seed"])


def test_after_stop_sql_runs_once_the_seeding_daemon_has_stopped(harness, monkeypatch, tmp_path):
    events = []

    class Daemon:
        port = 1

        def start(self, timeout):
            events.append("start")

        def stop(self):
            events.append("stop")

    class Client:
        def __init__(self, port):
            pass

        def init(self):
            pass

    def run_op(op, port, mcp, dsn):
        events.append(op.get("sql") or op["name"])
        return {"isError": False, "value": None}

    monkeypatch.setattr(harness.pg, "create", lambda name, **kw: "dsn")
    monkeypatch.setattr(harness.daemons, "python_daemon", lambda *a, **kw: Daemon())
    monkeypatch.setattr(harness.w1a, "wait_settled", lambda *a, **kw: None)
    monkeypatch.setattr(harness, "Mcp", Client)
    monkeypatch.setattr(harness, "run_op", run_op)
    scn = {"config": None, "seed": [harness.sql("late", after_stop=True),
                                    harness.sql("early"),
                                    harness.set_add("box", "tags", "a")]}
    harness.prepare_template(scn, "after_stop", tmp_path, [])
    assert events == ["start", "early", "memory_set_add", "stop", "late"]
    assert harness.SCENARIOS["set-cap"]["seed"][0]["after_stop"] is True


def test_dream_run_scenarios_switch_off_unported_stages_and_answer_every_request(harness):
    names = [n for n in harness.SCENARIOS if n.startswith("dream-run")]
    assert set(names) == {"dream-run", "dream-run-hold", "dream-run-empty", "dream-run-gates",
                          "dream-run-fallback", "dream-run-hints", "dream-run-literal",
                          "dream-run-outage", "dream-run-redirect", "dream-run-surrogate"}
    for n in names:
        scn = harness.SCENARIOS[n]
        config = scn["config"]
        # The Rust daemon refuses `run` while an unported stage is on.
        for switch in ("lessons:\n    enabled: false", "graph_insight:\n    enabled: false",
                       "stall_notice: false"):
            assert switch in config, (n, switch)
        # Only the fallback scenario configures a fallback endpoint (and so
        # probes the primary on every run).
        assert ("extractor_fallback" in scn) == (n == "dream-run-fallback")
        for reply in scn["extractor"]["replies"]:
            assert "sha256" not in reply, "vocab hints depend on embeddings: key by kind/user"
            assert reply["kind"] in ("claims", "events", "relations")
    run_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-dream-run-")]
    assert len(run_mutants) == 7
    src = Path(__file__).resolve().parents[1] / "src" / "dream"
    text = (src / "run.rs").read_text(encoding="utf-8") + (src / "relations.rs").read_text(
        encoding="utf-8")
    assert all(f'"{m}"' in text for m in run_mutants)
    extractor_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-dream-extractor-")]
    assert len(extractor_mutants) == 4
    text = (src / "extractor.rs").read_text(encoding="utf-8")
    assert all(f'"{m}"' in text for m in extractor_mutants)


def test_dream_stage_scenarios_key_their_replies_and_register_their_mutants(harness):
    names = sorted(n for n in harness.SCENARIOS if n.startswith("dream-stages-"))
    assert names == ["dream-stages-digests", "dream-stages-infer", "dream-stages-inferred",
                     "dream-stages-lessons", "dream-stages-rules"]
    for n in names:
        scn = harness.SCENARIOS[n]
        # The graph-insight refresh is the one stage still refused.
        assert "graph_insight:\n    enabled: false" in scn["config"], n
        for reply in scn["extractor"]["replies"]:
            assert "sha256" not in reply
            assert reply["kind"] in ("outcomes", "lessons", "rules", "digest"), (n, reply)
    stage_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-dream-stage-")]
    assert len(stage_mutants) == 7
    src = Path(__file__).resolve().parents[1] / "src"
    text = "".join((src / f).read_text(encoding="utf-8") for f in (
        "dream/stages.rs", "dream/lessons.rs", "dream/stage_calls.rs", "graph_review_slots.rs",
        "stores/lessons.rs"))
    assert all(f'"{m}"' in text for m in stage_mutants)


def test_dream_stage_helpers_match_python(harness):
    """The digest map step's split and the signal formatting agree with
    Python's. Importing the dream module loads the memory package, which needs
    torch; the Rust CI lane installs no model stack, so there it is skipped."""
    pytest.importorskip("torch", reason="the Python dream module needs torch")
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pseudolife_memory.memory.dream import _format_signals, split_session_context
    for text, cap in ((harness.DIGEST_B_CONTEXT, harness.DIGEST_CHARS), ("ab\ncd", 1),
                      ("x" * 9 + "\ny", 4)):
        assert harness.split_context(text, cap) == split_session_context(text, cap)
    for sigs in (harness.RULE_SIGNALS, harness.LESSON_SIGNALS, [harness.INFERRED_SIGNAL],
                 harness.LESSON_SIGNALS_3 + harness.LESSON_SIGNALS_4 + harness.LESSON_SIGNALS_5):
        assert harness.fmt_signals(sigs) == _format_signals(sigs)


def test_lesson_write_clocks_and_episode_tokens_normalize(harness):
    import time
    now = time.time()
    assert harness._recent_clock(now) == "<clock>"
    assert harness._recent_clock(1700000000.0) == 1700000000.0
    assert harness._recent_clock_ms(now * 1000) == "<clock>"
    assert harness._recent_clock_ms(1700000000000) == 1700000000000
    emap = {"a" * 32: "<ep#0>"}
    assert harness._rename(["episode:" + "a" * 32, "episode:x"], emap) == [
        "episode:<ep#0>", "episode:x"]
    # Provenance sorted on raw ids is sorted again on the renamed tokens, so
    # two episodes order alike on both sides; an unsorted list keeps order.
    emap = {"f" * 32: "<ep#0>", "1" * 32: "<ep#1>"}
    raw = sorted(["episode:" + "f" * 32, "episode:" + "1" * 32, "signal:1"])
    assert harness._rename(raw, emap) == ["episode:<ep#0>", "episode:<ep#1>", "signal:1"]
    assert harness._rename(["signal:1", "episode:" + "f" * 32], emap) == [
        "signal:1", "episode:<ep#0>"]


def _lesson_state(signals, lesson, audit):
    return {"rows": {
        "public.outcome_signals": {"columns": ["id", "created_at"],
                                   "rows": [[i + 1, t] for i, t in enumerate(signals)]},
        "public.lessons": {"columns": ["id", "asserted_at", "last_confirmed", "tx_time",
                                       "valid_time"], "rows": [[1, *lesson]]},
        "public.store_decisions": {"columns": ["id", "record"],
                                   "rows": [[1, {"lesson": "x", "asserted_at": audit}]]},
    }}


def test_lesson_clocks_keep_their_equality_and_order(harness, monkeypatch):
    import time
    monkeypatch.setattr(harness.w1a, "scrub_declared_rows", lambda state, before: {"state": state})

    def norm(state):
        return harness.normalize_state(state, {"rows": {}})[0]["rows"]

    def side(t, valid=None, asserted=None, audit=None, confirmed=None):
        s1, s2, w = t, t + 1.5, t + 9.25
        return _lesson_state([s1, s2], [asserted or w, confirmed or w, confirmed or w,
                                        valid or s1], audit or w)

    py, rs = time.time() - 100, time.time() - 40
    good = norm(side(py))
    # Different wall clocks, the same pattern: equal after normalization.
    assert good == norm(side(rs))
    assert good["public.lessons"]["rows"] == [
        [1, "<clock#2>", "<clock#2>", "<clock#2>", "<clock#0>"]]
    assert good["public.store_decisions"]["rows"][0][1]["asserted_at"] == "<clock#2>"
    # valid_time from the newest source, or from now, differs.
    assert norm(side(rs, valid=rs + 1.5)) != good
    assert norm(side(rs, valid=rs + 9.25)) != good
    # an audit not naming its lesson's asserted_at differs
    assert norm(side(rs, audit=rs + 9.5)) != good
    # a confirm that moves asserted_at differs from one that keeps it
    kept = norm(side(py, asserted=py + 9.25, confirmed=py + 20))
    assert norm(side(rs, asserted=rs + 9.25, confirmed=rs + 20)) == kept
    assert norm(side(rs, asserted=rs + 20, confirmed=rs + 20)) != kept
    # explicit seeded stamps compare exactly; an old created_at keeps presence
    old = norm(_lesson_state([1700000000.0], [1700000001.0] * 4, 1700000001.0))
    assert old["public.lessons"]["rows"] == [[1] + [1700000001.0] * 4]
    assert old["public.outcome_signals"]["rows"] == [[1, "<clock>"]]


def test_idle_seconds_compare_within_the_longest_step(harness):
    py = {"isError": False, "value": {"backlog": 1, "idle_seconds": 12.5}}
    assert harness.idle_diffs(py, {"value": {"idle_seconds": 13.4}}, 1.0) == []
    assert harness.idle_diffs(py, {"value": {"idle_seconds": 14.0}}, 1.0) == [
        "idle_seconds: python 12.500 vs rust 14.000 (apart by more than 1.000 s)"]
    assert harness.idle_diffs(py, {"value": {"idle_seconds": -0.1}}, 5.0)[0].endswith(
        "(not both >= 0)")
    assert harness.idle_diffs(py, {"value": {"idle_seconds": None}}, 5.0)
    assert harness.idle_diffs(py, {"value": {}}, 5.0)
    assert harness.idle_diffs({"value": {"idle_seconds": 0.0}},
                              {"value": {"idle_seconds": 0.0}}, 0.0) == []


def test_run_clocks_never_finish_before_they_start(harness):
    raw = {"rows": {"public.dream_runs": {"columns": ["id", "started_at", "finished_at"],
                                          "rows": [[1, 10.0, 12.0], [2, 20.0, None],
                                                   [3, 30.0, 29.5]]}}}
    assert harness.run_clock_diffs("rust", raw) == [
        "dream run clock (rust bank): finished_at 29.5 before started_at 30.0"]
    answer = {"value": {"runs": [{"id": 1, "started_at": 5.0, "finished_at": 4.0}]}}
    assert harness.run_clock_diffs("python", {"rows": {}}, answer) == [
        "dream run clock (python answer): finished_at 4.0 before started_at 5.0"]


def test_extractor_keys_reach_only_their_own_endpoint(harness):
    from stub_extractor import auth_digest
    scn = {"extractor_keys": {"primary": "kp", "fallback": "kf"}}
    ok = [{"method": "GET", "path": "/health", "authorization": None},
          {"method": "POST", "path": "/fallback/v1/chat/completions",
           "authorization": auth_digest("Bearer kf")},
          {"method": "POST", "path": "/v1/chat/completions",
           "authorization": auth_digest("Bearer kp")}]
    assert harness.extractor_key_diffs("rust", scn, ok) == []
    bad = [{**ok[1], "authorization": auth_digest("Bearer kp")},
           {**ok[0], "authorization": auth_digest("Bearer kp")}]
    diffs = harness.extractor_key_diffs("rust", scn, bad)
    assert len(diffs) == 2 and "expected the fallback key" in diffs[0]
    assert "expected no key" in diffs[1]
    assert harness.extractor_key_diffs("rust", {}, bad) == []
    env = harness.extractor_env({**scn, "extractor": {}}, type("S", (), {
        "base_url": "b", "fallback_base_url": "f"})())
    assert env["PSEUDOLIFE_DREAM_API_KEY"] == "kp"
    assert env["PSEUDOLIFE_DREAM_FALLBACK_API_KEY"] == "kf"


def test_dream_answers_normalize_stall_clocks_and_run_cursors(harness):
    ts = {1700000000.5: "<ts:3>"}
    value = {"stall": {"since": 1.0, "reason": "extractor_error", "last_success_at": None},
             "last_stall": {"since": 2.0, "recovered_at": 3.0},
             "last_dream_extractor": {"which": "primary", "base_url": "http://stub/v1", "at": 4.0},
             "runs": [{"id": 1, "cursor_before": 0.0, "cursor_after": 1700000000.5,
                       "finished_at": 5.0}]}
    out = harness.dream_tokens(value, ts)
    assert out["stall"] == {"since": "<clock>", "reason": "extractor_error",
                            "last_success_at": None}
    assert out["last_stall"]["recovered_at"] == "<clock>"
    assert out["last_dream_extractor"]["at"] == "<clock>"
    assert out["runs"][0] == {"id": 1, "cursor_before": 0.0, "cursor_after": "<ts:3>",
                              "finished_at": "<clock>"}


def test_deep_dream_scenarios_register_their_mutants_and_normalizers(harness, tmp_path):
    names = sorted(n for n in harness.SCENARIOS if n.startswith("dream-deep-"))
    assert names == ["dream-deep-apply", "dream-deep-apply-cautious", "dream-deep-bound",
                     "dream-deep-dry", "dream-deep-exclusion", "dream-deep-snapshot-failed"]
    for n in names:
        # No background tick or sweep may run a deep dream behind the ops.
        assert "auto_tick: false" in harness.SCENARIOS[n]["config"]
        assert "dream:\n    enabled: false" in harness.SCENARIOS[n]["config"]
    deep_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-dream-deep-")]
    assert len(deep_mutants) == 9
    text = (Path(__file__).resolve().parents[1] / "src" / "dream" / "deep.rs").read_text(
        encoding="utf-8")
    assert all(f'"{m}"' in text for m in deep_mutants)
    # Orphan ages compare numerically within 0.1 day, then mask; the
    # snapshot name compares by shape.
    def ages(*days):
        return {"value": {"would_orphan": [{"entity": f"e{i}", "age_days": d}
                                           for i, d in enumerate(days)]}}
    py, rs = ages(1077.3, 0.0, 1077.3), ages(1077.4, 0.0, 1077.3)
    assert harness.orphan_age_diffs(py, rs) == []
    assert [o["age_days"] for o in py["value"]["would_orphan"]] == ["<age>"] * 3
    assert py == rs
    # 100 s apart is under 0.1 day, but a day apart is caught even when the
    # order of ages agrees.
    py, rs = ages(1077.3, 2.0), ages(1076.3, 2.0)
    diffs = harness.orphan_age_diffs(py, rs)
    assert len(diffs) == 1 and "would_orphan[0].age_days" in diffs[0]
    py, rs = ages(1077.3, "x"), ages(1077.3, 2.0)
    assert harness.orphan_age_diffs(py, rs) == []
    assert py["value"]["would_orphan"][1]["age_days"] == "x"
    got = harness.dream_tokens({"snapshot": "graph-20261020-121500.json",
                                "would_orphan": [{"entity": "a", "age_days": 3.1}]}, {})
    assert got["snapshot"] == "<snapshot>"
    assert got["would_orphan"][0]["age_days"] == 3.1
    # The snapshot file op reads the newest file and drops clocks.
    snaps = tmp_path / "data" / "graph_snapshots"
    snaps.mkdir(parents=True)
    (snaps / "graph-20261020-121500.json").write_text('{"entities": []}', encoding="utf-8")
    (snaps / "graph-20261020-121501.json").write_text(
        '{"entities": [{"id": 1, "created_at": 5.5}], "edges": []}', encoding="utf-8")
    v = harness.home_op({"op": "snapshot_file"}, tmp_path)["value"]
    assert v["files"] == 2 and v["name_shape"]
    assert v["tables"]["entities"] == {"columns": ["id", "created_at"], "rows": [[1, "<clock>"]]}


def test_merge_judge_scenarios_isolate_the_judge_and_register_their_mutants(harness):
    names = sorted(n for n in harness.SCENARIOS if n.startswith("judge-merge-"))
    assert names == ["judge-merge-alias", "judge-merge-auto", "judge-merge-auto-reject",
                     "judge-merge-duplicates", "judge-merge-endpoint-errors",
                     "judge-merge-first-moved", "judge-merge-moved",
                     "judge-merge-reconsider", "judge-merge-replay", "judge-merge-same-model",
                     "judge-merge-served-mismatch", "judge-merge-shadow",
                     "judge-merge-tiebreak", "judge-merge-tiebreak-same-model",
                     "judge-merge-tiebreak-skip", "judge-merge-tiebreak-unknown-second",
                     "judge-merge-unencodable", "judge-merge-unknown-served"]
    for n in names:
        scn = harness.SCENARIOS[n]
        config = scn["config"]
        # The sweep's judge runs; every other step a tick would run is off.
        assert "dream:\n    enabled: true" in config
        for off in ("auto_tick: false", "analyzer_file_duplicates: false",
                    'link_judge_mode: "off"', 'junk_judge_mode: "off"',
                    'curation_judge_mode: "off"', 'candidate_judge_mode: "off"',
                    "min_batch: 1000000", "idle_seconds: 1000000000", "infer_outcomes: false"):
            assert off in config, (n, off)
        assert scn["shared_stub"] and n in harness.EXTERNAL_WRITES
        # The proposals land out of band and every such op waits for both
        # judges to settle; no proposal is seeded where a judge could race
        # the first comparison.
        assert all("entity_proposals" not in op.get("sql", "") for op in scn["seed"])
        assert all(op.get("settle") for op in scn["ops"] if op["op"] in ("sql", "restart"))
        # Every scenario asserts the path it is written for on both banks.
        assert any(op.get("expect") for op in scn["ops"]), n
        for op in scn["ops"]:
            if op["op"] == "restart":
                for off in ("auto_tick: false", 'link_judge_mode: "off"'):
                    assert off in op["config"], (n, off)
    judge_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-dream-judge-")]
    assert len(judge_mutants) == 15
    src = Path(__file__).resolve().parents[1] / "src" / "dream"
    text = "".join((src / f).read_text(encoding="utf-8")
                   for f in ("judge.rs", "review_judgments.rs"))
    assert all(f'"{m}"' in text for m in judge_mutants)
    # A decision record's clocks, and its proposal snapshot's verdict
    # clocks, keep only null-or-not; the rest compares exactly.
    rec = {"recorded_at": 1.5, "ended_at": None, "decided_at": 2.5, "dismissed_at": 3.0,
           "fingerprint": "ab", "proposal": {"judged_at": 4.0, "judged3_at": None, "id": 7}}
    assert harness.decision_record_clocks(rec) == {
        "recorded_at": "<clock>", "ended_at": None, "decided_at": "<clock>",
        "dismissed_at": "<clock>", "fingerprint": "ab",
        "proposal": {"judged_at": "<clock>", "judged3_at": None, "id": 7}}
    for col in ("judged_at", "judged2_at", "judged3_at"):
        assert ("entity_proposals", col) in harness.CLOCK
    # A listed decision's recorded_at is a clock only in memory_graph_review's
    # automatic_decisions listing; elsewhere it compares exactly.
    now = time.time()
    a = {"recorded_at": now, "automatic_decisions": [{"recorded_at": now, "state": "applied"}]}
    b = {"recorded_at": now + 1, "automatic_decisions": [{"recorded_at": now + 1,
                                                          "state": "applied"}]}
    harness.normalize_records(a, b)
    assert a["automatic_decisions"] == b["automatic_decisions"]
    assert a["recorded_at"] != b["recorded_at"]


def test_expect_reports_each_bank_that_misses_its_path(harness, monkeypatch):
    import types
    answers = {"py": [(True,), (False,)], "rs": [(True,), (None,)]}

    class Conn:
        def __init__(self, dsn):
            self.dsn = dsn
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def execute(self, query):
            return types.SimpleNamespace(fetchone=lambda: answers[self.dsn].pop(0))

    fake = types.SimpleNamespace(connect=lambda dsn, autocommit=True: Conn(dsn))
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    out = harness.expect_diffs({"expect": ["q1", "q2"]}, {"python": "py", "rust": "rs"})
    assert out == ["expect python: q2", "expect rust: q2"]


def test_warmups_are_awaited_per_side_until_each_probe_lands(harness, monkeypatch):
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    polls = {"python": 0, "rust": 0}

    def landed(dsn, before):
        polls[dsn] += 1
        return polls[dsn] >= (2 if dsn == "python" else 4)

    sides = {"python": "python", "rust": "rust"}
    assert harness.wait_warmups(sides, {"python": {}, "rust": {}}, landed=landed) == []
    assert polls == {"python": 2, "rust": 4}  # no side is polled once it has landed


def test_a_probe_that_never_lands_is_a_difference_naming_its_side(harness, monkeypatch):
    monkeypatch.setattr(harness.time, "sleep", lambda s: None)
    diffs = harness.wait_warmups({"python": "py", "rust": "rs"}, {"python": {}, "rust": {}},
                                 deadline_s=0, landed=lambda dsn, before: dsn == "py")
    assert diffs == ["warmup: rust's startup probe event did not land within 0s"]


@pytest.mark.parametrize("config,expected", [
    (None, True), ("memory:\n  lessons:\n    enabled: false\n", True),
    ("memory:\n  retrieval_log:\n    enabled: false\n", False),
])
def test_the_probe_is_expected_only_while_the_retrieval_log_is_on(harness, config, expected):
    assert harness.warmup_expected({"config": config}) is expected
    # Every scenario that turns the log off says so in its config.
    assert any(not harness.warmup_expected(s) for s in harness.SCENARIOS.values())


@pytest.mark.parametrize("log_on", [True, False])
def test_the_init_snapshot_waits_for_both_warmups(harness, monkeypatch, tmp_path, log_on):
    events = []

    class Daemon:
        def __init__(self, *args, **kwargs):
            self.port = 1

        def start(self, timeout):
            pass

        def launch(self):  # daemons.start_all launches every arm, then waits
            return self

        def wait(self, timeout, stages=None):
            return self

        def stop(self):
            pass

    def warmups(dsns, before):
        events.append("warmup")
        return ["warmup: rust's startup probe event did not land within 300s"]

    config = None if log_on else "memory:\n  retrieval_log:\n    enabled: false\n"
    monkeypatch.setitem(harness.SCENARIOS, "warmup-order",
                        {"config": config, "seed": [], "ops": [harness.sql("SELECT 1")]})
    monkeypatch.setattr(harness, "prepare_template", lambda *a: "template")
    monkeypatch.setattr(harness.pg, "create", lambda name, **kw: name)
    monkeypatch.setattr(harness.dbstate, "dump", lambda dsn: events.append("dump") or {"rows": {}})
    monkeypatch.setattr(harness.daemons, "python_daemon", Daemon)
    monkeypatch.setattr(harness.daemons, "rust_daemon", Daemon)
    monkeypatch.setattr(harness.w1a, "wait_settled", lambda *a, **kw: events.append("settled"))
    monkeypatch.setattr(harness, "wait_warmups", warmups)
    monkeypatch.setattr(harness, "Mcp", lambda port: SimpleNamespace(init=lambda: None))
    monkeypatch.setattr(harness, "run_op", lambda *a: {"isError": False, "value": None})
    monkeypatch.setattr(harness, "normalize_state", lambda state, before, *ts: ({}, {}, {}))
    monkeypatch.setattr(harness, "compare_response", lambda *a: [])
    result = harness._run_scenario("warmup-order", tmp_path / "candidate", tmp_path, "live",
                                   False, {}, None, [])
    init = events.index("settled")
    assert events[:init] == ["dump", "dump"]  # the pre-start states
    assert events[init + 1:] == (["warmup"] if log_on else []) + ["dump"] * 4
    first, second = result["steps"]
    assert first["diffs"] == (warmups(None, None) if log_on else [])
    assert second["diffs"] == []  # reported once, at the init step


@pytest.mark.parametrize("before,prior", [
    ({"rows": {}}, []),
    ({"rows": {"public.retrieval_events": {"columns": ["query_text", "id"],
                                           "rows": [["warmup probe", 1], ["other", 4]]}}}, [1, 4]),
])
def test_a_template_probe_does_not_count_as_this_boots_probe(harness, monkeypatch, before, prior):
    seen = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            seen.append((" ".join(sql.split()), params))
            return SimpleNamespace(fetchone=lambda: None)

    monkeypatch.setitem(sys.modules, "psycopg",
                        SimpleNamespace(connect=lambda dsn, autocommit: Conn()))
    assert harness.warmup_landed("dsn", before) is False
    (sql, params), = seen
    assert "query_text = 'warmup probe'" in sql and "origin = 'search'" in sql
    assert "NOT (id = ANY(%s::bigint[]))" in sql and params == (prior,)
