"""Legacy v≤7 .pt bank → schema v8 Postgres migration (P1.6).

Runs once: only when the entries table is empty AND a legacy
``memory_state/cms_state.pt`` exists under the data dir. Sources are
renamed ``*.pre-v8.bak`` afterwards — never deleted.

Both the entries branch AND the cortex-facts branch re-embed any row whose
stored embedding dim doesn't match the live schema's before inserting it
(2026-07-28, embedding-backbone-v25 review escalation) — a real legacy
bank always predates the current model/dimension for BOTH tables, and a
verbatim insert into a differently-dimensioned vector column raises after
entries has already committed, permanently blocking retry via the
idempotency guard below (``storage.load_entries() or
storage.load_facts()``) while the sources never get their ``.pre-v8.bak``
rename — silent, permanent fact loss. See ``migrate_legacy``'s docstring.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_legacy(data_dir: str | Path, storage, embedder) -> dict:
    """Import a legacy .pt bank into storage. Idempotent.

    ``embedder`` is the service's own, already-constructed
    :class:`~pseudolife_memory.memory.embedding.EmbeddingPipeline` (threaded
    in, not re-instantiated — single-copy principle). Every real legacy .pt
    bank in the wild predates the current build's embedder, so its stored
    entry AND fact embeddings can be dimensioned for an older model (e.g.
    384-d MiniLM) than the live schema's vector columns (1024-d as of
    schema v25). Inserting them verbatim raises a pgvector dimension error
    on every boot's migration attempt (swallowed to a warning by the caller
    and retried next boot — see ``MemoryService._ensure_init``). Any entry
    whose stored embedding dim doesn't match ``embedder.embedding_dim`` is
    instead re-embedded from its own stored TEXT through this pipeline
    before insertion; any cortex fact whose stored embedding dim doesn't
    match is re-embedded from its own (entity, attribute, value) claim text
    the same way — so every migrated row always fits the live column.

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

    target_dim = embedder.embedding_dim
    entries = episodes = facts = reembedded = 0

    if cms_path.exists():
        # weights_only=True: legacy CMS snapshot is tensors + plain containers;
        # avoid unpickling arbitrary objects from an imported .pt bank (CWE-502).
        state = torch.load(str(cms_path), map_location="cpu", weights_only=True)
        # Episodes first (entries carry episode_id FKs).
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
                embedding = e["embedding"]
                if len(embedding) != target_dim:
                    # Legacy dim doesn't fit this build's vector column —
                    # re-embed through the real pipeline rather than
                    # inserting a wrong-shaped vector (which either raises
                    # at the DB or, worse, would silently never match
                    # anything at search time if the column allowed it).
                    embedding = embedder.encode_single(e["text"])
                    reembedded += 1
                storage.insert_entry({
                    "band": band_name,
                    "text": e["text"],
                    "embedding": embedding,
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

    reembedded_facts = 0
    if cortex_path.exists():
        from pseudolife_memory.memory.cortex import CortexStore
        cortex = CortexStore()
        cortex.load(cortex_path)
        for r in cortex.records:
            if r.embedding is not None and len(r.embedding) != target_dim:
                # Same treatment as the entries branch above, and for the
                # same reason: a legacy fact's stored claim embedding
                # predates this build's model/dimension. Inserting it
                # verbatim raises a pgvector dimension error INSIDE
                # storage.replace_facts (a single-statement bulk insert),
                # i.e. AFTER the entries loop above already committed --
                # the `.pre-v8.bak` rename below is never reached, and the
                # idempotency guard at the top of this function
                # (`storage.load_entries() or storage.load_facts()`) then
                # permanently blocks every retry because entries is now
                # non-empty: the legacy facts are lost forever with no way
                # to re-attempt the migration (2026-07-28 review
                # escalation). Re-embed from the record's own
                # (entity, attribute, value) using the EXACT claim-text
                # shape ``MemoryService.cortex_write`` commits
                # (service.py ~line 1509:
                # ``f"{entity} {attribute} {value}".strip()``) so the row
                # fits the live vector column before it is ever inserted.
                claim = f"{r.entity} {r.attribute} {r.value}".strip()
                r.embedding = embedder.encode_single(claim)
                reembedded_facts += 1
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
               "episodes": episodes, "facts": facts, "reembedded": reembedded,
               "reembedded_facts": reembedded_facts}
    logger.warning("legacy bank migrated to schema v8: %s", summary)
    return summary
