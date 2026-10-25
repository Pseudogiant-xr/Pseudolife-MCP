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


def test_the_mutant_is_registered_and_compiled_in(monkeypatch):
    import importlib

    import pgdisposable as pg
    # w2e changes the shared name admission on import (test_w2e_lifecycle's
    # fixture): restore it afterwards for the other harness test files.
    monkeypatch.setattr(pg, "DISPOSABLE_NAME", pg.DISPOSABLE_NAME)
    w2e = importlib.import_module("w2e")
    assert set(rl.MUTANTS) <= set(w2e.MUTANTS)
    assert "recall-llm" in w2e.SCENARIOS
    src = (HERE.parent / "src" / "recall.rs").read_text(encoding="utf-8")
    for m in rl.MUTANTS:
        assert re.search(rf'mutants::active\("{re.escape(m)}"\)', src), m
