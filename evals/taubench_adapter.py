"""tau2-bench adapter — the "Learning on the Job" protocol against
Pseudolife memory.

Replicates the protocol of Tablan et al., "Learning on the Job"
(arXiv 2607.22157) on tau2-bench 1.0.1, domain ``banking_knowledge``:
97 tasks x 4 trials, scheduled TRIAL-MAJOR (every task once at trial 1,
then every task at trial 2, ...) so that what a condition learned in trial
t is available to every task at trial t+1. One episode is one tau2
simulation; the verdict is tau2's own ``reward_info.reward >= 0.999``.
Nothing here re-judges anything — tau2 scores the episode, this adapter
only schedules, records and aggregates.

Conditions (``--condition``):

  * ``baseline``     — tau2's stock ``llm_agent``. No memory of any kind,
                       and deliberately NO ``PL_*`` env at all.
  * ``experience``   — the ``pl_memory`` agent; the reflection sees only the
                       one-bit verdict (the paper's experience arm).
  * ``instruction``  — the ``pl_memory`` agent; on a failed episode the
                       reflection also sees the verified action sequence and
                       the action diff (the paper's corrections arm).

The feedback arm is ``PL_SUPERVISION``; WHERE rules live is the orthogonal
``--rules`` route (``entries``: the reflection writes constraint entries
credited by ``used_ids``; ``lessons``: ``memory_outcome`` only, the daemon's
rule-mode synthesis distils). Both routes run under both arms, and the
results file carries the route in its name.

``--read-only`` freezes the store for a transfer arm: the plugin reads and
never writes (no reflection, no episode rows). It is a FLAG, not a store
selector — the plugin parses ``PL_READ_ONLY`` as a boolean, so a bank name
passed here would read as False and the "frozen" arm would write. WHICH bank
is served comes from ``--daemon-url`` pointing at a daemon that serves the
frozen copy.

Metrics (all in the pure half of this module, all unit-tested):

  * per-trial success curve — number of tasks passing at trial t.
  * pass^k for k = 1..4, unbiased per task: ``C(c, k) / C(n, k)`` with c
    successes in n trials, averaged over tasks.
  * hold rate — P(pass at t+1 | pass at t) over consecutive trial pairs,
    reported as a (numerator, denominator) PAIR, never a bare rate: at
    these success counts the denominator is small enough that the ratio
    alone is not readable.
  * floor stratum — the tasks the BASELINE condition never solves;
    conversion is how many of those a learning condition solves at least
    once.
  * cluster bootstrap — B = 10,000 replicates, fixed seed, resampling the
    97 TASK indices with replacement and applying the SAME index draw to
    every condition in a replicate (pairing), percentile interval
    [2.5, 97.5]. The pairing is what makes a delta interval meaningful:
    two identical conditions come out at exactly (0, 0).

The pre-registered parity bar (``PARITY_BARS``) reads instruction/baseline
pass^1 >= 1.6 — the paper's Sonnet 5 replication, the instrument run here
— with the interval excluding 1.0; experience carries no bar (see the
constant's comment for the 2026-09-09 amendment from the Mistral 2.6x).

Note on the paper's committed baseline numbers (Mistral): the per-trial
curve is [6, 7, 5, 7] (25 successes over 388 episodes, so pass^1 = 25/388 =
0.064), pass^k = [0.064, 0.038, 0.034, 0.031], and the floor stratum is 84
never-solved tasks. Under this estimator all four are reproduced together
by one grid — 3 tasks pass all four trials, 1 passes three, 1 passes two,
8 pass once, 84 never — which ``tests/test_taubench_adapter.py`` pins. (A
first reading of the paper's HTML mistook the pass^k row × 97 — 6, 4, 3,
3 — for the per-trial curve; the analysis code's ``per_trial`` is the
source of record.)

Operator preconditions (this adapter starts NOTHING; it probes and exits
with the command to run):

  1. The bench Qwen server, reproducible config —
     ``. evals/qwen_server.ps1; Start-Qwen``. Never ``-Fast``: it is not
     bit-reproducible, and every tau2 user turn is model output the
     episode's verdict depends on.
  2. The emulating Claude shim on the EVAL port (:8092 by default), never
     the deployed shim's :8082 — see ``PRODUCTION_SHIM_PORTS``.
  3. The bench daemon: ``pseudolife-mcp serve`` against a FRESH bench
     database on port 8795 (a run that inherits a populated bank is not
     measuring learning on the job).

Commands:

    # smoke — 3 tasks x 2 trials, validates plumbing only (one route)
    python evals/taubench_adapter.py --condition instruction --rules entries \\
        --out-tag lotj --smoke --task-ids-file local/data/tau2/tasks.txt

    # full baseline, then a learning condition per route, then the report
    python evals/taubench_adapter.py --condition baseline --out-tag lotj \\
        --task-ids-file local/data/tau2/tasks.txt --num-trials 4
    python evals/taubench_adapter.py --condition instruction --rules lessons \\
        --out-tag lotj --task-ids-file local/data/tau2/tasks.txt \\
        --num-trials 4 --distill trial
    python evals/taubench_adapter.py --condition instruction --rules entries \\
        --out-tag lotj --task-ids-file local/data/tau2/tasks.txt --num-trials 4
    python evals/taubench_adapter.py --condition instruction --rules lessons \\
        --out-tag lotj --report \\
        --task-ids-file local/data/tau2/tasks.txt \\
        --baseline-rows evals/results/taubench-baseline-lotj.jsonl

``--report`` takes the same ``--task-ids-file`` the run did: it is the task-set
authority, so a task that wrote no rows scores as failures instead of leaving
the denominator (which would inflate pass^k). Without it the report falls back
to the union of the ids the rows carry and says so in ``task_set_source``.

Writes ``evals/results/taubench-<condition>[-<route>]-<tag>.jsonl`` — append-only,
one row per (condition, task, trial), resumable through the ``done`` set —
plus a ``.summary.json`` from ``--report``. Per house convention a canonical
rows file is never rewritten on a rerun: give the rerun its own ``--out-tag``
and promote deliberately.

Unattended runs: each finished episode prints one line containing the word
``ingested``, which is what ``evals/run_ledger.ps1`` counts under its
default ``-ProgressPattern 'ingested|cognified|\\(reused\\)'``. No custom
pattern is needed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import urllib.request
import uuid
from functools import partial
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# The verdict threshold is tau2's, not ours: a task is passed when its
# reward is 1.0, compared with a float tolerance rather than ==.
REWARD_THRESHOLD = 0.999

# Percentile interval for every bootstrap in this module.
PERCENTILES = (2.5, 97.5)

# Pre-registered parity bar on pass^1 relative to baseline (paper, Table 3).
# Amended 2026-09-09, before any learning-arm episode existed: the 2.6x /
# 1.6x first written here are the paper's Mistral Large ratios at a 0.064
# baseline. This adapter's agent is Sonnet 5 at medium effort — the paper's
# replication model — and its committed grids (paper_grids.tsv, scored with
# the estimators below) give baseline 0.247, instruction 0.397: 1.60x
# [1.31, 2.05], delta +0.149, 28 of 57 floor tasks. At our baseline (0.24 at
# its first 21 episodes) 2.6x would mean 0.65 absolute — a ceiling. No bar
# on experience: the paper ran no Sonnet experience arm, so its ratio is
# reported, never "met".
PARITY_BARS = {"instruction": 1.6, "experience": None}

CONDITIONS = ("baseline", "experience", "instruction")
# Feedback arm (condition) and rule route are ORTHOGONAL arms of the design
# (spec 2026-09-08): both routes run under both learning conditions, so a
# learning condition never picks a route by itself — --rules is required
# there and refused on the baseline, which carries no memory at all.
RULES = ("entries", "lessons")
DISTILL = ("episode", "trial", "off")
# How recalled memory reaches the agent (PL_RETRIEVAL_MODE): "nudge" = the
# first turn carries a reminder and the model searches itself (the paper's
# reported configuration); "inject" = the harness searches on the first turn
# and folds the results in (the paper's second mode — two nudge smokes on
# 2026-09-08 produced one search in seven episodes under tool-call
# emulation); "off" = tools present, nothing prompts their use.
RETRIEVAL = ("nudge", "inject", "off")

DOMAIN = "banking_knowledge"
RETRIEVAL_CONFIG = "bm25"

# :8082 is the deployed Claude shim and :8086 the deployed Codex shim
# (ops/install-shim-autostart.ps1, ops/install.ps1). A bench arm pointed at
# one measures whatever model and system prompt the LIVE shim was launched
# with, and the failure is quiet because the incumbent answers /v1/models.
# tests/test_bench_production_port_guard.py pins that no default here lands
# on one; build_tau2_command refuses an explicit one too.
PRODUCTION_SHIM_PORTS = (":8082", ":8086")

# The emulating shim runs on its own eval port, redirectable like every
# other bench endpoint (the PSEUDOLIFE_BENCH_*_URL convention).
AGENT_URL = os.environ.get("PSEUDOLIFE_BENCH_TAUBENCH_AGENT_URL",
                           "http://127.0.0.1:8092/v1")
# The simulated user runs on the bench Qwen server.
USER_URL = os.environ.get("PSEUDOLIFE_BENCH_QWEN_URL",
                          "http://127.0.0.1:1234/v1")

DEFAULT_DAEMON_URL = "http://127.0.0.1:8795"
DEFAULT_TAU2_VENV = ".venv-taubench"
DEFAULT_TAU2_ROOT = "reference/tau2-bench"
# tau2 reads its DOMAINS and writes its simulations under one TAU2_DATA_DIR,
# so the default is the pinned checkout's data dir (gitignored under
# reference/); check_data_dir() refuses a root without the banking domain.
DEFAULT_DATA_DIR = f"{DEFAULT_TAU2_ROOT}/data"
DEFAULT_MODEL = "bench"
DEFAULT_MAX_STEPS = 60
DEFAULT_NUM_TRIALS = 4

# Smoke: enough to exercise scheduling, the dream between trials, and the
# results/telemetry read — nothing about the numbers it produces is a result.
SMOKE_TASKS = 3
SMOKE_TRIALS = 2


# --- duplicated from ladder_sweep.py. VERBATIM: pinned AST-identical by
# tests/test_taubench_adapter.py, which also asserts this is the ONLY
# duplicated helper, so a second copy cannot arrive unpinned.

def probe(base_url: str, timeout: float = 4.0) -> bool:
    """Reachability check — GET <base_url>/models (llama.cpp serves it)."""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


# ── pure scoring ─────────────────────────────────────────────────────────
#
# A "grid" is ``{task_id: [bool per trial]}``. Under the bootstrap the keys
# become ``(task_id, draw_position)`` tuples so a task drawn twice stays two
# distinct entries while still resolving to its original id — which is what
# lets floor/conversion statistics be bootstrapped at all.


def _task_id(key):
    """The original task id behind a (possibly resampled) grid key."""
    return key[0] if isinstance(key, tuple) else key


def _width(grid: dict) -> int:
    widths = {len(v) for v in grid.values()}
    if len(widths) > 1:
        raise ValueError(f"ragged grid: trial counts {sorted(widths)}")
    return widths.pop() if widths else 0


def grid_from_results(results: dict, task_ids: list[str], n_trials: int,
                      trial_offset: int = 0) -> dict[str, list[bool]]:
    """tau2's ``results.json`` -> a grid over exactly ``task_ids``.

    A missing simulation is a FAILURE, not a gap: a task tau2 never ran (a
    crash, a truncated resume) did not pass, and treating it as absent
    would silently shrink the denominator. ``task_ids`` is the authority on
    the task set, so a stray simulation from an earlier ``--save-to`` is
    ignored rather than widening the grid.

    This is the reference implementation of the rule ``grid_from_rows``
    applies to the adapter's own JSONL: both take the task set as their
    authority and pad what is missing. This one reads tau2's file directly
    and is what a caller checking a run against its ``--save-to`` uses;
    ``--report`` reads the rows.

    ``trial_offset`` places a per-trial invocation's simulations in the
    right column: with ``--distill trial`` each tau2 run is
    ``--num-trials 1`` and every simulation reports trial 0. A trial index
    that lands outside the grid is an error, never a silent drop — dropping
    it would understate the condition. Where a (task, trial) has more than
    one simulation the last one in the file wins.
    """
    grid = {tid: [False] * n_trials for tid in task_ids}
    wanted = set(task_ids)
    for sim in results.get("simulations") or []:
        tid = sim.get("task_id")
        if tid not in wanted:
            continue
        trial = int(sim.get("trial") or 0) + trial_offset
        if not 0 <= trial < n_trials:
            raise ValueError(
                f"simulation {sim.get('id')!r} has trial index {trial} "
                f"outside [0, {n_trials}) — wrong --num-trials or a stale "
                "--save-to directory")
        reward = float((sim.get("reward_info") or {}).get("reward") or 0.0)
        grid[tid][trial] = reward >= REWARD_THRESHOLD
    return grid


def grid_from_rows(rows: list[dict], n_trials: int,
                   task_ids: list[str] | None = None
                   ) -> dict[str, list[bool]]:
    """The same grid, rebuilt from this adapter's own JSONL rows (what
    ``--report`` reads, so a report never needs the tau2 data dir).

    ``task_ids`` is the task-set AUTHORITY, exactly as in
    ``grid_from_results``: a task with no row did not pass — it crashed, or
    a resume was truncated — so it is padded with failures. Dropping it
    shrinks the denominator, which INFLATES pass^k for precisely the runs
    that went worst. A row outside the set is ignored so a stray row cannot
    widen the grid. Passing None keeps the older behaviour (the grid is
    whatever the rows carry), which ``build_summary`` uses only to build the
    union of the conditions it was given.
    """
    wanted = set(task_ids) if task_ids is not None else None
    grid: dict[str, list[bool]] = {tid: [False] * n_trials
                                   for tid in (task_ids or [])}
    for row in rows:
        tid = row["task_id"]
        if wanted is not None and tid not in wanted:
            continue
        trial = int(row["trial"])
        if not 0 <= trial < n_trials:
            raise ValueError(f"row for {tid!r} has trial {trial} outside "
                             f"[0, {n_trials})")
        grid.setdefault(tid, [False] * n_trials)
        if "success" in row:
            ok = bool(row["success"])
        else:
            ok = float(row.get("reward") or 0.0) >= REWARD_THRESHOLD
        grid[tid][trial] = ok
    return grid


def merge_grids(grids: list[dict]) -> dict:
    """Elementwise OR of same-shaped grids — how the per-trial invocations
    of a ``--distill trial`` run are stitched back into one grid. Safe
    because every slice defaults to False outside its own trial."""
    if not grids:
        return {}
    keys = list(grids[0])
    width = _width(grids[0])
    out = {k: list(grids[0][k]) for k in keys}
    for g in grids[1:]:
        if set(g) != set(keys) or _width(g) != width:
            raise ValueError("merge_grids needs identically shaped grids")
        for k in keys:
            out[k] = [a or b for a, b in zip(out[k], g[k])]
    return out


def per_trial(grid: dict) -> list[int]:
    """The success curve: how many tasks passed at each trial."""
    n = _width(grid)
    return [sum(1 for trials in grid.values() if trials[t]) for t in range(n)]


def pass_k(grid: dict, k: int) -> float:
    """Unbiased pass^k, averaged over tasks: with c successes in n trials,
    a task's probability that all of k trials drawn without replacement
    passed is ``C(c, k) / C(n, k)``."""
    n = _width(grid)
    if k < 1 or k > n:
        raise ValueError(f"k={k} outside 1..{n}")
    if not grid:
        return 0.0
    denom = math.comb(n, k)
    total = 0.0
    for trials in grid.values():
        c = sum(1 for t in trials if t)
        total += math.comb(c, k) / denom if c >= k else 0.0
    return total / len(grid)


def hold_rate(grid: dict) -> tuple[int, int]:
    """P(pass at t+1 | pass at t) as a (numerator, denominator) pair.

    Reported as a pair on purpose: at these success counts the denominator
    is often single-digit, and a bare "0.69" hides that it is 9 of 13.
    """
    num = den = 0
    for trials in grid.values():
        for t in range(len(trials) - 1):
            if trials[t]:
                den += 1
                num += 1 if trials[t + 1] else 0
    return num, den


def floor_tasks(baseline_grid: dict) -> set:
    """The floor stratum: tasks the BASELINE never solves in any trial."""
    return {_task_id(k) for k, trials in baseline_grid.items()
            if not any(trials)}


def conversion(grid: dict, floor: set) -> int:
    """How many floor tasks this condition solves at least once.

    Counts grid ENTRIES, not distinct ids: on a real grid each task appears
    once so the two agree, and under the bootstrap a task drawn twice
    counts twice — which is what makes this statistic resamplable.
    """
    return sum(1 for k, trials in grid.items()
               if _task_id(k) in floor and any(trials))


def majority(votes: list):
    """Majority value over a small vote list (a 3-vote judge panel).

    Modeled on ``evals/judge_ladder.py``'s ``majority`` — Nones dropped
    first, a STRICT majority required — but not a copy of it: that one
    carries per-vote confidences and abstains with a domain-specific
    ``("leave", 0.0)``. Here no majority is ``None``, and the caller
    decides what an abstention means.
    """
    votes = [v for v in votes if v is not None]
    if not votes:
        return None
    counts: dict = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    top, n = max(counts.items(), key=lambda kv: kv[1])
    return top if n * 2 > len(votes) else None


# ── paired cluster bootstrap ─────────────────────────────────────────────

def _percentile(values, q: float) -> float:
    """Linear-interpolated empirical percentile.

    Infinity-safe: a replicate whose baseline resample scored 0 yields an
    infinite ratio, and interpolating between two infinities would give
    nan, so an exact tie short-circuits.
    """
    xs = sorted(values)
    if not xs:
        raise ValueError("no replicates")
    pos = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi or xs[lo] == xs[hi]:
        return xs[int(lo)]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _draws(n: int, B: int, seed: int):
    """B index draws of size n, with replacement, deterministic in ``seed``.

    A generator seeded fresh on every call, so every function in this
    module that passes the same (n, B, seed) sees the SAME draws — that is
    the pairing, and it is why a delta between two identical conditions
    comes out at exactly zero.
    """
    rnd = random.Random(seed)
    for _ in range(B):
        yield [rnd.randrange(n) for _ in range(n)]


def _aligned_items(grids: dict[str, dict]) -> dict[str, list]:
    """Grids as ordered (key, trials) lists over one common task order —
    the precondition for applying one index draw to all of them."""
    names = list(grids)
    if not names:
        raise ValueError("no conditions")
    ref = list(grids[names[0]])
    for nm in names[1:]:
        if set(grids[nm]) != set(ref):
            raise ValueError(
                f"paired bootstrap needs identical task sets; {nm!r} differs "
                f"from {names[0]!r} by "
                f"{sorted(set(grids[nm]) ^ set(ref))[:5]}")
    return {nm: [(t, grids[nm][t]) for t in ref] for nm in names}


def _resample(items: list, idx: list[int]) -> dict:
    """One bootstrap replicate of a grid. Keys become (task_id, position)
    so a task drawn twice stays two entries and still resolves to its id."""
    return {(items[i][0], j): items[i][1] for j, i in enumerate(idx)}


def bootstrap_paired(grids: dict[str, dict], stat, B: int = 10_000,
                     seed: int = 0) -> dict[str, tuple[float, float]]:
    """Percentile interval per condition from a PAIRED cluster bootstrap.

    Resamples the task indices with replacement and applies the same draw
    to every condition in a replicate. Returns ``{name: (lo, hi)}``.
    """
    items = _aligned_items(grids)
    n = len(next(iter(items.values())))
    reps: dict[str, list[float]] = {nm: [] for nm in items}
    for idx in _draws(n, B, seed):
        for nm, its in items.items():
            reps[nm].append(stat(_resample(its, idx)))
    return {nm: (_percentile(v, PERCENTILES[0]),
                 _percentile(v, PERCENTILES[1])) for nm, v in reps.items()}


def _contrast(x: float, y: float, kind: str) -> tuple[float, bool]:
    """One contrast value and whether it was degenerate.

    A ratio with a zero denominator is ``inf`` when the numerator is
    positive; 0/0 is treated as parity (1.0) rather than nan so the
    interval stays computable, and both cases are counted in
    ``degenerate`` so a reader can see how much of an interval rests on
    that convention.
    """
    if kind == "delta":
        return x - y, False
    if kind != "ratio":
        raise ValueError(f"kind={kind!r} — expected 'delta' or 'ratio'")
    if y == 0:
        return (math.inf if x > 0 else 1.0), True
    return x / y, False


def bootstrap_contrast(grids: dict[str, dict], stat, a: str, b: str, *,
                       kind: str = "delta", B: int = 10_000,
                       seed: int = 0) -> dict:
    """Paired contrast ``a`` vs ``b`` (delta or ratio) with its interval.

    The same index draw hits both conditions in every replicate, so two
    identical grids give an interval of exactly (0.0, 0.0) for a delta and
    (1.0, 1.0) for a ratio.
    """
    items = _aligned_items({a: grids[a], b: grids[b]})
    n = len(items[a])
    point, _ = _contrast(stat(grids[a]), stat(grids[b]), kind)
    reps: list[float] = []
    degenerate = 0
    for idx in _draws(n, B, seed):
        va = stat(_resample(items[a], idx))
        vb = stat(_resample(items[b], idx))
        value, deg = _contrast(va, vb, kind)
        reps.append(value)
        degenerate += 1 if deg else 0
    return {"kind": kind, "a": a, "b": b, "point": point,
            "lo": _percentile(reps, PERCENTILES[0]),
            "hi": _percentile(reps, PERCENTILES[1]),
            "B": B, "seed": seed, "degenerate_replicates": degenerate}


def ratio_vs_baseline(grids: dict[str, dict], baseline: str, k: int = 1,
                      B: int = 10_000, seed: int = 0) -> dict[str, dict]:
    """pass^k ratio of each condition against ``baseline``, judged against
    the pre-registered parity bar. ``excludes_one`` is the other half of
    the bar: a ratio above 2.6 whose interval straddles 1.0 has not
    cleared it."""
    stat = partial(pass_k, k=k)
    out: dict[str, dict] = {}
    for name in grids:
        if name == baseline:
            continue
        c = bootstrap_contrast(grids, stat, name, baseline, kind="ratio",
                               B=B, seed=seed)
        bar = PARITY_BARS.get(name)
        c.update({"k": k, "bar": bar,
                  "meets_bar": bar is not None and c["point"] >= bar,
                  "excludes_one": c["lo"] > 1.0 or c["hi"] < 1.0})
        out[name] = c
    return out


# ── run orchestration (thin; every path configurable) ────────────────────

def tau2_exe(tau2_venv) -> str:
    """The tau2 console script inside its own venv — tau2's dependency tree
    stays out of the bench venv, exactly like the Cognee adapter's."""
    venv = Path(tau2_venv)
    return str(venv / ("Scripts/tau2.exe" if os.name == "nt" else "bin/tau2"))


