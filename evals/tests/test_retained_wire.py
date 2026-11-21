"""Targeted second Linux dev box test with a tiny untrained embedding fixture."""
import json
import os
from pathlib import Path

import pytest

from evals import ladder_sweep as ladder, longmemeval_bench as lme, regression_gate as gate
from evals.retained_daemon import DaemonSetup, Endpoint, bench_config
from rust.daemon.harness import embedding_fixture
from rust.daemon.harness.stub_extractor import StubExtractor


@pytest.fixture
def setup(tmp_path):
    required = ("PL_RETAINED_TEST_DAEMON", "PL_RETAINED_TEST_REVISION", "ORT_DYLIB_PATH")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.fail("Set these dev-box fixture variables before running the wire test: "
                    + ", ".join(missing), pytrace=False)
    binary = Path(os.environ["PL_RETAINED_TEST_DAEMON"])
    library = Path(os.environ["ORT_DYLIB_PATH"])
    model_dir = tmp_path / "embedding"
    embedding_fixture.create(model_dir, dimension=1024)
    return DaemonSetup(binary, os.environ["PL_RETAINED_TEST_REVISION"], model_dir, library,
                       tmp_path / "banks")


def fixture_config():
    config = bench_config()
    config["embedding"]["model_name"] = "fixture-embedding"
    return config


def test_longmemeval_uses_the_rust_public_wire(setup):
    old_date, new_date = "2024/11/01", "2024/11/02"
    old_line = "The build tool is maple."
    new_line = "The build tool is now cedar."
    data = {"question_id": "wire", "question_type": "knowledge-update",
            "question": "What is the build tool?", "answer": "cedar", "question_date": new_date,
            "haystack_dates": [new_date, old_date],
            "haystack_sessions": [[{"role": "user", "content": new_line}],
                                  [{"role": "user", "content": old_line}]]}

    def reply(date, line, value):
        return {"kind": "claims", "user": f"[1] [{date}] user: {line}", "times": 1,
                "content": json.dumps({"claims": [{"entity": "build", "attribute": "tool",
                                                    "value": value, "source": 1,
                                                    "confidence": 0.9}]})}

    config = fixture_config()
    with StubExtractor({"served_model": "fixture-extractor", "replies": [
            reply(old_date, old_line, "maple"), reply(new_date, new_line, "cedar"),
    ]}, "retained-wire") as stub:
        endpoint = Endpoint(stub.base_url, "bench", "synthetic", "fixture-extractor")
        row = lme.extract_question(data, lambda active: setup.bank(config, endpoint if active else None))
        assert not stub.unused()
        assert all(call["answered"] for call in stub.drain())

    assert row["consolidation"]["claims"] == 2
    assert "cedar" in row["contexts"]["cortex"]
    assert "maple" in row["contexts"]["cortex"]
    assert "cedar" in row["contexts"]["rag_control"]


def test_gate_replays_current_sets_and_scalar_history_over_mcp(setup):
    question = {"question_id": "gate", "question_type": "knowledge-update", "answer": "cedar",
                "question": "What is the build tool?", "question_date": "2024/11/02",
                "haystack_dates": ["2024/11/01"],
                "haystack_sessions": [[{"role": "user", "content": "The build tool is cedar."}]]}
    slots = [{"entity": "build", "attribute": "tool", "kind": "scalar", "values": ["maple", "cedar"]},
             # These fixture tokens have different vectors, so member cosine dedup keeps both.
             {"entity": "build", "attribute": "checks", "kind": "set",
              "values": ["memory cache", "cursor query"]}]
    row = gate.contexts_for(question, "frozen raw bytes", slots, lambda: setup.bank(fixture_config()))
    assert row["contexts"]["rag_control"] == "frozen raw bytes"
    assert all(value in row["contexts"]["cortex"] for value in ("maple", "cedar", "memory cache", "cursor query"))


def test_ladder_uses_rust_dream_results_and_separate_raw_control(setup, monkeypatch):
    old_line = "[2024/11/01] user: The build tool is maple by default."
    new_line = "[2024/11/02] user: The build tool has been cedar since the migration."
    monkeypatch.setattr(ladder, "CORPUS", [old_line, new_line])
    monkeypatch.setattr(ladder, "PAIRS", [{"question": "What is the build tool?", "gold": "cedar", "stale": "maple"}])
    config = fixture_config()
    with setup.bank(config) as bank:
        ladder.ingest(bank, ladder.CORPUS)
        contexts, _ = ladder.read_contexts(bank, cortex=False)
    assert ladder.measure(contexts)["gold_recoverable"] == 1, contexts
    reply = {"kind": "claims", "user": f"[1] {old_line}\n\n[2] {new_line}", "times": 1,
             "content": json.dumps({"claims": [{"entity": "build", "attribute": "tool", "value": "cedar",
                                                 "source": 2, "confidence": 0.9}]})}
    with StubExtractor({"served_model": "fixture-extractor", "replies": [reply]}, "ladder-wire") as stub:
        endpoint = Endpoint(stub.base_url, "bench", "synthetic", "fixture-extractor")
        result = ladder.run_rung(setup.bank, config, endpoint)
        assert not stub.unused()
    assert result["gold_recoverable"] == 1
    assert result["stale_leak"] == 0
