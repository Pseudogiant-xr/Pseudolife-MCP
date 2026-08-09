"""Consolidation-quarantine gate 3: live-bank journal replay (offline audit).

Preregistration: docs/superpowers/specs/2026-08-09-consolidation-quarantine-design.md

Replays the retained dream-run journals (schema v27) against the low-trust
rule READ-ONLY and reports how many of the recorded claims *would* have
parked under the paranoid configuration (``trusted_sources`` empty) — the
production friction estimate that decides whether default-on is ever
proposed. Nothing is written.

The rule evaluated per journaled scalar row mirrors the shipped routing
(2026-08-09 review corrections):

* only rows whose write took canonical effect (``inserted`` /
  ``superseded``) can represent ADDED friction — ``confirmed`` rows would
  confirm under the rule too (the confirm-first ordering), and
  ``contested`` rows parked anyway; both are excluded and counted;
* a row with NO ``src_entry_id`` follows the unbacked-claim fallback
  (origin defaults to agent) → counted in ``would_park`` and separately
  as ``no_backing``;
* a row whose entry was evicted (``src_entry_id`` set, join empty) is
  ``unresolvable`` — the audit's honest blind spot, not a guess.

    PYTHONPATH=. python evals/quarantine_replay.py --tag qreplay-<date> \
        [--dsn postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pseudolife_memory.service import _origin_from_source  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PREREG = "docs/superpowers/specs/2026-08-09-consolidation-quarantine-design.md"
DEFAULT_DSN = ("postgresql://pseudolife:pseudolife@127.0.0.1:5433/"
               "pseudolife_memory")


def replay(rows: list[dict], trusted: set[str]) -> dict:
    """Pure classification of journaled scalar rows (unit-testable)."""
    out = {"scalar_rows": 0, "would_park": 0, "no_backing": 0,
           "unresolvable": 0, "excluded_confirmed": 0,
           "excluded_contested": 0, "by_source": {}}
    for r in rows:
        if r.get("kind") != "scalar":
            continue
        out["scalar_rows"] += 1
        action = r.get("action")
        if action == "confirmed":
            out["excluded_confirmed"] += 1
            continue
        if action == "contested":
            out["excluded_contested"] += 1
            continue
        if r.get("src_entry_id") is None:
            # Unbacked claim: the shipped fallback quarantines on the
            # claim's own origin, which defaults to agent.
            out["no_backing"] += 1
            out["would_park"] += 1
            continue
        src = r.get("entry_source")
        if src is None:
            out["unresolvable"] += 1
            continue
        if _origin_from_source(src) == "agent" and src not in trusted:
            out["would_park"] += 1
            out["by_source"][src] = out["by_source"].get(src, 0) + 1
    out["would_park_rate"] = round(
        out["would_park"] / out["scalar_rows"], 3) if out["scalar_rows"] \
        else 0.0
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    import psycopg

    with psycopg.connect(args.dsn, connect_timeout=10) as conn:
        run_rows = conn.execute(
            "SELECT id, started_at, status FROM dream_runs "
            "ORDER BY started_at DESC LIMIT %s", (args.runs,)).fetchall()
        run_ids = [r[0] for r in run_rows]
        rows: list[dict] = []
        if run_ids:
            cur = conn.execute(
                "SELECT s.run_id, s.kind, s.action, s.src_entry_id, "
                "e.source "
                "FROM dream_run_slots s "
                "LEFT JOIN entries e ON e.id = s.src_entry_id "
                "WHERE s.run_id = ANY(%s)", (run_ids,))
            rows = [{"run_id": rid, "kind": kind, "action": action,
                     "src_entry_id": sid, "entry_source": src}
                    for rid, kind, action, sid, src in cur.fetchall()]

    result = replay(rows, trusted=set())
    payload = {
        "preregistration": PREREG,
        "tag": args.tag, "dsn_host": args.dsn.rsplit("@", 1)[-1],
        "runs_examined": len(run_ids),
        "journal_rows": len(rows),
        **result,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out = args.out or RESULTS_DIR / f"quarantine-replay-{args.tag}.json"
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(json.dumps({k: payload[k] for k in
                      ("runs_examined", "scalar_rows", "would_park",
                       "would_park_rate", "no_backing", "unresolvable",
                       "excluded_confirmed", "excluded_contested")}),
          flush=True)
    print(f"artifact -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