def load_task_ids(path) -> list[str]:
    """Task ids separated by any whitespace — one per line, or the paper's
    released ``data/task_ids.txt`` shape (one line, 97 ids, spaces);
    ``#`` comments ignored.

    tau2 has no --task-ids-file flag, only a variadic --task-ids, so the
    file is expanded onto the command line here rather than being passed
    through.
    """
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        out.extend(line.split())
    return out


def check_data_dir(data_dir) -> Path:
    """tau2 resolves BOTH its domain data and its simulations output under
    ``TAU2_DATA_DIR`` (``src/tau2/utils/utils.py``), so a data dir without
    the banking domain fails every run only after the servers are up.
    Exits naming the missing path."""
    root = Path(data_dir).resolve()
    domain = root / "tau2" / "domains" / DOMAIN
    if not domain.is_dir():
        raise SystemExit(
            f"TAU2_DATA_DIR {root} carries no {DOMAIN} domain at {domain} — "
            "point --data-dir at the pinned checkout's data dir "
            f"({DEFAULT_DATA_DIR}); tau2 reads its domains and writes its "
            "simulations under the same root")
    return root


def _reject_production_ports(urls: dict[str, str], allow: bool) -> None:
    if allow:
        return
    for label, url in urls.items():
        for port in PRODUCTION_SHIM_PORTS:
            if port in (url or ""):
                sys.exit(
                    f"--{label}-url {url} is on a production shim port "
                    f"({port}) — that would benchmark whatever the deployed "
                    "shim was launched with, and the probe would still pass "
                    "because the incumbent answers /v1/models. Start the "
                    "bench shim on the eval port, or pass "
                    "--allow-production-ports if you really mean it.")


