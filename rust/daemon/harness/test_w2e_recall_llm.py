"""The recall-llm scenarios' wiring, without daemons or a database."""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import w2e_recall_llm as rl  # noqa: E402
from stub_extractor import StubExtractor  # noqa: E402


def seed_body(prompt: str) -> bytes:
    """A body shaped as ``simple_complete`` sends it."""
    return json.dumps({"model": "stub-model",
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.0, "max_tokens": 256, "stream": False}).encode()


def test_the_scenario_runs_the_llm_driver_with_a_key_and_nothing_behind_it():
    scn = rl.scenarios([])["recall-llm"]
    assert "  recall:\n    driver: llm\n" in scn["config"]
    assert "  dream:\n    enabled: false\n    stall_notice: false\n" in scn["config"]
    assert "  lessons:\n    enabled: false\n" in scn["config"]
    assert "  graph_insight:\n    enabled: false\n" in scn["config"]
    assert scn["extractor_keys"]["primary"]


def test_each_model_calling_recall_has_its_reply_in_turn():
    scn = rl.scenarios([])["recall-llm"]
    calls = [op for op in scn["ops"]
             if op["name"] == "memory_recall" and op["args"]["query"].strip()]
    replies = scn["extractor"]["replies"]
    assert len(calls) == len(replies)
    stub = StubExtractor(scn["extractor"], "unit")
    auth = "sha256:side"
    got = [stub.lookup(seed_body(f"prompt {i}"), "primary", auth) for i in range(len(calls))]
    assert got == replies
    # Each reply answers once per caller: a further call goes unanswered.
    assert stub.lookup(seed_body("one more"), "primary", auth) is None
    redirect = next(r for r in replies if r.get("status") == 302)
    assert redirect["headers"]["Location"].startswith("/v1/")


def import_w2e(monkeypatch):
    import importlib

    import pgdisposable as pg
    # w2e changes the shared name admission on import (test_w2e_lifecycle's
    # fixture): restore it afterwards for the other harness test files.
    monkeypatch.setattr(pg, "DISPOSABLE_NAME", pg.DISPOSABLE_NAME)
    return importlib.import_module("w2e")


def test_unused_replies_fail_an_opted_in_scenario_per_side(monkeypatch):
    w2e = import_w2e(monkeypatch)
    spec = {"replies": [rl.reply("first"), rl.reply("second")]}
    stubs = {"python": StubExtractor(spec, "py"), "rust": StubExtractor(spec, "rs")}
    scn = {"extractor": spec, "extractor_all_used": True}
    stubs["python"].lookup(seed_body("p"), "primary", "sha256:py")
    stubs["python"].lookup(seed_body("p"), "primary", "sha256:py")
    stubs["rust"].lookup(seed_body("p"), "primary", "sha256:rs")
    diffs = w2e.unused_reply_diffs(scn, stubs)
    assert len(diffs) == 1 and diffs[0].startswith("extractor rust: reply never used")
    assert '"content": "second"' in diffs[0]
    stubs["rust"].lookup(seed_body("p"), "primary", "sha256:rs")
    assert w2e.unused_reply_diffs(scn, stubs) == []
    # Not opted in: nothing is checked.
    assert w2e.unused_reply_diffs({"extractor": spec}, {
        side: StubExtractor(spec, side) for side in ("python", "rust")}) == []


def test_a_recall_llm_run_missing_a_model_call_is_reported(monkeypatch):
    w2e = import_w2e(monkeypatch)
    scn = rl.scenarios([])["recall-llm"]
    assert scn["extractor_all_used"] is True
    calls = [op for op in scn["ops"]
             if op["name"] == "memory_recall" and op["args"]["query"].strip()]
    stubs = {side: StubExtractor(scn["extractor"], side) for side in ("python", "rust")}
    # As if one recall never reached the model (or one op were dropped).
    for side, stub in stubs.items():
        for i in range(len(calls) - 1):
            stub.lookup(seed_body(f"prompt {i}"), "primary", f"sha256:{side}")
    diffs = w2e.unused_reply_diffs(scn, stubs)
    assert [d.split(":")[0] for d in diffs] == ["extractor python", "extractor rust"]
    for side, stub in stubs.items():
        stub.lookup(seed_body("last"), "primary", f"sha256:{side}")
    assert w2e.unused_reply_diffs(scn, stubs) == []


def test_the_mutant_is_registered_and_compiled_in(monkeypatch):
    w2e = import_w2e(monkeypatch)
    assert set(rl.MUTANTS) <= set(w2e.MUTANTS)
    assert "recall-llm" in w2e.SCENARIOS
    src = (HERE.parent / "src" / "recall.rs").read_text(encoding="utf-8")
    for m in rl.MUTANTS:
        assert re.search(rf'mutants::active\("{re.escape(m)}"\)', src), m
