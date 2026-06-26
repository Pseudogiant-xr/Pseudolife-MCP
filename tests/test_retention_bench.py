import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))


def test_retention_bench_runs_and_boost_is_monotonic():
    from retention_bench import run_bench, GRID
    rows = run_bench()
    assert [r["retention_boost"] for r in rows] == GRID
    # Honest workload: reinforcement is COUPLED to access_count (reinforcing is
    # accessing), so reinforced entries already carry baseline protection at
    # boost=0 — the old "no protection at boost=0" invariant no longer holds by
    # design. What survives an honest model: raising retention_boost can only
    # raise a reinforced entry's eviction score against the fixed unreinforced
    # ones, so reinforced survival is NON-DECREASING across the grid. Whether the
    # boost helps *enough to matter* is the bench's output, not a hard invariant.
    rates = [r["reinforced_survival_rate"] for r in rows]
    assert rates == sorted(rates)
