"""Legacy v≤7 .pt bank → schema v8 Postgres migration (P1.6).

Runs once: only when the entries table is empty AND a legacy
``memory_state/cms_state.pt`` exists under the data dir. Sources are
renamed ``*.pre-v8.bak`` afterwards — never deleted.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_legacy(data_dir: str | Path, storage) -> dict:
    """Import a legacy .pt bank into storage. Idempotent.

    Returns ``{"migrated": bool, ...counts}``.
    """
    data_dir = Path(data_dir)
    cms_path = data_dir / "memory_state" / "cms_state.pt"
    cortex_path = data_dir / "cortex_state.pt"
    if not cms_path.exists() and not cortex_path.exists():
        return {"migrated": False, "reason": "no_legacy_state"}
    if storage.load_entries() or storage.load_facts():
        return {"migrated": False, "reason": "storage_not_empty"}

    import torch

    from pseudolife_memory.storage.sync import _record_to_row

    entries = episodes = facts = 0

    if cms_path.exists():
        state = torch.load(str(cms_path), map_location="cpu", weights_only=False)
        # Episodes first (entries carry episode_id FKs).
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
        storage.meta_set("migrated_interaction_count",
                         int(state.get("interaction_count", 0)))

    if cortex_path.exists():
        from pseudolife_memory.memory.cortex import CortexStore
        cortex = CortexStore()
        cortex.load(cortex_path)
        rows = [_record_to_row(r) for r in cortex.records]
        storage.replace_facts(rows)
        storage.meta_set("cortex_supersession_log", cortex.supersession_log[-200:])
        storage.meta_set("cortex_dream_cursor", cortex.dream_cursor)
        facts = len(rows)

    # Rename sources — migration is read-only on content, rename-only on
    # the filesystem, and never deletes.
    for p in (cms_path, cortex_path):
        if p.exists():
            p.rename(p.with_name(p.name + ".pre-v8.bak"))

    summary = {"migrated": True, "entries": entries,
               "episodes": episodes, "facts": facts}
    logger.warning("legacy bank migrated to schema v8: %s", summary)
    return summary