def build_tau2_command(*, condition: str, save_to: str, task_ids: list[str],
                       num_trials: int, seed: int | None,
                       agent_url: str, user_url: str, daemon_url: str,
                       data_dir: str, run_tag: str, telemetry_log: str,
                       rules: str | None = None, distill: str = "episode",
                       read_only: bool = False, trial_offset: int = 0,
                       retrieval: str = "nudge",
                       model: str = DEFAULT_MODEL,
                       max_steps: int = DEFAULT_MAX_STEPS,
                       tau2_venv=DEFAULT_TAU2_VENV,
                       allow_production_ports: bool = False,
                       ) -> tuple[list[str], dict[str, str]]:
    """One tau2 invocation as (argv, env overlay) — pure, so the exact
    flags a run would use are assertable without tau2 installed.

    The env overlay is what the ``pl_memory`` plugin reads; the baseline
    carries none of it, because a stray ``PL_*`` var is how a "no memory"
    arm quietly gets memory. ``PL_DREAM`` passes the distill mode through
    verbatim, and only ``episode`` makes the plugin dream for itself —
    under ``trial`` this adapter drives the dream between invocations.
    """
    if condition not in CONDITIONS:
        raise ValueError(f"condition={condition!r} not in {CONDITIONS}")
    if distill not in DISTILL:
        raise ValueError(f"distill={distill!r} not in {DISTILL}")
    if condition == "baseline" and rules:
        raise ValueError("the baseline carries no memory: --rules does not apply")
    if condition != "baseline" and rules not in RULES:
        raise ValueError(f"a learning condition needs --rules in {RULES}; "
                         f"got {rules!r}")
    if retrieval not in RETRIEVAL:
        raise ValueError(f"retrieval={retrieval!r} not in {RETRIEVAL}")
    _reject_production_ports({"agent": agent_url, "user": user_url},
                             allow_production_ports)

    agent_args = {"api_base": agent_url, "api_key": "sk-local-bench",
                  "temperature": 0, "reasoning_effort": "medium"}
    # The customer runs on the bench Qwen server, whose chat template thinks
    # by default and then returns an EMPTY content field — tau2 rejects the
    # turn ("UserMessage must have either content or tool_calls", the
    # 2026-09-08 smoke) and retries three times. The bench's own client pins
    # thinking off on every call (longmemeval_bench._chat); litellm forwards
    # the same request field through ``extra_body``.
    user_args = {"api_base": user_url, "api_key": "sk-local-bench",
                 "temperature": 0,
                 "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    argv = [
        tau2_exe(tau2_venv), "run",
        "--domain", DOMAIN,
        "--retrieval-config", RETRIEVAL_CONFIG,
        "--agent", "llm_agent" if condition == "baseline" else "pl_memory",
        "--agent-llm", f"openai/{model}",
        "--agent-llm-args", json.dumps(agent_args),
        "--user-llm", f"openai/{model}",
        "--user-llm-args", json.dumps(user_args),
        "--num-trials", str(num_trials),
        "--max-steps", str(max_steps),
        "--max-concurrency", "1",
        "--auto-resume",
        "--save-to", save_to,
    ]
    if seed is not None:
        argv += ["--seed", str(seed)]
    argv += ["--task-ids", *task_ids]

    # Absolute on purpose: tau2 is run with its checkout as cwd, where a
    # relative data dir does not exist (it reads its domains from here).
    env = {"TAU2_DATA_DIR": str(Path(data_dir).resolve())}
    if condition != "baseline":
        env.update({
            "PL_MCP_URL": daemon_url,
            "PL_ROUTE": rules,
            # The feedback arm: the plugin's reflection delivers the verified
            # sequence + diff only under "instruction".
            "PL_SUPERVISION": condition,
            "PL_RETRIEVAL_MODE": retrieval,
            "PL_DREAM": distill,
            "PL_RUN_TAG": run_tag,
            "PL_TAUBENCH_LOG": str(telemetry_log),
            # This invocation's column. Under --distill trial each tau2 run
            # is --num-trials 1, so tau2 reports trial 0 every time; the
            # plugin ADDS this when it stamps the run context, and without it
            # every record in the run says trial 0.
            "PL_TRIAL_OFFSET": str(trial_offset),
        })
        if read_only:
            # A FLAG, never a store name. The plugin parses PL_READ_ONLY as a
            # boolean (1/true/yes/on), so a bank name or a DSN here reads as
            # False and the frozen arm WRITES into the store it was meant to
            # leave alone. Which bank is served read-only is decided by
            # --daemon-url, not by this value.
            env["PL_READ_ONLY"] = "1"
    return argv, env


