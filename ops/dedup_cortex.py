"""One-time cortex sibling-slot cleanup (dry-run by default).

Collapses paraphrase fragments that past regex auto-promotes left behind — e.g.
``payments-db / host`` and ``payments / database host`` for one fact — by keeping
the canonical slot (strongest provenance tier, then most-recent) and retiring the
rest. Reversible: it retires (status -> superseded), never deletes.

SAFETY
------
* BACK UP FIRST: run ``ops/backup.ps1`` before ``--apply``.
* Prefer running while the daemon is stopped / quiescent — both this script and a
  running daemon write the cortex snapshot, so don't let them race.
* Dry-run (the default) reports proposed merges and writes nothing. Review the
  clusters before ``--apply``.

It connects to the same bank as the daemon via the standard env vars
(``PSEUDOLIFE_MCP_DATABASE_URL`` / ``PSEUDOLIFE_MCP_DATA_DIR`` /
``PSEUDOLIFE_MCP_CONFIG``).

    python ops/dedup_cortex.py                 # dry-run report
    python ops/dedup_cortex.py --threshold 0.92
    python ops/dedup_cortex.py --apply         # commit (after a backup)
"""
from __future__ import annotations

import argparse
import json
import os

from pseudolife_memory.service import MemoryService


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Cortex sibling-slot dedup (dry-run by default; --apply to commit).",
    )
    ap.add_argument("--threshold", type=float, default=0.90,
                    help="slot-embedding cosine floor to merge (default 0.90)")
    ap.add_argument("--apply", action="store_true",
                    help="commit the merges (back up the bank first)")
    args = ap.parse_args()

    if args.apply:
        print("APPLY mode — ensure you ran ops/backup.ps1 and the daemon is "
              "stopped/quiescent first.\n")

    svc = MemoryService(
        data_dir=os.environ.get("PSEUDOLIFE_MCP_DATA_DIR"),
        config_path=os.environ.get("PSEUDOLIFE_MCP_CONFIG"),
    )
    try:
        rep = svc.cortex_dedup(threshold=args.threshold, dry_run=not args.apply)
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        verb = "retired" if args.apply else "would be retired"
        print(f"\n{rep['merged']} slot(s) {verb} across "
              f"{len(rep['clusters'])} cluster(s) at threshold {rep['threshold']}.")
        if not args.apply and rep["merged"]:
            print("Review the clusters above, then re-run with --apply (after a backup).")
    finally:
        svc.flush()


if __name__ == "__main__":
    main()
