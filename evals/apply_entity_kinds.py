"""Apply an entity-kind artifact to the bank (human-gated).

Two writes, both reversible: entity_kinds rows, and a recompute of
facts.freshness_class through the SAME resolve_class the write path uses --
one policy, not two implementations that drift.

Reverting: `UPDATE facts SET freshness_class='evergreen'` restores the
pre-run state wholesale, and dropping entity_kinds reverts the write path.
BACK UP FIRST -- ops/backup.ps1.

Usage:
    python evals/apply_entity_kinds.py --artifact <path>            # dry run
    python evals/apply_entity_kinds.py --artifact <path> --apply
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import psycopg

DEFAULT_DSN = os.environ.get(
    "PL_DSN", "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory")

# Same single-copy rule as the classifier: load freshness.py by path so the
# recompute uses the EXACT policy the write path uses. Two implementations
# would drift, and the drift would be invisible -- the backfill would write
# classes new writes never reproduce.
def _load_freshness():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pl_freshness",
        Path(__file__).resolve().parents[1]
        / "pseudolife_memory" / "memory" / "freshness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_freshness = _load_freshness()


def _resolve(kind: str | None, attribute_norm: str) -> str:
    return _freshness.resolve_class(kind, attribute_norm)


def plan_updates(labels: dict[str, str], rows: list[tuple[str, str]],
                 current: dict[tuple[str, str], str] | None = None
                 ) -> list[tuple[str, str, str]]:
    """(entity, attribute, new_class) for every pair whose class changes."""
    cur = current or {}
    out = []
    for e, a in rows:
        want = _resolve(labels.get(e), a)
        if want != cur.get((e, a), "evergreen"):
            out.append((e, a, want))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--artifact", type=Path, required=True)
    p.add_argument("--dsn", default=DEFAULT_DSN)
    p.add_argument("--apply", action="store_true",
                   help="Without this the run is a dry run and writes nothing.")
    a = p.parse_args()

    art = json.loads(a.artifact.read_text(encoding="utf-8"))
    labels = art["labels"]

    with psycopg.connect(a.dsn) as conn:
        rows = [(r[0], r[1]) for r in conn.execute(
            "SELECT entity_norm, attribute_norm FROM facts "
            "WHERE status='current'").fetchall()]
        current = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT entity_norm, attribute_norm, freshness_class FROM facts "
            "WHERE status='current'").fetchall()}

        updates = plan_updates(labels, rows, current)
        print(f"kinds={len(labels)} fact_updates={len(updates)}")
        for e, at, c in updates[:15]:
            print(f"  {e} / {at} -> {c}")
        if not a.apply:
            print("dry run -- nothing written. Re-run with --apply.")
            return

        now = time.time()
        with conn.transaction():
            for e, k in labels.items():
                conn.execute(
                    "INSERT INTO entity_kinds "
                    "(entity_norm, kind, origin, confidence, decided_at) "
                    "VALUES (%s,%s,'model',NULL,%s) "
                    "ON CONFLICT (entity_norm) DO UPDATE SET "
                    "kind=EXCLUDED.kind, origin=EXCLUDED.origin, "
                    "decided_at=EXCLUDED.decided_at", (e, k, now))
            for e, at, c in updates:
                conn.execute(
                    "UPDATE facts SET freshness_class=%s "
                    "WHERE entity_norm=%s AND attribute_norm=%s AND status='current'",
                    (c, e, at))
    print(f"applied {len(labels)} kinds, {len(updates)} fact updates")


if __name__ == "__main__":
    main()
