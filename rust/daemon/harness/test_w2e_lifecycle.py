"""Numeric comparison and run ownership without starting real daemons."""
import argparse
import importlib
import json
import re
import sys
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
            nonlocal starts
            starts += 1
            self.started = True
            if (stage == "seed-start" or stage == "arm-start"
                    and starts == (2 if mode == "live" else 1)):
                raise FixtureFailure(stage)

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
    # The digest map step's split agrees with Python's.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from pseudolife_memory.memory.dream import _format_signals, split_session_context
    for text, cap in ((harness.DIGEST_B_CONTEXT, harness.DIGEST_CHARS), ("ab\ncd", 1),
                      ("x" * 9 + "\ny", 4)):
        assert harness.split_context(text, cap) == split_session_context(text, cap)
    for sigs in (harness.RULE_SIGNALS, harness.LESSON_SIGNALS, [harness.INFERRED_SIGNAL]):
        assert harness.fmt_signals(sigs) == _format_signals(sigs)
    stage_mutants = [m for m in harness.MUTANTS if m.startswith("mcp-dream-stage-")]
    assert len(stage_mutants) == 6
    src = Path(__file__).resolve().parents[1] / "src"
    text = "".join((src / f).read_text(encoding="utf-8") for f in (
        "dream/stages.rs", "dream/lessons.rs", "dream/stage_calls.rs", "graph_review_slots.rs"))
    assert all(f'"{m}"' in text for m in stage_mutants)


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
