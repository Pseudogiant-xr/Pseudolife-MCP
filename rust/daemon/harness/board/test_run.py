"""Lifecycle checks for the board runner, independent of a live database."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board import run  # noqa: E402


@pytest.mark.parametrize("name,status,body", [
    ("coordination hub (authenticated)", 401, {"error": "agent_required"}),
    ("api agents coordination view", 200, {"agents": []}),
])
def test_foundation_compares_implemented_board_routes(name, status, body):
    import run as foundation

    case = next(c for c in foundation.Tokens().cases() if c["name"] == name)
    response = {"status": status, "headers": {}, "json": body}
    result = foundation.compare_case(case, response, response)
    assert not result["diffs"], result["diffs"]
    assert result["declared"] is None


@pytest.mark.parametrize("invalid", [False, True])
def test_foundation_snapshot_clock_is_validated_per_request(invalid):
    import run as foundation

    case = next(c for c in foundation.Tokens().cases() if c["name"] == "api agents coordination view")
    py = {"status": 200, "headers": {}, "json": {"snapshot_at": 1000.2},
          "_clock_window": [1000.0, 1000.5]}
    rs = {"status": 200, "headers": {}, "json": {"snapshot_at": 2000.2},
          "_clock_window": [2000.0, 2000.5]}
    if invalid:
        py["json"]["snapshot_at"] = rs["json"]["snapshot_at"] = 99.0
        rs["_clock_window"] = py["_clock_window"]
    assert bool(foundation.compare_case(case, py, rs)["diffs"]) is invalid


@pytest.mark.parametrize("retained_days,snapshot_at,has_problem", [(7, None, False), (90, None, True),
                                                                (7, 0.0, True)])
def test_scenario_clock_errors_reach_the_harness_result(
        monkeypatch, tmp_path, retained_days, snapshot_at, has_problem):
    monkeypatch.setattr(run.pg, "create", lambda name: name)
    monkeypatch.setattr(run.pg, "drop", lambda name: None)
    monkeypatch.setattr(run, "start", lambda side, *args: SimpleNamespace(port=side, stop=lambda: None))
    monkeypatch.setattr(run, "wait_ready", lambda *args, **kw: None)
    monkeypatch.setattr(run, "dump", lambda dsn: {})
    monkeypatch.setattr(run, "credential_check", lambda *args: [])
    monkeypatch.setattr(run, "chain_check", lambda dsn: [])
    timestamp = 1_790_000_000

    def response(arm, step):
        arm.windows.steps.append((timestamp, timestamp + 0.5))
        body = {
            "retention_days": retained_days,
            "retained_since": (timestamp // 86400 - retained_days) * 86400}
        if snapshot_at is not None:
            body["snapshot_at"] = snapshot_at
        return {"status": 200, "json": body}

    monkeypatch.setattr(run.Arm, "run", response)
    scenario = SimpleNamespace(name="retention", config_yaml="coordination:\n  audit_retention_days: 7\n",
                               steps=lambda: [SimpleNamespace(kind="http", name="history", write=False)])
    result = run.run_scenario(scenario, Path("unused"), tmp_path, "live", False)
    assert bool(run.summarize([result])[3]) is has_problem


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
    scenario = SimpleNamespace(name="startup", config_yaml=None, steps=lambda: [])
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
    scenario = SimpleNamespace(name="cleanup", config_yaml=None, steps=lambda: [])
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


@pytest.mark.parametrize("difference", ["response", "state", "chain"])
def test_mutants_refuse_a_broken_unmutated_baseline(monkeypatch, tmp_path, difference):
    calls = []
    monkeypatch.setenv("PSEUDOLIFE_DAEMON_MUTANT", "inherited-control")
    monkeypatch.setattr(sys, "argv", ["board.run", "mutants", "--rust-bin", "unused", "--only", "identity"])
    monkeypatch.setattr(run.daemons, "scratch_root", lambda: tmp_path)

    def scenario(*args):
        calls.append(run.os.environ.get("PSEUDOLIFE_DAEMON_MUTANT"))
        return {"steps": [{"diffs": ["baseline"] if difference == "response" else []}],
                "state_diffs": ["baseline"] if difference == "state" else [],
                "chain": ["baseline"] if difference == "chain" else []}

    monkeypatch.setattr(run, "run_scenario", scenario)
    assert run.main() == 1, "a broken baseline was credited as a mutant kill"
    assert calls == [None], "mutants ran before a clean unmutated baseline"
    assert run.os.environ["PSEUDOLIFE_DAEMON_MUTANT"] == "inherited-control"


def test_mutants_require_the_control_to_differ_after_a_clean_baseline(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("PSEUDOLIFE_DAEMON_MUTANT", "inherited-control")
    monkeypatch.setattr(sys, "argv", ["board.run", "mutants", "--rust-bin", "unused", "--only", "identity"])
    monkeypatch.setattr(run.daemons, "scratch_root", lambda: tmp_path)

    def scenario(*args):
        calls.append(run.os.environ.get("PSEUDOLIFE_DAEMON_MUTANT"))
        return {"steps": [{"diffs": []}], "state_diffs": [], "chain": []}

    monkeypatch.setattr(run, "run_scenario", scenario)
    assert run.main() == 1, "disabled controls must survive"
    # A REST scenario runs the REST mutants; the MCP tool mutants live in
    # mcp-tools and run only with it.
    assert calls == [None, *(m for m in run.MUTANTS if m not in run.MCP_MUTANTS)]
    assert run.os.environ["PSEUDOLIFE_DAEMON_MUTANT"] == "inherited-control"


def test_mcp_tools_runs_only_its_own_mutants(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sys, "argv", ["board.run", "mutants", "--rust-bin", "unused", "--only", "mcp-tools"])
    monkeypatch.setattr(run.daemons, "scratch_root", lambda: tmp_path)

    def scenario(*args):
        calls.append(run.os.environ.get("PSEUDOLIFE_DAEMON_MUTANT"))
        return {"steps": [{"diffs": []}], "state_diffs": [], "chain": []}

    monkeypatch.setattr(run, "run_scenario", scenario)
    run.main()
    assert calls == [None, *(m for m in run.MUTANTS if m in run.MCP_MUTANTS)]
