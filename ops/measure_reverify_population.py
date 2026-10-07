"""Measure how many served cortex facts carry (or would carry) ``re_verify``.

The correction warning is a population claim as much as a per-fact one: it is
deliberately kept OUT of ``correct_with`` because it fires on a large share of
a mature bank, and that share is the reason. This script re-measures it so the
comment and the guide cite a dated figure instead of an inherited one.

Reports, for one bank:

* ``current_facts`` — live cortex rows (scalar records and set members).
* ``bare_flag_facts`` — current facts whose slot has ANY traced source entry
  that is superseded. This is the latching "any source superseded" test the
  ``last_confirmed`` comparison exists to reject; it is reported as the
  upper bound that motivates the comparison.
* ``keyed_flag_facts`` — current facts whose slot has a traced source entry
  superseded AFTER the fact was last confirmed. This is what is actually
  served, and what schema v39 makes durable.
* ``trace_supersession_pairs`` — surviving ``memory_traces`` x superseded
  ``entries`` pairs. This is exactly the row count the one-time v39 backfill
  inserts into ``memory_trace_invalidations`` on the first upgraded start.

READ-ONLY: every read runs inside one ``SET TRANSACTION READ ONLY`` block, so
this is safe to run against a live bank with the daemon up. A statement that
tried to write would be refused by the server rather than silently land.
"""

from __future__ import annotations

import json
import os
import sys

# ``facts.last_confirmed`` is NOT NULL but legacy rows can carry 0.0, which
# would make every source supersession look newer than the confirmation. The
# service falls back to ``asserted_at`` in exactly that case; mirror it.
_SEEN = "COALESCE(NULLIF(f.last_confirmed, 0), f.asserted_at)"

_QUERIES = {
    "current_facts":
        "SELECT count(*) FROM facts f WHERE f.status = 'current'",
    "bare_flag_facts":
        "SELECT count(*) FROM facts f WHERE f.status = 'current' AND EXISTS ("
        "  SELECT 1 FROM memory_traces t"
        "  JOIN entries e ON e.id = t.entry_id"
        "  WHERE t.entity_norm = f.entity_norm"
        "    AND t.attribute_norm = f.attribute_norm"
        "    AND e.superseded_at IS NOT NULL)",
    "keyed_flag_facts":
        "SELECT count(*) FROM facts f WHERE f.status = 'current' AND EXISTS ("
        "  SELECT 1 FROM memory_traces t"
        "  JOIN entries e ON e.id = t.entry_id"
        "  WHERE t.entity_norm = f.entity_norm"
        "    AND t.attribute_norm = f.attribute_norm"
        "    AND e.superseded_at IS NOT NULL"
        f"    AND e.superseded_at > {_SEEN})",
    "trace_supersession_pairs":
        "SELECT count(*) FROM memory_traces t"
        " JOIN entries e ON e.id = t.entry_id"
        " WHERE e.superseded_at IS NOT NULL",
}


def run(conn) -> dict:
    """Measure an open psycopg connection. Mutates nothing.

    The reads share one read-only transaction so the counts are mutually
    consistent on a live bank, and so a future edit that slips a write into
    :data:`_QUERIES` is refused by the server instead of landing. The
    transaction-scoped setting leaves the caller's session unchanged.
    """
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL search_path TO public")
        counts = {name: int(conn.execute(sql).fetchone()[0])
                  for name, sql in _QUERIES.items()}
    total = counts["current_facts"]
    for name in ("bare_flag_facts", "keyed_flag_facts"):
        counts[f"{name}_pct"] = (
            round(100.0 * counts[name] / total, 1) if total else 0.0)
    return counts


def main() -> None:
    import argparse

    import psycopg

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--database-url",
                    default=os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL"),
                    help="Postgres DSN (or set PSEUDOLIFE_MCP_DATABASE_URL)")
    ap.add_argument("--json", action="store_true",
                    help="emit the counts as JSON instead of a report")
    args = ap.parse_args()
    if not args.database_url:
        print("error: provide --database-url or set PSEUDOLIFE_MCP_DATABASE_URL",
              file=sys.stderr)
        sys.exit(2)
    with psycopg.connect(args.database_url, autocommit=True) as conn:
        counts = run(conn)
    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
        return
    print("=== re_verify population (read-only) ===")
    print(f"  current facts:                {counts['current_facts']}")
    print(f"  bare 'any source superseded': {counts['bare_flag_facts']} "
          f"({counts['bare_flag_facts_pct']}%)")
    print(f"  keyed on last_confirmed:      {counts['keyed_flag_facts']} "
          f"({counts['keyed_flag_facts_pct']}%)  <- served")
    print(f"  trace x superseded pairs:     "
          f"{counts['trace_supersession_pairs']}  <- v39 backfill inserts")


if __name__ == "__main__":
    main()