def trial_plan(num_trials: int, distill: str, save_to: str,
               seed: int = 0) -> list[dict]:
    """Trial-major scheduling as a list of tau2 invocations.

    With ``--distill trial`` the distillation has to happen BETWEEN trials,
    which tau2 cannot express inside one run — so the adapter runs tau2
    once per trial (``--num-trials 1``, its own seed and ``--save-to``) and
    drives ``memory_dream`` in the gaps. Otherwise one invocation covers
    all trials and the plugin dreams per episode (or not at all).
    """
    if distill == "trial":
        return [{"save_to": f"{save_to}-t{t}", "num_trials": 1,
                 "seed": seed + t, "trial_offset": t,
                 "dream_after": t < num_trials - 1}
                for t in range(num_trials)]
    return [{"save_to": save_to, "num_trials": num_trials, "seed": seed,
             "trial_offset": 0, "dream_after": False}]


def results_path(data_dir, save_to: str) -> Path:
    return Path(data_dir) / "simulations" / save_to / "results.json"


def telemetry_path(data_dir, name: str, trial_offset: int) -> Path:
    """One telemetry log per plan step, keyed by the step's trial column.

    Not one file per run: the plugin stamps tau2's own trial index, which is
    0 in every ``--distill trial`` invocation, so a shared file folds all
    four trials onto ``(task, 0)`` — memory calls summed across trials,
    ``window_ok`` last-write-wins, and a resumed run reading a stale file it
    cannot tell apart from this trial's.
    """
    return Path(data_dir) / f"{name}-t{trial_offset}.telemetry.jsonl"


