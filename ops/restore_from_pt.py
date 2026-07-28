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

DIMENSION SAFETY (2026-07-28, embedding-backbone-v25 review escalation):
this script restores a SAME-ERA snapshot verbatim — it does not re-embed,
unlike storage.migrate.migrate_legacy (whose whole job is bridging a
legacy .pt bank across a model/dimension change). ``.pt`` snapshots are
taken alongside a specific build, so the expected case is that a snapshot
matches the live bank's current embedding dimension. If it doesn't --
e.g. a pre-v25 384-d ``.bak`` restored after the live bank has already
moved to v25's vector(1024) — inserting those vectors verbatim would
either raise a pgvector dimension error partway through the entries loop
(each ``insert_entry`` commits on its own, so a failure here leaves a
PARTIAL restore: some entries land, the rest silently don't, and the
script never says so) or, if this script grows a bulk/batched insert path
later, silently write wrong-shaped data. Rather than guess a re-embedding
policy for what is explicitly a "restore what was actually captured"
tool, this script instead compares every stored embedding's length
against the live bank's declared dimension BEFORE writing anything, and
refuses outright (no partial writes) on any mismatch — the operator
picked the wrong ``.bak`` (or is trying to replay across a migration) and
should reach for ``ops/migrate_embeddings.py`` (empty-bank case) or
re-run the restore with the RIGHT snapshot instead.

    python ops/restore_from_pt.py \
        --dsn postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _dim_mismatches(state: dict | None, cortex, expected_dim: int) -> list[tuple[str, str, int]]:
    """Every stored embedding in the loaded snapshot(s) whose length isn't
    ``expected_dim`` — checked BEFORE any DB write, so a mismatch refuses
    the whole run instead of failing partway through (see module
    docstring, "DIMENSION SAFETY")."""
    bad: list[tuple[str, str, int]] = []
    for band_state in (state or {}).get("bands", {}).values():
        for e in band_state.get("entries", []):
            dim = len(e["embedding"])
            if dim != expected_dim:
                bad.append(("entry", (e.get("text") or "")[:40], dim))
    for r in (cortex.records if cortex is not None else []):
        if r.embedding is not None and len(r.embedding) != expected_dim:
            bad.append(("fact", f"{r.entity}.{r.attribute}", len(r.embedding)))
    return bad


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
    from pseudolife_memory.storage.schema import _EXPECTED_EMBEDDING_DIM
    from pseudolife_memory.storage.sync import _record_to_row

    storage = PostgresStorage(args.dsn)
    before = len(storage.load_entries())
    print(f"live bank before: {before} entries, "
          f"{len(storage.load_facts())} facts")

    # Load BOTH snapshots fully before writing anything — the dimension
    # check below must see everything up front so a mismatch refuses the
    # whole run rather than aborting after some entries/episodes already
    # landed (PostgresStorage's own constructor already guarantees the
    # LIVE bank matches _EXPECTED_EMBEDDING_DIM — it would have refused to
    # even construct otherwise — so that constant is exactly the dimension
    # every stored vector below must match).
    state = None
    if cms_path.exists():
        # weights_only=True: same guard as storage.migrate's legacy loader —
        # this reads the same .pt format, so importing a stale/tampered .bak
        # must not be able to unpickle arbitrary objects (CWE-502).
        state = torch.load(str(cms_path), map_location="cpu", weights_only=True)
    cortex = None
    if cortex_path.exists():
        from pseudolife_memory.memory.cortex import CortexStore
        cortex = CortexStore()
        cortex.load(cortex_path)

    mismatches = _dim_mismatches(state, cortex, _EXPECTED_EMBEDDING_DIM)
    if mismatches:
        print(f"\nREFUSING: {len(mismatches)} stored embedding(s) in the "
              f"snapshot are not vector({_EXPECTED_EMBEDDING_DIM}) — this "
              "script restores a same-era snapshot verbatim, it does not "
              "re-embed (see module docstring). Sample mismatches:",
              file=sys.stderr)
        for kind, label, dim in mismatches[:5]:
            print(f"  {kind} {label!r}: vector({dim})", file=sys.stderr)
        print("\nNothing was written. Use the matching snapshot, or "
              "ops/migrate_embeddings.py for a genuine dimension change.",
              file=sys.stderr)
        storage.close()
        sys.exit(1)

    entries = episodes = facts = 0

    if state is not None:
        print(f"loading entries from {cms_path}")
        ep_payload = (state.get("episodes") or {}).get("episodes") or {}
        for _eid, ep in ep_payload.items():
            storage.upsert_episode({
                "id": ep["id"], "title": ep["title"], "hint": ep.get("hint"),
                "started_at": ep["started_at"], "ended_at": ep.get("ended_at"),
                "closed_by_new_start": bool(ep.get("closed_by_new_start")),
                "session_key": ep.get("session_key"),
                "parent_id": ep.get("parent_id"),
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

    if cortex is not None:
        print(f"loading facts from {cortex_path}")
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
