import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))


def test_retention_bench_runs_and_shows_protection():
    from retention_bench import run_bench, GRID
    rows = run_bench()
    assert [r["retention_boost"] for r in rows] == GRID
    by_boost = {r["retention_boost"]: r for r in rows}
    # boost=0 must match today's eviction: reinforced entries get NO protection,
    # so their survival rate is no better than the unreinforced baseline.
    z = by_boost[0.0]
    assert z["reinforced_survival_rate"] <= z["unreinforced_survival_rate"] + 1e-9
    # a high boost must measurably protect reinforced entries vs boost=0.
    assert by_boost[max(GRID)]["reinforced_survival_rate"] > z["reinforced_survival_rate"]
