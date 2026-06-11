"""One-off recovery: import a legacy/snapshot .pt bank ADDITIVELY into the
live Postgres bank, preserving rows already present.

Unlike storage.migrate.migrate_legacy (which is guarded to an *empty* bank
and renames the sources), this is for restoring after an accidental DB
wipe when the bank already has some newer rows you want to keep. It:

  * reads ``memory_state/cms_state.pt.pre-v8.bak`` + ``cortex_state.pt.pre-v8.bak``
    (override via --cms / --cortex),
  * INSERTs entries + episodes (BIGSERIAL assigns fresh ids; no collision),
  * INSERTs cortex facts one-by-one (never DELETEs existing facts),
  * leaves the .bak files untouched.

Run with the daemon STOPPED (it must re-hydrate afterward, and a running
daemon's cortex snapshot would otherwise rewrite the facts table).

    python ops/restore_from_pt.py \
        --dsn postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get(
        "PSEUDOLIFE_MCP_DATABASE_URL",
        "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory",
    ))
    ap.add_argument("--data-dir", default=os.environ.get(
        "PSEUDOLIFE_MCP_DATA_DIR", str(Path.home() / ".pseudolife-mcp")))
    ap.add_argument("--cms", default=None)
    ap.add_argument("--cortex", default=None)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    cms_path = Path(args.cms) if args.cms else (
        data_dir / "memory_state" / "cms_state.pt.pre-v8.bak")
    cortex_path = Path(args.cortex) if args.cortex else (
        data_dir / "cortex_state.pt.pre-v8.bak")

    import torch

    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.storage.sync import _record_to_row

    storage = PostgresStorage(args.dsn)
    before = len(storage.load_entries())
    print(f"live bank before: {before} entries, "
          f"{len(storage.load_facts())} facts")

    entries = episodes = facts = 0

    if cms_path.exists():
        print(f"loading entries from {cms_path}")
        state = torch.load(str(cms_path), map_location="cpu", weights_only=False)
        ep_payload = (state.get("episodes") or {}).get("episodes") or {}
        for _eid, ep in ep_payload.items():
            storage.upsert_episode({
                "id": ep["id"], "title": ep["title"], "hint": ep.get("hint"),
                "started_at": ep["started_at"], "ended_at": ep.get("ended_at"),
                "closed_by_new_start": bool(ep.get("closed_by_new_start")),
            })
            episodes += 1
        for band_name, band_state in (state.get("bands") or {}).items():
            for e in band_state.get("entries", []):
                storage.insert_entry({
                    "band": band_name,
                    "text": e["text"],
                    "embedding": e["embedding"],
                    "surprise": float(e.get("surprise_score", 0.0)),
                    "ts": float(e.get("timestamp", 0.0)),
                    "access_count": int(e.get("access_count", 0)),
                    "source": e.get("source", ""),
                    "superseded_at": e.get("superseded_at"),
                    "superseded_by_text": e.get("superseded_by_text"),
                    "last_logical_turn": e.get("last_logical_turn"),
                    "episode_id": e.get("episode_id"),
                    "episode_title": e.get("episode_title"),
                    "tags": list(e.get("tags") or []),
                    "slots": [list(s) for s in (e.get("slots") or [])],
                })
                entries += 1
    else:
        print(f"WARN: no cms snapshot at {cms_path}")

    if cortex_path.exists():
        print(f"loading facts from {cortex_path}")
        from pseudolife_memory.memory.cortex import CortexStore
        cortex = CortexStore()
        cortex.load(cortex_path)
        for rec in cortex.records:
            row = _record_to_row(rec)
            row.pop("id", None)  # force INSERT
            storage.upsert_fact(row)
            facts += 1
    else:
        print(f"WARN: no cortex snapshot at {cortex_path}")

    after = len(storage.load_entries())
    print(f"imported: {entries} entries, {episodes} episodes, {facts} facts")
    print(f"live bank after:  {after} entries, "
          f"{len(storage.load_facts())} facts")
    storage.close()


if __name__ == "__main__":
    main()
