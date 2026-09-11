"""The local coordination instrument reports bounded, explicitly synthetic evidence."""
import json

import pytest


def test_percentile_uses_nearest_rank_and_empty_is_unknown():
    from evals.coordination_bench import percentile
    assert percentile([], 95) is None
    assert percentile(list(range(1, 21)), 95) == 19
    assert percentile([9, 1, 5], 50) == 5


def test_summary_keeps_control_comparison_and_host_uncertainty(tmp_path):
    from evals.coordination_bench import summarize, write_summary
    arms = {name: {"ordinary_ms": values, "enqueue_to_event_ms": [], "harness_ack_ms": [],
                   "requests": 3, "context_bytes": 10, "duplicate_ids": 0}
            for name, values in {"disabled": [1, 2, 3], "pull": [2, 3, 4], "channel": [3, 4, 5]}.items()}
    summary = summarize(arms)
    assert summary["arms"]["pull"]["ordinary_p95_delta_ms"] == 1
    assert summary["arms"]["channel"]["ordinary_p95_delta_ms"] == 2
    assert summary["host_wake_ms"] is None and summary["model_ack_ms"] is None
    assert summary["ack_actor"] == "test_harness"
    path = tmp_path / "summary.json"
    write_summary(path, summary)
    assert json.loads(path.read_text()) == summary


def test_trace_rejects_sensitive_fields_and_flushes_each_record(tmp_path):
    from evals.coordination_bench import Trace
    path = tmp_path / "trace.jsonl"
    with Trace(path) as trace:
        trace.write(arm="pull", kind="request", action="send", status=200, elapsed_ms=1.0)
        assert len(path.read_text().splitlines()) == 1
        with pytest.raises(ValueError, match="trace field"):
            trace.write(credential="must-not-be-written")
    assert "must-not-be-written" not in path.read_text()


def test_database_override_is_refused_before_connection(monkeypatch):
    from evals.coordination_bench import disposable_database
    monkeypatch.setenv("PSEUDOLIFE_TEST_DATABASE_URL", "postgresql://example.invalid/unrelated")
    with pytest.raises(ValueError, match="override"):
        with disposable_database():
            pytest.fail("database opened")
