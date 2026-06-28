#!/usr/bin/env python
"""Recompute confidence for existing agent-origin edges using the same
edge_confidence() the live link path uses, so graph_review's dubious detector is
meaningful for the CURRENT graph. Dry-run by default. Idempotent (pure recompute).

Per the live-bank lesson: BACK UP FIRST (ops/backup.ps1), plain psycopg.connect()
with lock/statement timeouts, idempotent UPDATE only — no DDL, no PostgresStorage().

Usage:
  python ops/backfill_edge_confidence.py            # dry-run: print distribution
  python ops/backfill_edge_confidence.py --apply    # write the new confidences
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from pseudolife_memory.memory.relation_quality import edge_confidence

_SELECT = """
SELECT e.id, s.display, e.relation, d.display, e.confidence
FROM edges e
JOIN entities s ON e.src_id = s.id
JOIN entities d ON e.dst_id = d.id
WHERE e.origin = 'agent' AND e.superseded_at IS NULL
ORDER BY e.id
"""


def recompute_rows(rows: list[tuple]) -> list[tuple[int, float]]:
    """Pure: (id, src, relation, dst, old_conf) -> (id, new_conf)."""
    return [(rid, edge_confidence(src, rel, dst))
            for (rid, src, rel, dst, _old) in rows]


def _dsn() -> str:
    return os.environ.get(
        "PSEUDOLIFE_MCP_DATABASE_URL",
        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory")


def main() -> int:
    apply = "--apply" in sys.argv
    import psycopg
    with psycopg.connect(_dsn(), connect_timeout=5) as conn:
        conn.execute("SET lock_timeout = '5s'")
        conn.execute("SET statement_timeout = '30s'")
        rows = conn.execute(_SELECT).fetchall()
        updates = recompute_rows(rows)
        # (old or 0): a NULL confidence coerces to 0 so NULL edges are always recomputed (intended).
        changed = [(rid, new) for (rid, _s, _r, _d, old), (_rid2, new)
                   in zip(rows, updates) if abs((old or 0) - new) > 1e-6]
        dist = Counter(round(new, 3) for _id, new in updates)
        print(f"agent edges: {len(rows)}; would change: {len(changed)}")
        print("new-confidence distribution:", dict(sorted(dist.items())))
        for rid, new in changed[:15]:
            src, rel, dst = next((s, r, d) for (i, s, r, d, _o) in rows if i == rid)
            print(f"  edge {rid}: {src} --{rel}--> {dst}  -> {new}")
        if not apply:
            print("\n[DRY RUN] re-run with --apply to write")
            return 0
        with conn.cursor() as cur:
            cur.executemany("UPDATE edges SET confidence = %s WHERE id = %s",
                            [(new, rid) for rid, new in changed])
        conn.commit()
        print(f"\napplied {len(changed)} updates")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
