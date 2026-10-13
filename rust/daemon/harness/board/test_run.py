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


@pytest.mark.parametrize("failure", ["readiness", "second_start", "stop"])
def test_failed_scenarios_clean_up_every_owned_bank(monkeypatch, tmp_path, failure):
    created, dropped, stopped = [], [], []
    monkeypatch.setattr(run.pg, "create", lambda name: created.append(name) or name)
    monkeypatch.setattr(run.pg, "drop", dropped.append)
    monkeypatch.setattr(run, "dump", lambda _: {})
    monkeypatch.setattr(run, "credential_check", lambda *args: [])
    monkeypatch.setattr(run, "chain_check", lambda _: [])

    def stop(side):
        stopped.append(side)
        if failure == "stop" and side == "rust":
            raise RuntimeError("fixture stop")

    def start(side, *args):
        if failure == "second_start" and side == "rust":
            raise RuntimeError("fixture startup")
        return SimpleNamespace(port=side, stop=lambda: stop(side))

    def ready(*args, **kwargs):
        if failure == "readiness":
            raise RuntimeError("fixture readiness")

    monkeypatch.setattr(run, "start", start)
    monkeypatch.setattr(run, "wait_ready", ready)
    scenario = SimpleNamespace(name="cleanup", steps=lambda: [])
    with pytest.raises(RuntimeError, match="fixture"):
        run.run_scenario(scenario, Path("unused"), tmp_path, "live", False)
    assert set(dropped) == set(created)
    assert len(dropped) == len(created) == 2
    assert set(stopped) == ({"python"} if failure == "second_start" else {"python", "rust"})


@pytest.mark.parametrize("mode,selection", [
    ("live", ["identitiy"]), ("golden", ["identitiy"]),
    ("mutants", ["identitiy"]), ("golden", ["mixed"]), ("live", []),
])
def test_invalid_selection_fails_before_any_scenario(monkeypatch, mode, selection):
    monkeypatch.setattr(sys, "argv", ["board.run", mode, "--rust-bin", "unused", "--only", *selection])
    monkeypatch.setattr(run.daemons, "scratch_root", lambda: Path("unused"))
    monkeypatch.setattr(run, "run_scenario", lambda *args: pytest.fail("invalid selection ran a scenario"))
    with pytest.raises(SystemExit) as raised:
        run.main()
    assert raised.value.code == 2
