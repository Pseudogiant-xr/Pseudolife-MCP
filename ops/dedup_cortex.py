"""One-time cortex sibling-slot cleanup (dry-run by default).

Collapses paraphrase fragments that past regex auto-promotes left behind — e.g.
``payments-db / host`` and ``payments / database host`` for one fact — by keeping
the canonical slot (strongest provenance tier, then most-recent) and retiring the
rest. Reversible: it retires (status -> superseded), never deletes.

SAFETY
------
* BACK UP FIRST: run ``ops/backup.ps1`` before ``--apply``.
* Stop the daemon first. The script opens the bank as its writer, and a bank
  has exactly one: while a daemon holds it, the script refuses to start
  (exit 2, naming the process that holds the writer lease).
* Dry-run (the default) reports proposed merges and saves nothing (opening the
  bank still runs the startup bookkeeping every service start does). Review
  the clusters before ``--apply``, which saves per slot like the daemon's
  autosave and never rewrites a whole table.

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
import sys

from pseudolife_memory.service import MemoryService
from pseudolife_memory.storage.postgres import WriterLeaseHeld


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
              "stopped first.\n")

    svc = MemoryService(
        data_dir=os.environ.get("PSEUDOLIFE_MCP_DATA_DIR"),
        config_path=os.environ.get("PSEUDOLIFE_MCP_CONFIG"),
    )
    try:
        rep = svc.cortex_dedup(threshold=args.threshold, dry_run=not args.apply)
    except WriterLeaseHeld as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(rep, indent=2, ensure_ascii=False))
    verb = "retired" if args.apply else "would be retired"
    print(f"\n{rep['merged']} slot(s) {verb} across "
          f"{len(rep['clusters'])} cluster(s) at threshold {rep['threshold']}.")
    if args.apply:
        # Per-slot, like the daemon's own saves: only what changed is
        # written. Never flush(), whose full snapshot DELETEs and re-inserts
        # every fact, world fact and lesson from this process's copy.
        svc.autosave_if_changed()
    elif rep["merged"]:
        print("Review the clusters above, then re-run with --apply (after a backup).")


if __name__ == "__main__":
    main()
