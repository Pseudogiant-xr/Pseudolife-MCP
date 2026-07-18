"""Memory-loop capture metrics — read-only report over the live bank.

Measures the three beats of the memory loop against the real Postgres bank:
CAPTURE (do sessions store anything substantive), REFLECT (do sessions log
outcomes — explicitly or via the auto-outcome stage), and the failure share
(failures are the highest-value signals and were historically under-logged).

Baseline (2026-07-18, N=89 root episodes since 2026-06-27, BEFORE the
auto-outcome stage deployed):

    sessions with >=1 substantive store   88/89  (99%)
    sessions with zero outcome signals    31/89  (35%)
    outcomes by type                      112 success / 25 failure / 23 correction
    failure+correction share              30%
    median stores per session             1      (p90 3.2)

Success criteria for the auto-outcome stage (re-measure 2-3 weeks after
2026-07-18): outcome coverage of substantive sessions >= 90%, and
failure+correction share of NEW signals above 30%.

Usage (read-only; safe against the live bank):

    python evals/capture_metrics.py            # table
    python evals/capture_metrics.py --json     # machine-readable
    python evals/capture_metrics.py --since 2026-07-18   # window start (UTC date)

DSN: ``PSEUDOLIFE_METRICS_DSN`` env var, defaulting to the stock local stack
(``postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

DSN = os.environ.get(
    "PSEUDOLIFE_METRICS_DSN",
    "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
)

# One row per root session episode: stores (substantive = non-status/log),
# explicit and inferred outcome counts across the episode subtree.
_PER_SESSION_SQL = """
WITH RECURSIVE subtree AS (
    SELECT id, id AS root_id FROM episodes
    WHERE parent_id IS NULL AND session_key IS NOT NULL
      AND started_at >= %(since)s
    UNION ALL
    SELECT e.id, s.root_id FROM episodes e
    JOIN subtree s ON e.parent_id = s.id
)
SELECT
    s.root_id,
    COUNT(DISTINCT en.id) FILTER (
        WHERE en.source NOT IN ('status', 'log')) AS substantive_stores,
    COUNT(DISTINCT en.id) AS total_stores,
    COUNT(DISTINCT o.id) FILTER (
        WHERE COALESCE(o.origin, '') <> 'inferred') AS explicit_outcomes,
    COUNT(DISTINCT o.id) FILTER (
        WHERE o.origin = 'inferred') AS inferred_outcomes
FROM subtree s
LEFT JOIN entries en ON en.episode_id = s.id
LEFT JOIN outcome_signals o ON o.episode_id = s.id
GROUP BY s.root_id
"""

_OUTCOME_MIX_SQL = """
SELECT COALESCE(o.origin, '') = 'inferred' AS inferred, o.outcome, COUNT(*)
FROM outcome_signals o WHERE o.created_at >= %(since)s
GROUP BY 1, 2 ORDER BY 1, 2
"""


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = q * (len(vs) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (idx - lo)


def collect(since_epoch: float) -> dict:
    import psycopg

    with psycopg.connect(DSN, connect_timeout=10) as conn:
        rows = conn.execute(
            _PER_SESSION_SQL, {"since": since_epoch}).fetchall()
        mix = conn.execute(
            _OUTCOME_MIX_SQL, {"since": since_epoch}).fetchall()

    sessions = [
        {"root_id": r[0], "substantive": r[1], "total": r[2],
         "explicit": r[3], "inferred": r[4]} for r in rows]
    substantive = [s for s in sessions if s["substantive"] > 0]
    with_any_outcome = [s for s in substantive
                        if s["explicit"] + s["inferred"] > 0]
    store_counts = [float(s["substantive"]) for s in sessions]
    outcome_mix = {
        ("inferred" if inferred else "explicit", outcome): int(n)
        for inferred, outcome, n in mix}
    total_outcomes = sum(outcome_mix.values()) or 1
    neg = sum(n for (_, o), n in outcome_mix.items()
              if o in ("failure", "correction"))
    return {
        "sessions": len(sessions),
        "substantive_sessions": len(substantive),
        "capture_coverage": round(
            len(substantive) / len(sessions), 3) if sessions else None,
        "outcome_coverage_of_substantive": round(
            len(with_any_outcome) / len(substantive), 3)
        if substantive else None,
        "median_stores": _percentile(store_counts, 0.5),
        "p90_stores": round(_percentile(store_counts, 0.9), 1),
        "outcome_mix": {f"{k[0]}.{k[1]}": v
                        for k, v in sorted(outcome_mix.items())},
        "failure_correction_share": round(neg / total_outcomes, 3),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--since", default="2026-06-27",
                    help="UTC date (YYYY-MM-DD) the window starts at")
    args = ap.parse_args(argv)
    since_epoch = datetime.strptime(args.since, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp()
    stats = collect(since_epoch)
    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    print(f"\nmemory-loop capture metrics (since {args.since})")
    print(f"{'sessions (root episodes)':<38}{stats['sessions']:>8}")
    print(f"{'  with >=1 substantive store':<38}"
          f"{stats['substantive_sessions']:>8}"
          f"   ({stats['capture_coverage']:.0%})"
          if stats['capture_coverage'] is not None else "")
    oc = stats["outcome_coverage_of_substantive"]
    print(f"{'outcome coverage (substantive)':<38}"
          f"{oc:>8.0%}" if oc is not None else
          f"{'outcome coverage (substantive)':<38}{'n/a':>8}")
    print(f"{'median / p90 stores per session':<38}"
          f"{stats['median_stores']:>5.1f} / {stats['p90_stores']}")
    print(f"{'failure+correction share':<38}"
          f"{stats['failure_correction_share']:>8.0%}")
    print("outcome mix:")
    for k, v in stats["outcome_mix"].items():
        print(f"    {k:<28}{v:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
