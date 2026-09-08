"""Unit tests for the tau2-bench adapter's pure parts (no tau2 install, no
GPU, no daemon, no subprocess).

``evals/taubench_adapter.py`` splits cleanly in two, and only the first half
is tested here because only the first half decides anything:

  * **Scoring** — the "Learning on the Job" protocol arithmetic (per-trial
    curve, the unbiased pass^k estimator, hold rate, the baseline floor
    stratum and conversion into it, the paired cluster bootstrap). Every
    expectation below is hand-computed in the test, never re-derived by
    calling the function under test a second way.
  * **Command construction** — ``build_tau2_command`` returns argv + an env
    overlay and shells out nowhere, so the exact flags a run would use are
    assertable without tau2 installed.

``run()`` itself is driven exactly once, by
``test_a_failed_dream_between_trials_stops_the_run``, with the subprocess,
both probes and the dream replaced — everything it would otherwise start.

The paper's committed Mistral baseline (97 tasks x 4 trials on
``banking_knowledge``) is reproduced in FULL by the single grid
``_paper_baseline_grid`` builds, and
``test_paper_baseline_grid_reproduces_every_committed_number`` pins all of
it at once: per-trial ``[6, 7, 5, 7]``, pass^k
``[0.064, 0.038, 0.034, 0.031]``, an 84-task floor stratum, and — in
``test_hold_rate_is_a_numerator_denominator_pair`` — the hold pair
``(11, 18)``. (An earlier reading took the paper's pass^k row x 97 —
``[6, 4, 3, 3]`` — for the per-trial curve; the analysis code's
``per_trial`` is the source of record, and that curve is retracted.)

The adapter deliberately duplicates only ONE helper from the rest of the
bench (``probe``); the AST pin below is the same device
``tests/test_cognee_adapter.py`` uses, and it is written over the union of
the shared helper names so that copying a second one later without pinning
it fails here.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_EVALS = _REPO / "evals"
_ADAPTER = _EVALS / "taubench_adapter.py"

sys.path.insert(0, str(_EVALS))


@pytest.fixture(scope="module")
def tb():
    spec = importlib.util.spec_from_file_location("taubench_adapter_under_test",
                                                  _ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── grid helpers used by several tests ───────────────────────────────────

def _grid(rows: dict[str, str]) -> dict[str, list[bool]]:
    """``{"t1": "1001"} -> {"t1": [True, False, False, True]}``."""
    return {k: [c == "1" for c in v] for k, v in rows.items()}


def _paper_baseline_grid() -> dict[str, list[bool]]:
    """97 tasks x 4 trials reproducing EVERY committed Mistral-baseline
    number of the paper at once: per-trial ``[6, 7, 5, 7]`` (the analysis
    code's ``per_trial``), pass^k ``[0.064, 0.038, 0.034, 0.031]`` and an
    84-task floor stratum:

      * 3 tasks pass all four trials      -> 3 to every trial; pass^4 = 3/97
      * 1 task passes trials 2, 3, 4      -> C(3,2)/6 = 1/2 and C(3,3)/4 = 1/4
      * 1 task passes trials 2 and 4      -> C(2,2)/6 = 1/6
      * 8 tasks pass once (3 / 2 / 1 / 2 on trials 1..4)
      * 84 tasks never pass               -> the floor stratum

    Successes: 12 + 3 + 2 + 8 = 25 = 0.064 * 388.
    """
    g: dict[str, list[bool]] = {}
    for i in range(3):
        g[f"all{i}"] = [True, True, True, True]
    g["three0"] = [False, True, True, True]
    g["two0"] = [False, True, False, True]
    singles = {0: 3, 1: 2, 2: 1, 3: 2}
    for trial, n in singles.items():
        for i in range(n):
            row = [False, False, False, False]
            row[trial] = True
            g[f"t{trial + 1}only{i}"] = row
    for i in range(84):
        g[f"floor{i}"] = [False, False, False, False]
    return g


# ── pass^k, per-trial, hold, floor/conversion ────────────────────────────

def test_pass_k_on_a_hand_computed_three_task_grid(tb):
    """Unbiased estimator per task: C(c, k) / C(n, k), averaged over tasks.

    Tasks: a = 4/4, b = 2/4, c = 0/4.
      pass^1 = (4/4 + 2/4 + 0/4)/3            = 6/12  = 0.5
      pass^2 = (C(4,2)/6 + C(2,2)/6 + 0)/3    = (1 + 1/6)/3 = 7/18
      pass^3 = (C(4,3)/4 + 0 + 0)/3           = 1/3
      pass^4 = (C(4,4)/1 + 0 + 0)/3           = 1/3
    """
    g = _grid({"a": "1111", "b": "1010", "c": "0000"})
    assert tb.pass_k(g, 1) == pytest.approx(0.5)
    assert tb.pass_k(g, 2) == pytest.approx(7 / 18)
    assert tb.pass_k(g, 3) == pytest.approx(1 / 3)
    assert tb.pass_k(g, 4) == pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        tb.pass_k(g, 5)


def test_paper_baseline_grid_reproduces_every_committed_number(tb):
    """The estimator reproduces the paper's Mistral baseline in full from
    one grid: per-trial curve, all four pass^k values (to the paper's 3
    decimals) and the 84-task floor. If the estimator ever drifts, one of
    these rounds differently."""
    g = _paper_baseline_grid()
    assert len(g) == 97
    assert tb.per_trial(g) == [6, 7, 5, 7]
    assert tb.pass_k(g, 1) == pytest.approx(25 / 388)
    assert tb.pass_k(g, 2) == pytest.approx((3 + 0.5 + 1 / 6) / 97)
    assert tb.pass_k(g, 3) == pytest.approx((3 + 0.25) / 97)
    assert tb.pass_k(g, 4) == pytest.approx(3 / 97)
    assert [round(tb.pass_k(g, k), 3) for k in (1, 2, 3, 4)] == \
        [0.064, 0.038, 0.034, 0.031]
    assert len(tb.floor_tasks(g)) == 84


def test_hold_rate_is_a_numerator_denominator_pair(tb):
    """P(pass at t+1 | pass at t) over consecutive trial pairs.

    On the paper-shaped grid: the 3 all-pass tasks contribute 3 held pairs
    each (9/9); the three-pass task holds twice (2/2); the two-pass task
    breaks once (0/1); the single-pass tasks on trials 1..3 break once each
    (0/6) and the trial-4 ones have no successor. So (11, 18) — which is
    the hold pair the paper's analysis code reports for the Mistral
    baseline, a fifth committed number this one grid reproduces.
    """
    assert tb.hold_rate(_paper_baseline_grid()) == (11, 18)
    # A task that never passes contributes no denominator at all.
    assert tb.hold_rate(_grid({"a": "0000"})) == (0, 0)
    # [T, F, T, T]: only pairs that START from a pass count, so t0 (broken)
    # and t2 (held) — t1 starts from a failure and t3 has no successor.
    assert tb.hold_rate(_grid({"a": "1011"})) == (1, 2)


def test_floor_stratum_is_defined_by_the_baseline_and_conversion_counts_it(tb):
    baseline = _grid({"a": "1000", "b": "0000", "c": "0000", "d": "0000"})
    floor = tb.floor_tasks(baseline)
    assert floor == {"b", "c", "d"}
    learning = _grid({"a": "1111", "b": "0010", "c": "1111", "d": "0000"})
    # b and c are floor tasks the learning condition solved at least once;
    # a is not in the floor, so its successes do not count as conversion.
    assert tb.conversion(learning, floor) == 2
    assert tb.conversion(baseline, floor) == 0


# ── grid_from_results ────────────────────────────────────────────────────

def _results(sims: list[dict]) -> dict:
    return {"timestamp": "2026-09-08T00:00:00", "info": {},
            "tasks": [], "simulations": sims}


def _sim(task_id: str, trial: int, reward: float, **kw) -> dict:
    row = {"id": f"{task_id}-{trial}", "task_id": task_id, "trial": trial,
           "seed": 0, "reward_info": {"reward": reward},
           "termination_reason": "user_stop", "agent_cost": 0.1,
           "user_cost": 0.02, "messages": []}
    row.update(kw)
    return row


def test_grid_from_results_treats_missing_simulations_as_failures(tb):
    res = _results([_sim("t1", 0, 1.0), _sim("t1", 2, 1.0)])
    g = tb.grid_from_results(res, ["t1", "t2"], 4)
    assert g == {"t1": [True, False, True, False],
                 "t2": [False, False, False, False]}


def test_grid_from_results_uses_the_0999_reward_threshold(tb):
    res = _results([_sim("t1", 0, 0.999), _sim("t1", 1, 0.9989),
                    _sim("t1", 2, 1.0), _sim("t1", 3, 0.0)])
    assert tb.grid_from_results(res, ["t1"], 4)["t1"] == [True, False,
                                                          True, False]


def test_grid_from_results_ignores_tasks_outside_the_requested_set(tb):
    """A --task-ids-file is the authority on which tasks are in the run; a
    stray simulation from an earlier --save-to must not widen the grid."""
    res = _results([_sim("t1", 0, 1.0), _sim("stray", 0, 1.0)])
    assert set(tb.grid_from_results(res, ["t1"], 2)) == {"t1"}


def test_grid_from_results_offsets_trials_for_a_per_trial_invocation(tb):
    """With --distill trial each tau2 run is --num-trials 1, so every
    simulation says trial 0; the offset places it in the right column."""
    res = _results([_sim("t1", 0, 1.0)])
    g = tb.grid_from_results(res, ["t1"], 4, trial_offset=2)
    assert g["t1"] == [False, False, True, False]


def test_grid_from_results_rejects_a_trial_index_out_of_range(tb):
    """Silently dropping it would understate the condition."""
    res = _results([_sim("t1", 7, 1.0)])
    with pytest.raises(ValueError, match="trial"):
        tb.grid_from_results(res, ["t1"], 4)


def test_merge_grids_ors_the_per_trial_slices(tb):
    a = _grid({"t1": "1000", "t2": "0000"})
    b = _grid({"t1": "0000", "t2": "0100"})
    assert tb.merge_grids([a, b]) == _grid({"t1": "1000", "t2": "0100"})
    with pytest.raises(ValueError):
        tb.merge_grids([a, _grid({"t3": "0000"})])


# ── paired cluster bootstrap ─────────────────────────────────────────────

def _pass1(tb):
    return lambda g: tb.pass_k(g, 1)


def test_bootstrap_interval_contains_the_point_estimate(tb):
    g = _paper_baseline_grid()
    out = tb.bootstrap_paired({"baseline": g}, _pass1(tb), B=200, seed=0)
    lo, hi = out["baseline"]
    assert lo <= tb.pass_k(g, 1) <= hi
    assert lo < hi                       # a degenerate interval would be a bug


def test_bootstrap_is_seed_deterministic(tb):
    g = _paper_baseline_grid()
    a = tb.bootstrap_paired({"b": g}, _pass1(tb), B=200, seed=7)
    b = tb.bootstrap_paired({"b": g}, _pass1(tb), B=200, seed=7)
    c = tb.bootstrap_paired({"b": g}, _pass1(tb), B=200, seed=8)
    assert a == b
    assert a != c


def test_pairing_gives_two_identical_grids_a_zero_width_delta(tb):
    """The whole point of pairing: one index draw per replicate, applied to
    every condition. Two identical grids must then differ by exactly 0 in
    every replicate, so the delta interval is exactly (0.0, 0.0) — an
    unpaired bootstrap would produce a wide one here."""
    g = _paper_baseline_grid()
    grids = {"baseline": g, "instruction": dict(g)}
    d = tb.bootstrap_contrast(grids, _pass1(tb), "instruction", "baseline",
                              kind="delta", B=200, seed=0)
    assert (d["lo"], d["hi"]) == (0.0, 0.0)
    assert d["point"] == 0.0
    r = tb.bootstrap_contrast(grids, _pass1(tb), "instruction", "baseline",
                              kind="ratio", B=200, seed=0)
    assert (r["point"], r["lo"], r["hi"]) == (1.0, 1.0, 1.0)


def test_bootstrap_resampling_keeps_task_identity_for_floor_stats(tb):
    """``conversion`` is a stat over the bootstrap too, so a resampled grid
    must still let it decide floor membership — duplicated tasks count
    once per draw, which is what makes the statistic resampling-stable."""
    baseline = _grid({"a": "1000", "b": "0000"})
    learning = _grid({"a": "1111", "b": "1000"})
    floor = tb.floor_tasks(baseline)
    out = tb.bootstrap_paired({"learning": learning},
                              lambda g: tb.conversion(g, floor),
                              B=100, seed=0)
    lo, hi = out["learning"]
    assert 0 <= lo <= 1 and 0 <= hi <= 2


def test_ratio_vs_baseline_applies_the_parity_bar(tb):
    """The pre-registered bar: instruction/baseline pass^1 >= 2.6 and
    experience/baseline >= 1.6, with the interval excluding 1.0."""
    assert tb.PARITY_BARS == {"instruction": 2.6, "experience": 1.6}
    base = _grid({f"t{i}": "1000" for i in range(10)})          # pass^1 = 0.25
    # every task all four trials -> pass^1 = 1.0, ratio 4.0
    strong = _grid({f"t{i}": "1111" for i in range(10)})
    out = tb.ratio_vs_baseline({"baseline": base, "instruction": strong},
                               baseline="baseline", k=1, B=200, seed=0)
    got = out["instruction"]
    assert got["point"] == pytest.approx(4.0)
    assert got["bar"] == 2.6
    assert got["meets_bar"] is True
    assert got["excludes_one"] is True
    weak = _grid({f"t{i}": ("1100" if i < 5 else "1000") for i in range(10)})
    out2 = tb.ratio_vs_baseline({"baseline": base, "instruction": weak},
                                baseline="baseline", k=1, B=200, seed=0)
    assert out2["instruction"]["meets_bar"] is False


# ── the 3-vote judge helper ──────────────────────────────────────────────

def test_majority_needs_a_strict_majority(tb):
    assert tb.majority(["a", "a", "b"]) == "a"
    assert tb.majority(["a", "b", "c"]) is None      # no strict majority
    assert tb.majority(["a", "a", None]) == "a"      # Nones are dropped first
    assert tb.majority([None, None]) is None
    assert tb.majority([]) is None
    assert tb.majority([True, True, False]) is True


# ── command construction ─────────────────────────────────────────────────

_COMMON = dict(save_to="tb-x", task_ids=["t1", "t2"], num_trials=4, seed=0,
               agent_url="http://127.0.0.1:8092/v1",
               user_url="http://127.0.0.1:1234/v1",
               daemon_url="http://127.0.0.1:8795",
               data_dir="local/data/tau2", run_tag="x",
               telemetry_log="local/data/tau2/tb-x.telemetry.jsonl")


def _argv_val(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_build_tau2_command_baseline_uses_llm_agent_and_no_plugin_env(tb):
    argv, env = tb.build_tau2_command(condition="baseline", **_COMMON)
    assert _argv_val(argv, "--agent") == "llm_agent"
    assert "pl_memory" not in argv
    assert not [k for k in env if k.startswith("PL_")], (
        "the baseline condition must carry no plugin env at all — a stray "
        "PL_* var is how a 'no memory' arm quietly gets memory")
    # tau2 runs with the checkout as its cwd, so a relative data dir does
    # not resolve there (the 2026-09-08 smoke: "Data directory does not
    # exist"); the env carries the ABSOLUTE path.
    assert env["TAU2_DATA_DIR"] == str(Path("local/data/tau2").resolve())
    assert Path(env["TAU2_DATA_DIR"]).is_absolute()


def test_build_tau2_command_learning_conditions_set_the_plugin_env(tb):
    argv, env = tb.build_tau2_command(condition="instruction", rules="lessons",
                                      distill="episode",
                                      read_only=True, **_COMMON)
    assert _argv_val(argv, "--agent") == "pl_memory"
    assert env["PL_MCP_URL"] == "http://127.0.0.1:8795"
    assert env["PL_ROUTE"] == "lessons"
    assert env["PL_SUPERVISION"] == "instruction"
    assert env["PL_DREAM"] == "episode"
    assert env["PL_READ_ONLY"] == "1"
    assert env["PL_RUN_TAG"] == "x"
    assert env["PL_TAUBENCH_LOG"].endswith(".telemetry.jsonl")


def test_read_only_reaches_the_plugin_as_a_BOOLEAN_flag(tb):
    """``PL_READ_ONLY`` is parsed by the plugin as a flag (1/true/yes/on),
    so anything else — a bank name, a DSN — reads as FALSE and the frozen
    arm writes into the store it was supposed to leave alone. The frozen
    bank is selected by pointing ``--daemon-url`` at a daemon serving the
    copy; this env only says "do not write to it".

    ``tests/test_taubench_plugin.py`` pins the other half: that the plugin's
    own parser reads this exact value as True."""
    _, env = tb.build_tau2_command(condition="experience", rules="entries",
                                   read_only=True, **_COMMON)
    assert env["PL_READ_ONLY"] == "1"
    _, env = tb.build_tau2_command(condition="experience", rules="entries",
                                   **_COMMON)
    assert "PL_READ_ONLY" not in env, (
        "a writing arm must carry no read-only env at all")
    # Freezing the store and consolidating into it are contradictory orders:
    # the dream writes. Refused loudly rather than silently skipped.
    with pytest.raises(SystemExit):
        tb.main(["--condition", "experience", "--rules", "entries",
                 "--out-tag", "x", "--read-only", "--distill", "episode"])
    src = _ADAPTER.read_text(encoding="utf-8")
    assert "--read-only-store" not in src, (
        "a store NAME on this flag is exactly the bug: the plugin would "
        "parse it as False")
    assert re.search(r'"--read-only",\s*action="store_true"', src)


def test_retrieval_mode_reaches_the_plugin_and_names_the_run(tb):
    """``--retrieval nudge|inject`` (default nudge) rides to the plugin as
    PL_RETRIEVAL_MODE; inject is part of the run name so its rows never
    share a resume set with a nudge run of the same tag."""
    _, env = tb.build_tau2_command(condition="experience", rules="entries",
                                   **_COMMON)
    assert env["PL_RETRIEVAL_MODE"] == "nudge"
    _, env = tb.build_tau2_command(condition="experience", rules="entries",
                                   retrieval="inject", **_COMMON)
    assert env["PL_RETRIEVAL_MODE"] == "inject"
    _, env = tb.build_tau2_command(condition="baseline", **_COMMON)
    assert "PL_RETRIEVAL_MODE" not in env
    assert tb.run_name("experience", "t", "entries") == \
        "taubench-experience-entries-t"
    assert tb.run_name("experience", "t", "entries", retrieval="inject") == \
        "taubench-experience-entries-inject-t"
    with pytest.raises(ValueError):
        tb.build_tau2_command(condition="experience", rules="entries",
                              retrieval="teleport", **_COMMON)


def test_build_tau2_command_keeps_route_and_arm_orthogonal(tb):
    """Feedback arm and rule route are separate arms of the design: every
    learning condition names its route explicitly, the baseline refuses
    one, and the arm reaches the plugin as PL_SUPERVISION — without it the
    instruction arm would silently run as experience (no corrections)."""
    for cond in ("experience", "instruction"):
        for route in ("entries", "lessons"):
            _, env = tb.build_tau2_command(condition=cond, rules=route, **_COMMON)
            assert env["PL_ROUTE"] == route
            assert env["PL_SUPERVISION"] == cond
        with pytest.raises(ValueError):
            tb.build_tau2_command(condition=cond, **_COMMON)
    with pytest.raises(ValueError):
        tb.build_tau2_command(condition="baseline", rules="entries", **_COMMON)


def test_build_tau2_command_carries_the_protocol_flags(tb):
    argv, _ = tb.build_tau2_command(condition="experience", rules="entries",
                                    **_COMMON)
    assert argv[-len(["--task-ids", "t1", "t2"]):] == ["--task-ids", "t1", "t2"]
    for flag, value in (("--domain", "banking_knowledge"),
                        ("--retrieval-config", "bm25"),
                        ("--num-trials", "4"), ("--max-steps", "60"),
                        ("--max-concurrency", "1"), ("--save-to", "tb-x")):
        assert _argv_val(argv, flag) == value
    assert "--auto-resume" in argv
    agent_args = json.loads(_argv_val(argv, "--agent-llm-args"))
    assert agent_args["api_base"] == "http://127.0.0.1:8092/v1"
    assert agent_args["temperature"] == 0
    assert agent_args["reasoning_effort"] == "medium"
    user_args = json.loads(_argv_val(argv, "--user-llm-args"))
    assert user_args["api_base"] == "http://127.0.0.1:1234/v1"
    # The customer's thinking mode is pinned OFF the way the bench's own
    # client does it — the 2026-09-08 smoke's first episode retried three
    # times on an empty customer turn without this.
    assert user_args["extra_body"]["chat_template_kwargs"] == \
        {"enable_thinking": False}
    assert _argv_val(argv, "--agent-llm").startswith("openai/")
    assert _argv_val(argv, "--user-llm").startswith("openai/")


def test_task_ids_are_expanded_from_the_file(tb, tmp_path):
    """tau2 has no --task-ids-file, only a variadic --task-ids, so the file
    is expanded onto the command line rather than passed through."""
    p = tmp_path / "tasks.txt"
    p.write_text("# banking_knowledge subset\nt1\n\n t2 \n", encoding="utf-8")
    assert tb.load_task_ids(p) == ["t1", "t2"]
    argv, _ = tb.build_tau2_command(
        condition="baseline", **{**_COMMON, "task_ids": tb.load_task_ids(p)})
    assert argv[argv.index("--task-ids") + 1:] == ["t1", "t2"]
    # The paper's released data/task_ids.txt is ONE line of 97 ids separated
    # by spaces; a line-only reader would hand tau2 a single bogus id.
    p.write_text("t1 t2  t3\nt4 # trailing note\n", encoding="utf-8")
    assert tb.load_task_ids(p) == ["t1", "t2", "t3", "t4"]


def test_data_dir_must_carry_the_domain(tb, tmp_path):
    """tau2 resolves BOTH its domain data and its simulations output under
    TAU2_DATA_DIR (reference/tau2-bench/src/tau2/utils/utils.py), so a data
    dir that lacks the banking domain makes every run fail after the
    servers are up. The default is the pinned checkout's data dir and the
    guard names the missing path."""
    assert tb.DEFAULT_DATA_DIR.replace("\\", "/").endswith("tau2-bench/data")
    with pytest.raises(SystemExit) as exc:
        tb.check_data_dir(tmp_path)
    assert "banking_knowledge" in str(exc.value)
    (tmp_path / "tau2" / "domains" / "banking_knowledge").mkdir(parents=True)
    assert tb.check_data_dir(tmp_path) == tmp_path


@pytest.mark.parametrize("endpoint", ["agent_url", "user_url"])
@pytest.mark.parametrize("port", [":8082", ":8086"])
def test_build_tau2_command_refuses_a_production_shim_port(tb, port, endpoint):
    """:8082/:8086 are the deployed shims. A bench arm pointed at one
    measures whatever the live shim was launched with, and the failure is
    quiet because the incumbent answers /v1/models. BOTH endpoints are
    checked: every simulated user turn is model output the episode's verdict
    depends on, so a user simulator on a live shim contaminates a run exactly
    as much as an agent on one."""
    assert tb.PRODUCTION_SHIM_PORTS == (":8082", ":8086")
    bad = {**_COMMON, endpoint: f"http://127.0.0.1{port}/v1"}
    with pytest.raises(SystemExit, match="production"):
        tb.build_tau2_command(condition="baseline", **bad)
    argv, _ = tb.build_tau2_command(condition="baseline",
                                    allow_production_ports=True, **bad)
    flag = "--agent-llm-args" if endpoint == "agent_url" else "--user-llm-args"
    assert json.loads(_argv_val(argv, flag))["api_base"] == bad[endpoint]


def test_default_agent_url_is_the_eval_port_and_is_redirectable(tb):
    assert ":8092" in tb.AGENT_URL
    assert not any(p in tb.AGENT_URL for p in tb.PRODUCTION_SHIM_PORTS)


# ── trial-major scheduling ───────────────────────────────────────────────

def test_trial_plan_is_one_invocation_per_trial_when_distilling_per_trial(tb):
    plan = tb.trial_plan(num_trials=4, distill="trial", save_to="tb-x", seed=10)
    assert [p["num_trials"] for p in plan] == [1, 1, 1, 1]
    assert [p["trial_offset"] for p in plan] == [0, 1, 2, 3]
    assert [p["seed"] for p in plan] == [10, 11, 12, 13]
    assert [p["save_to"] for p in plan] == ["tb-x-t0", "tb-x-t1",
                                            "tb-x-t2", "tb-x-t3"]
    # A dream between trials, but not after the last one.
    assert [p["dream_after"] for p in plan] == [True, True, True, False]


def test_each_plan_step_gets_its_own_telemetry_log_at_an_absolute_trial(tb):
    """Under ``--distill trial`` every tau2 invocation is ``--num-trials 1``,
    so tau2 reports trial 0 four times and the plugin stamps 0 four times.
    Two consequences, both fixed here:

      * one telemetry file per step, or all four trials fold onto (task, 0)
        — memory calls summed across trials, window_ok last-write-wins, and
        a resumed run reading a stale file;
      * ``PL_TRIAL_OFFSET``, which the plugin ADDS to tau2's trial when it
        stamps the run context, so the records carry the real column.
    """
    name = "taubench-instruction-lessons-x"
    plan = tb.trial_plan(num_trials=4, distill="trial", save_to=name, seed=0)
    logs = [tb.telemetry_path("local/data/tau2", name, s["trial_offset"])
            for s in plan]
    assert len({str(p) for p in logs}) == 4, "one file per trial, not one file"
    assert str(logs[2]).endswith(f"{name}-t2.telemetry.jsonl")

    _, env = tb.build_tau2_command(
        condition="instruction", rules="lessons", trial_offset=2,
        **{**_COMMON, "num_trials": 1,
           "telemetry_log": str(logs[2])})
    assert env["PL_TRIAL_OFFSET"] == "2"
    assert env["PL_TAUBENCH_LOG"].endswith("-t2.telemetry.jsonl")

    # With the offset applied by the plugin, the per-step telemetry is keyed
    # by the ABSOLUTE trial and the row finds it there.
    tel = {("t1", 2): {"n_memory_calls": 5, "window_ok": True}}
    rows = tb.rows_from_results(_results([_sim("t1", 0, 1.0)]),
                                condition="instruction", tag="x",
                                plan=plan[2], telemetry=tel,
                                task_ids=["t1"])
    assert rows[0]["trial"] == 2
    assert rows[0]["n_memory_calls"] == 5


def test_the_baseline_carries_no_trial_offset_either(tb):
    _, env = tb.build_tau2_command(condition="baseline", trial_offset=3,
                                   **_COMMON)
    assert not [k for k in env if k.startswith("PL_")]


@pytest.mark.parametrize("distill", ["episode", "off"])
def test_trial_plan_is_a_single_invocation_otherwise(tb, distill):
    plan = tb.trial_plan(num_trials=4, distill=distill, save_to="tb-x", seed=0)
    assert len(plan) == 1
    assert plan[0]["num_trials"] == 4 and plan[0]["trial_offset"] == 0
    assert plan[0]["dream_after"] is False


# ── rows, telemetry, resume, naming ──────────────────────────────────────

def test_rows_from_results_carries_the_verdict_and_costs(tb):
    res = _results([_sim("t1", 0, 1.0, termination_reason="user_stop",
                         agent_cost=0.5, user_cost=0.1),
                    _sim("t2", 0, 0.2)])
    plan = tb.trial_plan(4, "episode", "tb-x", 0)[0]
    rows = tb.rows_from_results(res, condition="experience", tag="x",
                                plan=plan, telemetry={}, task_ids=["t1", "t2"])
    by_id = {(r["task_id"], r["trial"]): r for r in rows}
    assert by_id[("t1", 0)]["success"] is True
    assert by_id[("t1", 0)]["reward"] == 1.0
    assert by_id[("t1", 0)]["agent_cost"] == 0.5
    assert by_id[("t2", 0)]["success"] is False
    # No telemetry file -> the fields are null, which is NOT zero.
    assert by_id[("t1", 0)]["n_memory_calls"] is None
    assert by_id[("t1", 0)]["window_ok"] is None


def test_rows_from_results_folds_in_plugin_telemetry(tb, tmp_path):
    """The record shape is the plugin's (``pl_taubench.telemetry``): a
    ``kind`` of ``call`` or ``episode``, and the daemon's ``used_ids_*``
    keys carried VERBATIM on the episode record — ``used_ids_unmatched``
    and ``used_ids_served_elsewhere`` are id LISTS, which the adapter
    counts."""
    log = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"kind": "call", "task_id": "t1", "trial": 0,
                    "tool": "memory_search"}),
        json.dumps({"kind": "call", "task_id": "t1", "trial": 0,
                    "tool": "memory_lesson_search"}),
        json.dumps({"kind": "episode", "task_id": "t1", "trial": 0,
                    "window_ok": True, "used_ids_recorded": 3,
                    "used_ids_unmatched": [7],
                    "used_ids_served_elsewhere": [8, 9]}),
        "not json",                                    # tolerated, skipped
        json.dumps({"kind": "episode", "trial": 0}),   # no task_id -> skipped
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tel = tb.load_telemetry(log)
    assert tel[("t1", 0)]["n_memory_calls"] == 2
    assert tel[("t1", 0)]["used_ids_recorded"] == 3
    rows = tb.rows_from_results(_results([_sim("t1", 0, 1.0)]),
                                condition="experience", tag="x",
                                plan=tb.trial_plan(4, "episode", "tb-x", 0)[0],
                                telemetry=tel, task_ids=["t1"])
    assert rows[0]["n_memory_calls"] == 2
    assert rows[0]["used_ids_unmatched"] == 1
    assert rows[0]["served_elsewhere"] == 2
    assert rows[0]["window_ok"] is True
    assert tb.load_telemetry(tmp_path / "missing.jsonl") == {}


def test_done_set_makes_the_run_resumable_per_episode(tb, tmp_path):
    p = tmp_path / "rows.jsonl"
    p.write_text('{"task_id": "t1", "trial": 0}\n\n'
                 '{"task_id": "t1", "trial": 1}\n', encoding="utf-8")
    assert tb.done_set(tb.load_rows(p)) == {("t1", 0), ("t1", 1)}
    assert tb.load_rows(tmp_path / "nope.jsonl") == []


def test_out_file_naming_keys_learning_runs_by_route(tb):
    """Both routes run under one condition and tag; without the route in
    the name they would share a file and the resume set would skip the
    second route's episodes as already done."""
    assert tb.out_file("baseline", "run1") == \
        _EVALS / "results" / "taubench-baseline-run1.jsonl"
    assert tb.out_file("experience", "run1", "entries") == \
        _EVALS / "results" / "taubench-experience-entries-run1.jsonl"
    assert tb.out_file("experience", "run1", "lessons") != \
        tb.out_file("experience", "run1", "entries")
    with pytest.raises(ValueError):
        tb.out_file("experience", "run1")
    assert tb.smoke_tag("run1") == "smoke-run1"
    assert tb.smoke_tag("smoke-run1") == "smoke-run1"


# ── the use-window invariant gates the ratios ────────────────────────────

def _jsonl(rows) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def _rows_for(grid, condition, window_ok=True):
    out = []
    for task_id, trials in grid.items():
        for t, ok in enumerate(trials):
            out.append({"condition": condition, "task_id": task_id, "trial": t,
                        "reward": 1.0 if ok else 0.0, "success": ok,
                        "window_ok": window_ok})
    return out


def _report_grids():
    """Baseline solves t0 at trial 1 only and nothing else, so the floor is
    the other 9 tasks. pass^1(baseline) = (1/4)/10 = 0.025; the learning
    grid passes everything, pass^1 = 1.0, so the ratio is exactly 40 and
    conversion is all 9 floor tasks."""
    base = _grid({"t0": "1000", **{f"t{i}": "0000" for i in range(1, 10)}})
    strong = _grid({f"t{i}": "1111" for i in range(10)})
    return base, strong


def test_grid_from_rows_takes_a_task_set_authority(tb):
    """Same rule as ``grid_from_results``: a task with no row did NOT pass —
    it crashed, or a resume was truncated — so it is padded with failures.
    Dropping it shrinks the denominator, which INFLATES pass^k for exactly
    the runs that went worst. A row outside the set is ignored, so a stray
    row cannot widen the grid either."""
    rows = [{"task_id": "t1", "trial": 0, "success": True}]
    assert tb.grid_from_rows(rows, 2, ["t1", "t2"]) == {
        "t1": [True, False], "t2": [False, False]}
    stray = rows + [{"task_id": "zz", "trial": 0, "success": True}]
    assert set(tb.grid_from_rows(stray, 2, ["t1", "t2"])) == {"t1", "t2"}
    # No authority given: the grid is whatever the rows happen to carry.
    assert set(tb.grid_from_rows(rows, 2)) == {"t1"}


def test_report_scores_a_missing_task_as_failures_and_records_it(tb):
    """10 tasks were run, 9 wrote rows. With the task set as the authority
    that is 9/10 at pass^1; without it, 9/9 — a perfect score for a run that
    lost a task."""
    task_ids = [f"t{i}" for i in range(10)]
    rows = _rows_for(_grid({t: "1111" for t in task_ids[:9]}), "instruction")
    withheld = tb.build_summary(rows, baseline_rows=None,
                                condition="instruction", tag="x", n_trials=4,
                                B=50, seed=0, task_ids=task_ids)
    assert withheld["n_tasks"] == 10
    assert withheld["n_tasks_missing"] == 1
    assert withheld["task_set_source"] == "task_ids"
    assert withheld["per_trial"] == [9, 9, 9, 9]
    assert withheld["pass_k"]["1"] == pytest.approx(0.9)

    inflated = tb.build_summary(rows, baseline_rows=None,
                                condition="instruction", tag="x", n_trials=4,
                                B=50, seed=0)
    assert inflated["n_tasks"] == 9
    assert inflated["n_tasks_missing"] == 0
    assert inflated["task_set_source"] == "rows"
    assert inflated["pass_k"]["1"] == pytest.approx(1.0)


def test_the_task_set_defaults_to_the_union_of_both_conditions(tb):
    """Without a task-ids file the union of condition and baseline ids is
    the best authority available — and it is what makes the paired bootstrap
    possible at all: it needs identical task sets, so a task only the
    baseline ran used to raise instead of counting as a failure."""
    rows = _rows_for(_grid({"t0": "1111"}), "instruction")
    base = _rows_for(_grid({"t0": "0000", "t1": "1000"}), "baseline")
    summary = tb.build_summary(rows, baseline_rows=base,
                               condition="instruction", tag="x", n_trials=4,
                               B=50, seed=0)
    assert summary["n_tasks"] == 2
    assert summary["n_tasks_missing"] == 1
    assert summary["task_set_source"] == "rows"
    assert summary["vs_baseline"]["baseline_n_tasks"] == 2
    # instruction: t0 = 4/4, t1 = 0/4 -> 0.5. baseline: 0/4 and 1/4 -> 0.125.
    assert summary["pass_k"]["1"] == pytest.approx(0.5)
    assert summary["vs_baseline"]["ratio"]["point"] == pytest.approx(4.0)


def test_report_reads_the_task_set_from_the_task_ids_file(tb, tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(tb, "RESULTS_DIR", tmp_path)
    rows = _rows_for(_grid({"t0": "1111"}), "instruction")
    tb.out_file("instruction", "z", "lessons").write_text(_jsonl(rows),
                                                          encoding="utf-8")
    ids = tmp_path / "tasks.txt"
    ids.write_text("t0\nt1\nt2\n", encoding="utf-8")
    summary = tb.report("instruction", "z", None, n_trials=4, B=50, seed=0,
                        rules="lessons", task_ids_file=ids)
    assert summary["n_tasks"] == 3
    assert summary["n_tasks_missing"] == 2
    assert summary["pass_k"]["1"] == pytest.approx(1 / 3)


def test_report_publishes_ratios_when_the_use_window_held(tb):
    base, strong = _report_grids()
    summary = tb.build_summary(_rows_for(strong, "instruction"),
                               baseline_rows=_rows_for(base, "baseline"),
                               condition="instruction", tag="x",
                               n_trials=4, B=100, seed=0)
    assert summary["window"]["violations"] == 0
    assert summary["ratios_published"] is True
    assert summary["vs_baseline"]["floor"] == 9
    assert summary["vs_baseline"]["conversion"] == 9
    assert summary["vs_baseline"]["ratio"]["point"] == pytest.approx(40.0)
    assert summary["vs_baseline"]["ratio"]["bar"] == 2.6
    assert summary["vs_baseline"]["ratio"]["meets_bar"] is True
    assert summary["vs_baseline"]["delta"]["point"] == pytest.approx(0.975)
    assert summary["per_trial"] == [10, 10, 10, 10]
    assert summary["hold_rate"] == {"numerator": 30, "denominator": 30,
                                    "rate": 1.0}


def test_report_withholds_ratios_when_the_use_window_was_violated(tb):
    """A violated use window means a served memory was used outside the
    window it was scored in — the ratio would be measuring the leak."""
    base, strong = _report_grids()
    rows = _rows_for(strong, "instruction")
    rows[0]["window_ok"] = False
    summary = tb.build_summary(rows, baseline_rows=_rows_for(base, "baseline"),
                               condition="instruction", tag="x",
                               n_trials=4, B=100, seed=0)
    assert summary["window"]["violations"] == 1
    assert summary["ratios_published"] is False
    assert "window" in summary["ratios_withheld_because"].lower()
    assert summary["vs_baseline"].get("ratio") is None
    # The descriptive half still reports — only the comparison is withheld.
    assert summary["per_trial"] == [10, 10, 10, 10]


def test_report_treats_unknown_window_as_unknown_not_a_violation(tb):
    """No plugin telemetry (the baseline condition has none) must not read
    as a violation, or the baseline could never be compared against."""
    rows = _rows_for(_grid({"t0": "1111"}), "baseline", window_ok=None)
    summary = tb.build_summary(rows, baseline_rows=None,
                               condition="baseline", tag="x", n_trials=4,
                               B=50, seed=0)
    assert summary["window"] == {"ok": 0, "violations": 0, "unknown": 4}
    assert summary["ratios_published"] is True


# ── import hygiene and the duplicated-helper pin ─────────────────────────

_HEAVY = ("torch", "pseudolife_memory", "mcp", "tau2")


def test_module_is_import_light(tb):
    """The adapter runs from the bench venv beside a separate tau2 venv and
    must import without torch, the library, the MCP client, or tau2 itself
    — ``dream_run`` imports ``mcp`` lazily."""
    src = _ADAPTER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    top: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.append(node.module.split(".")[0])
    assert not set(top) & set(_HEAVY), f"heavy top-level import: {top}"
    assert not set(sys.modules) & {"tau2", "cognee"}


def _top_nodes(text: str) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = n
    return out


# Shared bench helpers and where they live. The adapter copies ``probe`` and
# nothing else today; the assertion is written over the whole union so that
# copying a second one later without keeping it identical fails here.
_SHARED = {
    "ladder_sweep.py": ("probe",),
    "longmemeval_bench.py": ("_chat", "_THINKING_LEVELS", "_SAMPLER_PROTECTED"),
    "beam_adapter.py": ("format_turn", "judge_response", "parse_judge_score"),
}


@pytest.mark.parametrize("origin,names", sorted(_SHARED.items()))
def test_any_duplicated_helper_is_ast_identical_to_its_origin(origin, names):
    adapter = _top_nodes(_ADAPTER.read_text(encoding="utf-8"))
    src = _top_nodes((_EVALS / origin).read_text(encoding="utf-8"))
    for name in names:
        if name not in adapter:
            continue
        assert name in src, f"{name} no longer exists in {origin}"
        assert ast.dump(adapter[name]) == ast.dump(src[name]), (
            f"taubench_adapter.{name} has drifted from {origin} — copy the "
            "origin verbatim or stop duplicating it")


def test_probe_is_the_one_helper_copied(tb):
    """Recorded so the pin above is not vacuous: if this list changes, the
    pin's coverage changed with it."""
    adapter = _top_nodes(_ADAPTER.read_text(encoding="utf-8"))
    copied = {n for names in _SHARED.values() for n in names if n in adapter}
    assert copied == {"probe"}


def test_ledger_progress_line_matches_the_default_pattern(tb):
    """evals/run_ledger.ps1 counts finished units with -ProgressPattern
    'ingested|cognified|\\(reused\\)'; the per-episode line must contain one
    of those words or the ledger sits at 0 all night looking healthy."""
    line = tb.episode_line("t1", 2, 1.0, True)
    assert "ingested" in line
    assert "t1" in line and "2" in line


def test_check_dream_result_flags_an_error_or_a_non_dict(tb):
    """The between-trials dream is the whole lessons route: if it failed,
    trial t+1 runs against whatever trial t left unconsolidated. The MCP
    client turns failures into ``{"error": ...}`` rather than raising, so
    only an explicit check sees them."""
    assert tb.check_dream_result({"content": ['{"facts": 3}']}) is None
    assert "error" in (tb.check_dream_result({"error": "daemon said no"}) or "")
    assert tb.check_dream_result(None)
    assert tb.check_dream_result("done")


def test_maintenance_session_uid_is_a_fresh_maint_handle(tb):
    """Bank maintenance runs under its own short-lived session, never the
    run's: a maintenance session must never be a candidate for ``used_ids``
    credit. Same shape the plugin's own trigger mints."""
    a, b = tb.maint_session_uid(), tb.maint_session_uid()
    assert a.startswith("maint-") and b.startswith("maint-")
    assert a != b


class _Proc:
    returncode = 0


def test_a_failed_dream_between_trials_stops_the_run(tb, tmp_path, monkeypatch,
                                                     capsys):
    """A failed consolidation is not a warning to scroll past. The next
    trial's condition IS what the previous trial distilled, so continuing
    measures an arm that learned nothing while reporting it as the arm that
    did."""
    data_dir = tmp_path / "data"
    # run() refuses a data dir without the domain (tau2 reads domains and
    # writes simulations under one root); give this one the shape.
    (data_dir / "tau2" / "domains" / tb.DOMAIN).mkdir(parents=True)
    monkeypatch.setattr(tb, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(tb, "probe", lambda url, timeout=4.0: True)
    monkeypatch.setattr(tb, "probe_daemon", lambda url: True)

    def _fake_tau2(argv, cwd=None, env=None, check=False):
        path = tb.results_path(data_dir, argv[argv.index("--save-to") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_results([_sim("t1", 0, 1.0)])),
                        encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(tb.subprocess, "run", _fake_tau2)

    seen: dict = {}

    async def _fake_dream(url, session_uid):
        seen["session"] = session_uid
        return {"error": "daemon said no"}

    monkeypatch.setattr(tb, "dream_run", _fake_dream)

    with pytest.raises(SystemExit) as exc:
        tb.run(condition="instruction", tag="x", task_ids=["t1"], num_trials=2,
               rules="lessons", distill="trial", read_only=False,
               agent_url="http://127.0.0.1:8092/v1",
               user_url="http://127.0.0.1:1234/v1",
               daemon_url="http://127.0.0.1:8795", data_dir=str(data_dir),
               tau2_venv=str(tmp_path), tau2_root=str(tmp_path), max_steps=5,
               model="bench", seed=0, allow_production_ports=False)
    assert exc.value.code not in (0, None)
    printed = capsys.readouterr().out
    assert "daemon said no" in printed, printed
    assert seen["session"].startswith("maint-"), (
        "the dream must run under a maintenance session, not the run's — "
        "the run's session is a used_ids credit candidate")


def test_probe_daemon_requires_db_ok(tb, monkeypatch):
    calls = {}

    def fake_get(url, timeout=0):
        calls["url"] = url
        return calls["payload"]

    monkeypatch.setattr(tb, "_get_json", fake_get)
    calls["payload"] = {"status": "ok", "db": "ok"}
    assert tb.probe_daemon("http://127.0.0.1:8795") is True
    assert calls["url"].endswith("/health")
    calls["payload"] = {"status": "ok", "db": "degraded"}
    assert tb.probe_daemon("http://127.0.0.1:8795") is False
    calls["payload"] = {}
    assert tb.probe_daemon("http://127.0.0.1:8795") is False


def test_bootstrap_default_is_ten_thousand_replicates_and_seed_zero(tb):
    """The pre-registered protocol: B = 10,000, fixed seed, percentile
    [2.5, 97.5]."""
    import inspect
    sig = inspect.signature(tb.bootstrap_paired)
    assert sig.parameters["B"].default == 10_000
    assert sig.parameters["seed"].default == 0
    assert tb.PERCENTILES == (2.5, 97.5)


def test_percentile_interpolates_and_survives_an_infinite_ratio(tb):
    """A replicate whose baseline resample scored 0 gives an infinite
    ratio; the interval must stay computable rather than turn into nan."""
    assert tb._percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert tb._percentile([1, 2, 3, math.inf], 100) == math.inf
    assert tb._percentile([math.inf, math.inf], 50) == math.inf


def test_summary_is_written_as_valid_json_even_with_an_infinite_ratio(
        tb, tmp_path, monkeypatch):
    """A baseline that solves nothing makes the ratio infinite, and bare
    ``Infinity`` is not valid JSON — every strict reader (jq, any other
    language) rejects it, so the artifact would be unusable by exactly the
    tools a published number gets checked with."""
    monkeypatch.setattr(tb, "RESULTS_DIR", tmp_path)
    rows = _rows_for(_grid({f"t{i}": "1111" for i in range(4)}), "instruction")
    base = _rows_for(_grid({f"t{i}": "0000" for i in range(4)}), "baseline")
    base_path = tmp_path / "baseline.jsonl"
    base_path.write_text(_jsonl(base), encoding="utf-8")
    rows_path = tb.out_file("instruction", "x", "lessons")
    rows_path.write_text(_jsonl(rows), encoding="utf-8")
    summary = tb.report("instruction", "x", base_path, n_trials=4, B=50,
                        seed=0, rules="lessons")
    assert summary["vs_baseline"]["ratio"]["point"] == math.inf
    written = rows_path.with_suffix(".summary.json").read_text(encoding="utf-8")
    assert "Infinity" not in written
    assert json.loads(written)["vs_baseline"]["ratio"]["point"] == "inf"
    assert json.loads(written)["vs_baseline"]["conversion"] == 4


def test_report_prints_the_withholding_reason(tb, tmp_path, monkeypatch,
                                              capsys):
    """Withholding has to be visible in the terminal, not only in the
    artifact — the operator reading the run's tail is who decides whether
    to publish."""
    monkeypatch.setattr(tb, "RESULTS_DIR", tmp_path)
    rows = _rows_for(_grid({"t0": "1111"}), "instruction")
    rows[0]["window_ok"] = False
    tb.out_file("instruction", "y", "entries").write_text(_jsonl(rows),
                                                          encoding="utf-8")
    tb.report("instruction", "y", None, n_trials=4, B=50, seed=0,
              rules="entries")
    assert "RATIOS WITHHELD" in capsys.readouterr().out
