"""Subprocess completion is observed before ownership cleanup and file hashing."""
from contextlib import contextmanager

from . import harness


def test_communicate_completion_is_an_upper_bound_recorded_before_cleanup(monkeypatch, tmp_path):
    timing = {}
    events = []

    class Process:
        returncode = 0

        def communicate(self, **kwargs):
            events.append("communicate-return")
            return b"raw stdout", b"raw stderr"

    @contextmanager
    def owned(*args, **kwargs):
        yield Process()
        assert timing["communicate_completed_monotonic"] == 12.5
        events.append("cleanup")

    monkeypatch.setattr(harness, "owned_process", owned)
    monkeypatch.setattr(harness.time, "monotonic", lambda: 12.5)
    response = harness.run_cli(["fixture"], [], cwd=tmp_path, env={}, timeout=10,
                               process_timing=timing)
    assert events == ["communicate-return", "cleanup"]
    assert response == {"exit_code": 0, "stdout_b64": "cmF3IHN0ZG91dA==", "stderr_b64": "cmF3IHN0ZGVycg=="}
    assert timing == {"communicate_completed_monotonic": 12.5}
