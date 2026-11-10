"""On a copy of a real bank the daemons' background timers stay out of reach.

The reaper's first tick comes one interval (300 s by default) after each
daemon starts. It is not tied to any step. On a real bank's copy it closes
or sweeps real idle sessions and empty roots, so whether the tick landed
before an arm's last dump depended on how long that arm had been up: Rust's
dumps showed one real client_sessions row and the deferred_empty_roots meta
changed, and Python's did not (w2e review-slots, dream-rollback-gates, graph
and console scenarios; forced run 8eb08c214, daemon box, 2026-11-09). Under
a real-bank base the harness now defaults the interval out of reach.
Python's 30 s autosave did the same with still-dirty cortex slots: each save
re-inserts the slot's rows, so its facts_id_seq ran one ahead of Rust's (w2e
console-facts-null, console-facts-null-audit, forget-memory). Scenarios that
set their own interval keep it, and synthetic arms are unchanged.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REAP = "PSEUDOLIFE_SESSION_REAP_SECONDS"
SAVE = "PSEUDOLIFE_MCP_AUTOSAVE_SECONDS"


def test_w2e_arms_on_a_real_base_get_no_reaper_tick(monkeypatch):
    import w2e
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    env = w2e.side_env({}, "dsn")
    assert env[REAP] == "86400" and env[SAVE] == "86400"
    # A scenario that drives a timer keeps its own interval.
    assert w2e.side_env(w2e.SCENARIOS["store-reaper"], "dsn")[REAP] == "0.1"
    assert w2e.side_env(w2e.SCENARIOS["counters-autosave"], "dsn")[SAVE] == "1"


def test_w2e_synthetic_arms_are_unchanged(monkeypatch):
    import w2e
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE", raising=False)
    env = w2e.side_env({}, "dsn")
    assert REAP not in env and SAVE not in env


def test_route_arms_on_a_real_base_get_no_reaper_tick(monkeypatch):
    import run

    class Plain(run.Scenario):
        env = {}

    class Reaper(run.Scenario):
        env = {REAP: "5"}

    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    assert run.common_env(Plain(), "dsn")[REAP] == "86400"
    assert run.common_env(Plain(), "dsn")[SAVE] == "86400"
    assert run.common_env(Reaper(), "dsn")[REAP] == "5"
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE")
    assert REAP not in run.common_env(Plain(), "dsn") and SAVE not in run.common_env(Plain(), "dsn")


def test_w2d_search_arms_on_a_real_base_get_no_timer_ticks(monkeypatch):
    """Review (Opus): the forced run's w2d_search suite builds its arms from
    its own common_env, and its settle may take 900 s, past the reaper's
    300 s."""
    import w2d_search
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    env = w2d_search.common_env("dsn")
    assert env[REAP] == "86400" and env[SAVE] == "86400"
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE")
    env = w2d_search.common_env("dsn")
    assert REAP not in env and SAVE not in env


def test_a_route_scenarios_own_autosave_wins(monkeypatch):
    import run

    class Saves(run.Scenario):
        env = {SAVE: "1"}

    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    assert run.common_env(Saves(), "dsn")[SAVE] == "1"


IDLE = "PSEUDOLIFE_SESSION_IDLE_SECONDS"


def test_the_reaper_scenario_on_a_real_base_closes_no_real_session(monkeypatch):
    """The route ``reaper`` scenario keeps its 5 s tick, which retries init
    after the held lease is released, but on a real base that tick also
    closed a genuinely idle real session: Rust's session-end dream finished
    its graph refresh before the stop, Python's dream thread was killed at
    the stop. The real arm's idle threshold is out of reach; the scenario
    seeds no session of its own, so the tick has nothing else to close."""
    import run
    monkeypatch.setenv("PL_HARNESS_BASE_DATABASE", "pl_cf_mir_seed")
    env = run.common_env(run.SCENARIOS["reaper"](), "dsn")
    assert env[REAP] == "5" and env[IDLE] == "315360000" and env[SAVE] == "86400"
    # Only this scenario: another route arm on the base keeps the default.
    assert IDLE not in run.common_env(run.SCENARIOS["tokens"](), "dsn")


def test_the_reaper_scenarios_synthetic_arm_is_unchanged(monkeypatch):
    import run
    monkeypatch.delenv("PL_HARNESS_BASE_DATABASE", raising=False)
    env = run.common_env(run.SCENARIOS["reaper"](), "dsn")
    assert env[REAP] == "5" and IDLE not in env and SAVE not in env