def run_name(condition: str, tag: str, rules: str | None = None,
             retrieval: str | None = None) -> str:
    """The run's identity: condition, route (learning conditions only), a
    non-default retrieval mode, and tag. Route and mode are part of the name
    because they run under the same condition and tag — sharing one file
    would let the resume set skip the second arm's episodes as already
    done."""
    if condition == "baseline":
        return f"taubench-{condition}-{tag}"
    if rules not in RULES:
        raise ValueError(f"a learning condition's run name needs a route "
                         f"in {RULES}; got {rules!r}")
    mode = "" if retrieval in (None, "nudge") else f"-{retrieval}"
    return f"taubench-{condition}-{rules}{mode}-{tag}"


def out_file(condition: str, tag: str, rules: str | None = None,
             retrieval: str | None = None) -> Path:
    return RESULTS_DIR / f"{run_name(condition, tag, rules, retrieval)}.jsonl"


def smoke_tag(tag: str) -> str:
    return tag if tag.startswith("smoke-") else f"smoke-{tag}"


def load_rows(path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def done_set(rows: list[dict]) -> set[tuple[str, int]]:
    """(task_id, trial) pairs already recorded — the resume unit is one
    episode, so an interrupted run re-reads tau2's results and appends only
    what is new."""
    return {(r["task_id"], int(r["trial"])) for r in rows
            if "task_id" in r and "trial" in r}


def load_telemetry(path) -> dict[tuple[str, int], dict]:
    """Fold the ``pl_memory`` plugin's episode telemetry into per-(task,
    trial) records. The plugin writes it; this adapter only reads it.

    Contract (``pl_taubench.telemetry``) — one JSON object per line, each
    carrying ``task_id`` and ``trial`` from the plugin's run context::

        {"kind": "call", "task_id": ..., "trial": ..., "tool": ...}
        {"kind": "episode", "task_id": ..., "trial": ..., "window_ok": bool,
         "used_ids_recorded": int, "used_ids_unmatched": [ids],
         "used_ids_served_elsewhere": [ids], ...}

    The episode record carries the daemon's ``used_ids_*`` keys verbatim
    (the two miss classes are id lists); this reader counts them into
    ``used_ids_unmatched`` / ``served_elsewhere``. ``event`` is accepted as
    a synonym for ``kind`` and ``memory_call`` for ``call``. Lines that are
    not JSON objects, or that lack task_id/trial, are skipped: the file is
    written by a live agent loop and a torn last line is normal. A missing
    file yields ``{}``, which leaves every telemetry field on the row null
    — deliberately distinguishable from zero.
    """
    path = Path(path)
    out: dict[tuple[str, int], dict] = {}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or "task_id" not in rec \
                or "trial" not in rec:
            continue
        key = (rec["task_id"], int(rec["trial"]))
        slot = out.setdefault(key, {"n_memory_calls": 0})
        kind = rec.get("kind", rec.get("event"))
        if kind in ("call", "memory_call"):
            slot["n_memory_calls"] += 1
        elif kind == "episode":
            if "window_ok" in rec:
                slot["window_ok"] = rec["window_ok"]
            if "used_ids_recorded" in rec:
                slot["used_ids_recorded"] = _count(rec["used_ids_recorded"])
            if "used_ids_unmatched" in rec:
                slot["used_ids_unmatched"] = _count(rec["used_ids_unmatched"])
            for field in ("used_ids_served_elsewhere", "served_elsewhere"):
                if field in rec:
                    slot["served_elsewhere"] = _count(rec[field])
    return out


def _count(value) -> int | None:
    """An id list counts as its length; an int stands; anything else is
    unknown (None), never silently zero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


TELEMETRY_FIELDS = ("n_memory_calls", "used_ids_recorded",
                    "used_ids_unmatched", "served_elsewhere", "window_ok")


def rows_from_results(results: dict, *, condition: str, tag: str, plan: dict,
                      telemetry: dict, task_ids: list[str],
                      extra: dict | None = None) -> list[dict]:
    """One row per simulation in this invocation's results.json.

    Telemetry is looked up by the ABSOLUTE trial: the plugin adds
    ``PL_TRIAL_OFFSET`` to the trial index the harness gives it, so its
    records already carry the real column. The invocation's own index is
    tried second, which is what a plugin build predating that offset writes
    — and since each step reads only its own telemetry file, that fallback
    can only find this step's records. Absent telemetry leaves the fields
    null rather than zero — "the plugin logged nothing" and "the plugin
    logged no memory calls" are different findings.
    """
    wanted = set(task_ids)
    rows = []
    for sim in results.get("simulations") or []:
        tid = sim.get("task_id")
        if tid not in wanted:
            continue
        sim_trial = int(sim.get("trial") or 0)
        trial = sim_trial + plan["trial_offset"]
        reward = float((sim.get("reward_info") or {}).get("reward") or 0.0)
        tel = telemetry.get((tid, trial)) or telemetry.get((tid, sim_trial)) \
            or {}
        row = {
            "condition": condition, "tag": tag, "task_id": tid,
            "trial": trial, "sim_id": sim.get("id"),
            "seed": sim.get("seed", plan.get("seed")),
            "save_to": plan["save_to"],
            "reward": reward, "success": reward >= REWARD_THRESHOLD,
            "termination_reason": sim.get("termination_reason"),
            "agent_cost": sim.get("agent_cost"),
            "user_cost": sim.get("user_cost"),
        }
        row.update({f: tel.get(f) for f in TELEMETRY_FIELDS})
        row.update(extra or {})
        rows.append(row)
    return rows


def episode_line(task_id: str, trial: int, reward: float,
                 success: bool) -> str:
    """One line per finished episode. The word "ingested" is deliberate:
    ``evals/run_ledger.ps1`` counts finished units with its default
    ``-ProgressPattern 'ingested|cognified|\\(reused\\)'``, and a line
    matching none of them leaves the ledger's progress column at 0 all
    night while looking healthy."""
    verdict = "PASS" if success else "fail"
    return (f"  task {task_id} trial {trial}: reward {reward:.3f} "
            f"{verdict} — episode ingested")


# ── the daemon ───────────────────────────────────────────────────────────

def _get_json(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def probe_daemon(url: str) -> bool:
    """The bench daemon must be up AND have its database — a daemon that
    answers /health with a degraded db would serve empty memory to every
    episode and the run would look like a very fast baseline."""
    try:
        payload = _get_json(f"{url.rstrip('/')}/health", timeout=10.0)
    except Exception:  # noqa: BLE001
        return False
    return bool(payload) and payload.get("db") == "ok"


def maint_session_uid() -> str:
    """A fresh maintenance-session handle for a dream.

    Bank maintenance is not part of any episode: it runs under its own
    short-lived session so its writes are never attributed to a graded run
    and its session is never a candidate for ``used_ids`` credit. Same
    ``maint-`` shape the plugin's own trigger mints.
    """
    return f"maint-{uuid.uuid4().hex}"


# Read timeout for the between-trial dream, in seconds. ``memory_dream
# run`` is one synchronous tool call that drains EVERY pending signal of
# the trial: under rule mode that is one extractor call per signal, and
# the emulating shim answered a single-signal dream in 7-12 s in the
# 2026-09-08 smoke (lotj4), so a 97-task trial is 12-20 minutes in one
# call, plus one retry per MUST INCLUDE miss. The MCP client's default read
# timeout is 300 s: the adapter would raise a quarter of the way in and die
# mid-arm while the daemon kept dreaming. Two hours covers a full trial at
# 5x the measured per-signal cost.
DREAM_READ_TIMEOUT_S = 7200.0


def _mcp_timeout(create_client, connect_s: float, read_s: float):
    """A ``Timeout`` of the httpx flavour the installed ``mcp`` client
    builds its ``AsyncClient`` from — ``httpx2`` since the client moved to
    it, ``httpx`` before; a ``Timeout`` from the other package is rejected by
    the client, so it is taken from the helper's own namespace first."""
    g = getattr(create_client, "__globals__", {})
    mod = g.get("httpx2") or g.get("httpx")
    if mod is None:
        try:
            import httpx2 as mod  # type: ignore[no-redef]
        except ImportError:
            import httpx as mod  # type: ignore[no-redef]
    return mod.Timeout(connect_s, read=read_s)


async def dream_run(url: str, session_uid: str) -> dict:
    """Drive one consolidation pass on the bench daemon between trials.

    ``mcp`` is imported lazily: this module must stay importable from the
    bench venv (and from the test suite) without the MCP client installed.

    A tool-level failure comes back as ``error`` rather than raising, so the
    caller can report the daemon's own words; see ``check_dream_result``.
    """
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import (create_mcp_http_client,
                                            streamable_http_client)
    timeout = _mcp_timeout(create_mcp_http_client, 30.0, DREAM_READ_TIMEOUT_S)
    async with create_mcp_http_client(
            headers={"X-PL-Session": session_uid}, timeout=timeout) as http:
        async with streamable_http_client(url.rstrip("/") + "/mcp",
                                          http_client=http) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool("memory_dream",
                                              {"action": "run"})
                texts = [getattr(c, "text", str(c))
                         for c in (res.content or [])]
                out = {"content": texts}
                if getattr(res, "isError", False):
                    out["error"] = "; ".join(texts) or "memory_dream failed"
                return out


def check_dream_result(result) -> str | None:
    """Why this dream must stop the run, or None if it succeeded.

    The consolidation between trials IS the lessons route: trial t+1 runs
    against what trial t distilled, so a dream that failed turns the rest of
    the run into an arm that learned nothing while still being reported as
    the arm that did. Failures arrive as data (``{"error": ...}``), never as
    an exception, so nothing notices them without this check.
    """
    if not isinstance(result, dict):
        return f"memory_dream returned {type(result).__name__}, not a result"
    if result.get("error"):
        return f"memory_dream reported an error: {result['error']}"
    # The daemon's single-flight guard answers ``{"skipped":
    # "dream_in_progress"}`` — no error key — when another cycle holds it.
    # Nothing was consolidated, so it is a failed dream for this purpose.
    for text in result.get("content") or []:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("skipped"):
            return (f"memory_dream skipped the pass ({payload['skipped']}): "
                    "nothing was consolidated")
    return None


# ── report ───────────────────────────────────────────────────────────────

def window_tally(rows: list[dict]) -> dict[str, int]:
    """The use-window invariant, counted. ``window_ok`` absent or None is
    UNKNOWN, not a violation — the baseline condition runs without the
    plugin and logs none, and reading that as a violation would make the
    baseline uncomparable."""
    tally = {"ok": 0, "violations": 0, "unknown": 0}
    for row in rows:
        value = row.get("window_ok")
        if value is None:
            tally["unknown"] += 1
        elif value:
            tally["ok"] += 1
        else:
            tally["violations"] += 1
    return tally


def task_ids_from_rows(*row_sets) -> list[str]:
    """The task ids present across some row sets, first-seen order.

    The fallback authority when no ``--task-ids-file`` was given. Weaker than
    the file — a task no condition ever recorded is invisible to it — but it
    is what makes the two grids share a task set, which the paired bootstrap
    requires.
    """
    out: list[str] = []
    seen: set[str] = set()
    for rows in row_sets:
        for row in rows or []:
            tid = row.get("task_id")
            if tid is not None and tid not in seen:
                seen.add(tid)
                out.append(tid)
    return out


def build_summary(rows: list[dict], *, baseline_rows: list[dict] | None,
                  condition: str, tag: str, n_trials: int = DEFAULT_NUM_TRIALS,
                  B: int = 10_000, seed: int = 0,
                  task_ids: list[str] | None = None) -> dict:
    """The full report for one condition, plus its comparison to a baseline.

    ``task_ids`` is the task set the run was supposed to cover (the
    ``--task-ids-file``). Every id in it that wrote no row is scored as
    failures and counted in ``n_tasks_missing``: a task the run lost is not
    a task the run gets to leave out of its denominator. Without it the set
    falls back to the union of the ids the two conditions did record, which
    ``task_set_source`` reports.

    The comparison half is GATED on the use-window invariant: if any
    episode reports ``window_ok: false``, a served memory was used outside
    the window it was scored in, and the ratio would be measuring the leak
    rather than the learning. The descriptive half (curve, pass^k, hold,
    conversion) still reports — it is the ratios that are withheld, with
    the reason recorded in the artifact and printed.
    """
    source = "task_ids" if task_ids is not None else "rows"
    if task_ids is None:
        task_ids = task_ids_from_rows(rows, baseline_rows or [])
    grid = grid_from_rows(rows, n_trials, task_ids)
    recorded = {r.get("task_id") for r in rows}
    window = window_tally(rows)
    summary: dict = {
        "condition": condition, "tag": tag, "n_tasks": len(grid),
        "n_tasks_missing": sum(1 for t in task_ids if t not in recorded),
        "task_set_source": source,
        "n_trials": n_trials, "n_rows": len(rows),
        "per_trial": per_trial(grid),
        "pass_k": {str(k): pass_k(grid, k)
                   for k in range(1, min(n_trials, _width(grid) or 1) + 1)},
        "window": window,
        "bootstrap": {"B": B, "seed": seed,
                      "percentiles": list(PERCENTILES)},
        "ratios_published": window["violations"] == 0,
        "ratios_withheld_because": None,
        "vs_baseline": None,
    }
    num, den = hold_rate(grid)
    summary["hold_rate"] = {"numerator": num, "denominator": den,
                            "rate": (num / den) if den else None}
    summary["bootstrap"]["pass_1"] = list(
        bootstrap_paired({condition: grid}, partial(pass_k, k=1),
                         B=B, seed=seed)[condition])
    if not summary["ratios_published"]:
        summary["ratios_withheld_because"] = (
            f"{window['violations']} of {len(rows)} episodes report "
            "window_ok=false — a served memory was used outside the window "
            "it was scored in, so a ratio against baseline would measure "
            "the leak. Fix the plugin's use window and rerun; the "
            "descriptive metrics above stand.")

    if baseline_rows:
        base_grid = grid_from_rows(baseline_rows, n_trials, task_ids)
        floor = floor_tasks(base_grid)
        vs: dict = {
            "baseline_n_tasks": len(base_grid),
            "baseline_per_trial": per_trial(base_grid),
            "baseline_pass_1": pass_k(base_grid, 1),
            "floor": len(floor),
            "conversion": conversion(grid, floor),
            "ratio": None, "delta": None,
        }
        if summary["ratios_published"]:
            grids = {condition: grid, "baseline": base_grid}
            stat = partial(pass_k, k=1)
            vs["delta"] = bootstrap_contrast(grids, stat, condition,
                                             "baseline", kind="delta",
                                             B=B, seed=seed)
            ratio = bootstrap_contrast(grids, stat, condition, "baseline",
                                       kind="ratio", B=B, seed=seed)
            bar = PARITY_BARS.get(condition)
            ratio.update({"k": 1, "bar": bar,
                          "meets_bar": bar is not None
                          and ratio["point"] >= bar,
                          "excludes_one": ratio["lo"] > 1.0
                          or ratio["hi"] < 1.0})
            vs["ratio"] = ratio
        summary["vs_baseline"] = vs
    return summary


def _json_safe(value):
    """Make a summary JSON-VALID before it is written.

    A replicate whose baseline resample scored 0 gives an infinite ratio,
    and ``json.dumps`` renders that as bare ``Infinity`` — which Python
    reads back but every strict JSON reader (jq, any other language)
    rejects, so the artifact would be unusable by exactly the tools a
    published number gets checked with. Non-finite floats become the
    strings "inf" / "-inf" / "nan", which are still legible in the
    artifact and still say what happened.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if math.isnan(value) else ("inf" if value > 0
                                                else "-inf")
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def report(condition: str, tag: str, baseline_rows_path=None,
           n_trials: int = DEFAULT_NUM_TRIALS, B: int = 10_000,
           seed: int = 0, rules: str | None = None,
           task_ids_file=None, retrieval: str | None = None) -> dict:
    """Write the summary artifact for one condition.

    Pass the run's ``--task-ids-file`` here too: it is what makes a task that
    wrote no rows count as failures instead of quietly leaving the
    denominator, and the summary records how many that was.
    """
    path = out_file(condition, tag, rules, retrieval)
    rows = load_rows(path)
    if not rows:
        raise SystemExit(f"no rows to report at {path}")
    baseline_rows = (load_rows(baseline_rows_path) if baseline_rows_path
                     else None)
    task_ids = load_task_ids(task_ids_file) if task_ids_file else None
    summary = build_summary(rows, baseline_rows=baseline_rows,
                            condition=condition, tag=tag, n_trials=n_trials,
                            B=B, seed=seed, task_ids=task_ids)
    if summary["n_tasks_missing"]:
        print(f"WARNING: {summary['n_tasks_missing']} of "
              f"{summary['n_tasks']} tasks wrote no rows and are scored as "
              "failures", flush=True)
    if not summary["ratios_published"]:
        print(f"RATIOS WITHHELD: {summary['ratios_withheld_because']}",
              flush=True)
    out = path.with_suffix(".summary.json")
    payload = json.dumps(_json_safe(summary), indent=2, allow_nan=False)
    out.write_text(payload, encoding="utf-8")
    print(payload)
    return summary


# ── run ──────────────────────────────────────────────────────────────────

def run(*, condition: str, tag: str, task_ids: list[str], num_trials: int,
        rules: str | None, distill: str, read_only: bool,
        agent_url: str, user_url: str, daemon_url: str, data_dir: str,
        tau2_venv: str, tau2_root: str, max_steps: int, model: str,
        seed: int, allow_production_ports: bool,
        retrieval: str = "nudge") -> None:
    """Drive tau2 per the trial plan and append one row per episode.

    Starts nothing: the Qwen server, the emulating shim and the bench
    daemon are operator preconditions (see the module docstring), and a
    missing one exits with the command to run rather than launching it.
    """
    # Resolved once here: tau2 and the plugin both run with the checkout as
    # cwd, so every path handed to them (data dir, telemetry log) is absolute.
    data_dir = str(check_data_dir(data_dir))
    if not probe(user_url):
        sys.exit(f"no user-simulator server at {user_url} — start it first "
                 "(evals/qwen_server.ps1 Start-Qwen)")
    if not probe(agent_url):
        sys.exit(f"no agent server at {agent_url} — start the emulating "
                 "shim on the eval port first")
    if condition != "baseline" and not probe_daemon(daemon_url):
        sys.exit(f"bench daemon at {daemon_url} is not healthy (needs "
                 "db: ok) — start it against a FRESH bench database "
                 "(pseudolife-mcp serve) before running a learning arm")

    name = run_name(condition, tag, rules, retrieval)
    out_path = out_file(condition, tag, rules, retrieval)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = done_set(load_rows(out_path))
    print(f"tau2 {DOMAIN} / {condition}: {len(task_ids)} tasks x "
          f"{num_trials} trials ({len(done)} episode-rows already done)",
          flush=True)

    plan = trial_plan(num_trials, distill, name, seed)
    extra = {"rules": rules, "distill": distill, "read_only": read_only}
    for step in plan:
        telemetry_log = telemetry_path(data_dir, name, step["trial_offset"])
        argv, env_overlay = build_tau2_command(
            condition=condition, save_to=step["save_to"], task_ids=task_ids,
            num_trials=step["num_trials"], seed=step["seed"],
            agent_url=agent_url, user_url=user_url, daemon_url=daemon_url,
            data_dir=data_dir, run_tag=tag, telemetry_log=str(telemetry_log),
            rules=rules, distill=distill, read_only=read_only,
            retrieval=retrieval,
            trial_offset=step["trial_offset"],
            model=model, max_steps=max_steps, tau2_venv=tau2_venv,
            allow_production_ports=allow_production_ports)
        print(f"launching: {' '.join(argv)}", flush=True)
        proc = subprocess.run(argv, cwd=tau2_root,
                              env={**os.environ, **env_overlay}, check=False)
        if proc.returncode != 0:
            print(f"tau2 exited {proc.returncode} for {step['save_to']} — "
                  "recording whatever it wrote, then stopping", flush=True)

        res_path = results_path(data_dir, step["save_to"])
        if not res_path.exists():
            sys.exit(f"tau2 wrote no results at {res_path} — nothing to "
                     "record; check the launch above")
        results = json.loads(res_path.read_text(encoding="utf-8"))
        telemetry = load_telemetry(telemetry_log)
        rows = rows_from_results(results, condition=condition, tag=tag,
                                 plan=step, telemetry=telemetry,
                                 task_ids=task_ids, extra=extra)
        with out_path.open("a", encoding="utf-8") as f:
            for row in rows:
                if (row["task_id"], row["trial"]) in done:
                    continue
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                done.add((row["task_id"], row["trial"]))
                print(episode_line(row["task_id"], row["trial"],
                                   row["reward"], row["success"]), flush=True)
        if proc.returncode != 0:
            sys.exit(proc.returncode)
        if step["dream_after"]:
            import asyncio
            print(f"dreaming between trials (after {step['save_to']})",
                  flush=True)
            result = asyncio.run(dream_run(daemon_url, maint_session_uid()))
            print(f"dream: {json.dumps(result, default=str)}", flush=True)
            problem = check_dream_result(result)
            if problem:
                sys.exit(f"{problem} — stopping: trial "
                         f"{step['trial_offset'] + 1} would run against an "
                         "unconsolidated store and be reported as a learning "
                         "arm anyway. The rows written so far stand.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--condition", choices=CONDITIONS, required=True)
    ap.add_argument("--out-tag", required=True)
    ap.add_argument("--rules", choices=RULES, default=None,
                    help="rule route for a learning condition (required "
                         "there, refused on the baseline): 'entries' = the "
                         "reflection writes constraint entries credited by "
                         "used_ids; 'lessons' = memory_outcome only, the "
                         "dream distils")
    ap.add_argument("--retrieval", choices=RETRIEVAL, default="nudge",
                    help="how recalled memory reaches the agent: 'nudge' = "
                         "the first turn asks the model to search (paper's "
                         "reported mode); 'inject' = the harness searches on "
                         "the first turn and folds the results in (paper's "
                         "second mode); 'off' = tools only")
    ap.add_argument("--distill", choices=DISTILL, default="episode",
                    help="'episode': the plugin consolidates per episode; "
                         "'trial': this adapter runs tau2 once per trial and "
                         "dreams in between; 'off': no consolidation")
    ap.add_argument("--read-only", action="store_true",
                    help="serve memory read-only: no writes, no reflection, "
                         "no episode rows. WHICH bank that is comes from "
                         "--daemon-url pointing at a daemon serving the "
                         "frozen copy; this flag only freezes it")
    ap.add_argument("--num-trials", type=int, default=DEFAULT_NUM_TRIALS)
    ap.add_argument("--task-ids-file", default=None,
                    help="one task id per line; expanded onto tau2's "
                         "variadic --task-ids (it has no file flag). Pass it "
                         "to --report too: it is the task-set authority, so "
                         "a task that wrote no rows scores as failures "
                         "rather than shrinking the denominator")
    ap.add_argument("--tau2-venv", default=DEFAULT_TAU2_VENV)
    ap.add_argument("--tau2-root", default=DEFAULT_TAU2_ROOT)
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="TAU2_DATA_DIR")
    ap.add_argument("--daemon-url", default=DEFAULT_DAEMON_URL)
    ap.add_argument("--agent-url", default=AGENT_URL)
    ap.add_argument("--user-url", default=USER_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--limit-tasks", type=int, default=None)
    ap.add_argument("--limit-trials", type=int, default=None)
    ap.add_argument("--allow-production-ports", action="store_true",
                    help=f"permit an endpoint on {PRODUCTION_SHIM_PORTS} — "
                         "only with a deliberate reason")
    ap.add_argument("--smoke", action="store_true",
                    help=f"{SMOKE_TASKS} tasks x {SMOKE_TRIALS} trials, tag "
                         "prefixed 'smoke-' — plumbing validation only")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--baseline-rows", default=None,
                    help="the baseline condition's JSONL, for the floor "
                         "stratum, conversion and the ratios")
    ap.add_argument("--bootstrap-B", type=int, default=10_000)
    ap.add_argument("--bootstrap-seed", type=int, default=0)
    args = ap.parse_args(argv)
    if args.read_only and args.distill != "off":
        ap.error("--read-only freezes the store, and consolidation writes to "
                 "it — pass --distill off, or drop --read-only")
    if args.condition == "baseline" and args.rules:
        ap.error("--rules does not apply to the baseline (no memory)")
    if args.condition != "baseline" and not args.rules:
        ap.error(f"--rules {{{','.join(RULES)}}} is required for a learning "
                 "condition: the feedback arm and the rule route are "
                 "separate arms of the design")

    tag = smoke_tag(args.out_tag) if args.smoke else args.out_tag
    if args.report:
        report(args.condition, tag, args.baseline_rows,
               n_trials=args.num_trials, B=args.bootstrap_B,
               seed=args.bootstrap_seed, rules=args.rules,
               retrieval=args.retrieval,
               task_ids_file=args.task_ids_file)
        return 0

    if not args.task_ids_file:
        raise SystemExit("--task-ids-file is required for a run (the paper's "
                         "97 banking_knowledge task ids)")
    task_ids = load_task_ids(args.task_ids_file)
    num_trials = args.num_trials
    limit_tasks, limit_trials = args.limit_tasks, args.limit_trials
    if args.smoke:
        limit_tasks = limit_tasks or SMOKE_TASKS
        limit_trials = limit_trials or SMOKE_TRIALS
    if limit_tasks:
        task_ids = task_ids[:limit_tasks]
    if limit_trials:
        num_trials = min(num_trials, limit_trials)

    run(condition=args.condition, tag=tag, task_ids=task_ids,
        num_trials=num_trials, rules=args.rules, distill=args.distill,
        retrieval=args.retrieval,
        read_only=args.read_only, agent_url=args.agent_url,
        user_url=args.user_url, daemon_url=args.daemon_url,
        data_dir=args.data_dir, tau2_venv=args.tau2_venv,
        tau2_root=args.tau2_root, max_steps=args.max_steps, model=args.model,
        seed=args.seed,
        allow_production_ports=args.allow_production_ports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
