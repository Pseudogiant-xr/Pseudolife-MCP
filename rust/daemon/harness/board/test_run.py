"""Lifecycle checks for the board runner, independent of a live database."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board import run  # noqa: E402


@pytest.mark.parametrize("mode", ["live", "golden"])
def test_every_arm_waits_for_storage_before_board_reads(monkeypatch, tmp_path, mode):
    ready = set()
    monkeypatch.setattr(run.pg, "create", lambda name: name)
    monkeypatch.setattr(run.pg, "drop", lambda name: None)
    monkeypatch.setattr(run, "start", lambda side, *args: SimpleNamespace(port=side, stop=lambda: None))
    monkeypatch.setattr(run, "wait_ready", lambda port, **kw: ready.add(port))
    monkeypatch.setattr(run, "load_golden", lambda name: {})
    monkeypatch.setattr(run, "dump", lambda dsn: {})
    monkeypatch.setattr(run, "credential_check", lambda *args: [])

    def check(dsn):
        assert "rust" in ready, "Rust storage was read before readiness"
        if mode == "live":
            assert "python" in ready
        return []

    monkeypatch.setattr(run, "chain_check", check)
    scenario = SimpleNamespace(name="startup", steps=lambda: [])
    run.run_scenario(scenario, Path("unused"), tmp_path, mode, False)


@pytest.mark.parametrize("require_embedder,expected_calls", [(False, 2), (True, 3)])
def test_readiness_requires_healthy_storage_and_the_requested_tier(
        monkeypatch, require_embedder, expected_calls):
    replies = iter([{"embedder": {}, "db": "error: fixture"},
                    {"db": "ok"}, {"db": "ok", "embedder": {}}])
    calls = []

    def health(*args, **kwargs):
        calls.append(args)
        return {"json": next(replies)}

    monkeypatch.setattr(run, "call", health)
    monkeypatch.setattr(run.time, "sleep", lambda _: None)
    run.wait_ready(1, require_embedder=require_embedder)
    assert len(calls) == expected_calls
