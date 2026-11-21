"""Targeted second Linux dev box test with a tiny untrained embedding fixture."""
import json
import os
from pathlib import Path

from evals import longmemeval_bench as lme
from evals.retained_daemon import DaemonSetup, Endpoint, bench_config
from rust.daemon.harness import embedding_fixture
from rust.daemon.harness.stub_extractor import StubExtractor


def test_longmemeval_uses_the_rust_public_wire(tmp_path):
    binary = Path(os.environ["PL_RETAINED_TEST_DAEMON"])
    library = Path(os.environ["ORT_DYLIB_PATH"])
    model_dir = tmp_path / "embedding"
    embedding_fixture.create(model_dir, dimension=1024)
    setup = DaemonSetup(binary, "0" * 40, model_dir, library, tmp_path / "banks")

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

    config = bench_config()
    config["embedding"]["model_name"] = "fixture-embedding"
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
