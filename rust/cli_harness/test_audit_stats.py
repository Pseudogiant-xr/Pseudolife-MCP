"""The stats comparison must retain every computed field and reject bad clocks."""
import base64
import copy
import json
from pathlib import Path

import pytest

from .rows import audit


def test_committed_stats_fixture_is_reproducible_from_executed_oracle():
    fixture = Path(__file__).resolve().parents[1] / "shim/src/cli/board_audit/testdata/stats.json"
    assert json.loads(fixture.read_text(encoding="utf-8")) == audit.stats_fixture()


def observation():
    from pseudolife_memory import __version__
    from pseudolife_memory.board_audit_stats import compute_stats
    report = compute_stats([], [], [], since=1000.0, until=5000.0,
                           now=6000.123, version=__version__, sources={})
    return {"arm": "python", "window": [6000.0, 6001.0], "stats_defaults": False,
            "stdout": base64.b64encode((json.dumps(report, separators=(",", ":")) + "\n").encode()).decode(),
            "stderr": "", "exit": 0, "files": {}}


def test_only_validated_clock_and_version_are_replaced():
    obs = observation()
    before = json.loads(base64.b64decode(obs["stdout"]))
    audit.stats_clock(obs)
    after = json.loads(base64.b64decode(obs["stdout"]))
    assert after.pop("generated_at") == "<generated_at>"
    assert after.pop("version") == "<version>"
    before.pop("generated_at")
    before.pop("version")
    assert after == before


@pytest.mark.parametrize("field,value", [("generated_at", 50), ("version", "invented")])
def test_invalid_runtime_fields_fail_the_comparison(field, value):
    obs = observation()
    report = json.loads(base64.b64decode(obs["stdout"]))
    report[field] = value
    obs["stdout"] = base64.b64encode(json.dumps(report).encode()).decode()
    with pytest.raises(AssertionError):
        audit.stats_clock(obs)


def test_computed_counts_and_output_file_payload_are_not_hidden():
    obs = observation()
    bad = copy.deepcopy(obs)
    report = json.loads(base64.b64decode(bad["stdout"]))
    report["mail_latency"]["sends"] = 3
    bad["stdout"] = base64.b64encode(json.dumps(report).encode()).decode()
    obs["files"]["out.jsonl"] = "file:" + obs["stdout"]
    bad["files"]["out.jsonl"] = "file:" + bad["stdout"]
    audit.stats_clock(obs)
    audit.stats_clock(bad)
    assert obs["stdout"] != bad["stdout"]
    assert obs["files"] != bad["files"]
