"""Conversions + hydration between in-memory structures and PostgresStorage.

The bands / cortex stay the hot path; these helpers are the only place
that knows how a :class:`MemoryEntry` or :class:`CortexRecord` maps onto
a schema-v8 row. Cortex persistence is snapshot-style (full rewrite per
mutation): the cortex is small by design, and one transactional rewrite
is simpler and strictly safer than row diffing. Per-row upserts arrive
with Phase 2 when entity links make ids meaningful.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import torch

from pseudolife_memory.memory.cortex import CortexRecord, CortexStore
from pseudolife_memory.memory.episodes import Episode, EpisodeManager
from pseudolife_memory.memory.titans_memory import MemoryEntry

_CORTEX_LOG_KEY = "cortex_supersession_log"
_CORTEX_CURSOR_KEY = "cortex_dream_cursor"


# v11 writer-aware temporal stamp — shared mapping for every canonical record.
def _stamp_to_row(r) -> dict[str, Any]:
    return {
        "tx_time": r.tx_time,
        "valid_time": r.valid_time,
        "hlc_phys": r.hlc_phys,
        "hlc_logical": r.hlc_logical,
        "writer_id": r.writer_id,
        "session_id": r.session_id,
        "version": getattr(r, "version", 1) or 1,
    }


def _stamp_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "tx_time": row.get("tx_time"),
        "valid_time": row.get("valid_time"),
        "hlc_phys": row.get("hlc_phys"),
        "hlc_logical": row.get("hlc_logical"),
        "writer_id": row.get("writer_id"),
        "session_id": row.get("session_id"),
        "version": row.get("version", 1) or 1,
    }


# ── entries ──────────────────────────────────────────────────────────────

def entry_to_row(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "band": entry.bank,
        "text": entry.text,
        "embedding": entry.embedding,
        "surprise": float(entry.surprise_score),
        "ts": float(entry.timestamp),
        "access_count": int(entry.access_count),
        "source": entry.source,
        "superseded_at": entry.superseded_at,
        "superseded_by_text": entry.superseded_by_text,
        "last_logical_turn": entry.last_logical_turn,
        "episode_id": entry.episode_id,
        "episode_title": entry.episode_title,
        "tags": list(entry.tags),
        "slots": [list(s) for s in entry.slots],
    }


def row_to_entry(row: dict[str, Any], device: str = "cpu") -> MemoryEntry:
    return MemoryEntry(
        text=row["text"],
        embedding=torch.as_tensor(row["embedding"], dtype=torch.float32).to(device),
        surprise_score=row["surprise"],
        timestamp=row["ts"],
        access_count=row["access_count"],
        source=row["source"],
        bank=row["band"],
        superseded_at=row["superseded_at"],
        superseded_by_text=row["superseded_by_text"],
        last_logical_turn=row["last_logical_turn"],
        slots=[tuple(s) for s in (row["slots"] or [])],
        episode_id=row["episode_id"],
        episode_title=row["episode_title"],
        tags=list(row["tags"] or []),
        db_id=row["id"],
        reinforcements=row.get("reinforcements", 0),
    )


def hydrate_cms(cms, storage) -> int:
    """Fill band entries + episode log from storage. Returns entry count.

    Rows whose ``band`` no longer exists (preset change) land in the
    first band rather than being dropped.
    """
    named = {b.name: b for b in cms.bands}
    count = 0
    for row in storage.load_entries():
        band = named.get(row["band"], cms.bands[0])
        band.entries.append(row_to_entry(row, device=band.device))
        band._dirty = True
        count += 1
    em = EpisodeManager()
    for ep in storage.load_episodes():
        em.episodes[ep["id"]] = Episode(**ep)
    # An episode left open by a dead daemon stays open — same semantics
    # as the torch.save round-trip (auto-close happens on next start()).
    open_eps = [e for e in em.episodes.values() if e.ended_at is None]
    em.current_id = open_eps[-1].id if open_eps else None
    cms.episodes = em
    return count


def episode_row(ep: Episode) -> dict[str, Any]:
    return asdict(ep)


# ── cortex ───────────────────────────────────────────────────────────────

def _record_to_row(r: CortexRecord) -> dict[str, Any]:
    from pseudolife_memory.memory.cortex import _norm_key

    return {
        "entity": r.entity,
        "attribute": r.attribute,
        "entity_norm": _norm_key(r.entity),
        "attribute_norm": _norm_key(r.attribute),
        "value": r.value,
        "polarity": r.polarity,
        "status": r.status,
        "confidence": float(r.confidence),
        "origin": r.origin or None,
        "support": sorted(r.support),
        "provenance": sorted(r.provenance),
        "asserted_at": float(r.asserted_at),
        "last_confirmed": float(r.last_confirmed),
        "supersedes_value": r.supersedes_value,
        "superseded_by_value": r.superseded_by_value,
        "superseded_at": r.superseded_at,
        "embedding": r.embedding,
        "entity_id": None,          # linked by snapshot_cortex
        "object_entity_id": None,   # linked by snapshot_cortex
        **_stamp_to_row(r),
    }


def snapshot_cortex(cortex: CortexStore, storage) -> int:
    """Transactionally rewrite the facts table from the in-memory cortex.

    Auto-linking (spec §5.1): each row's subject — and its value, when
    the value names a known entity or alias — gets resolved against the
    entities table so the cortex and the graph stay joined.
    """
    from pseudolife_memory.graph import norm_name

    rows = [_record_to_row(r) for r in cortex.records]
    emap = storage.entity_id_map()
    for row in rows:
        row["entity_id"] = emap.get(norm_name(row["entity"]))
        row["object_entity_id"] = emap.get(norm_name(row["value"]))
    storage.replace_facts(rows)
    storage.meta_set(_CORTEX_LOG_KEY, cortex.supersession_log[-200:])
    storage.meta_set(_CORTEX_CURSOR_KEY, cortex.dream_cursor)
    return len(rows)


def hydrate_cortex(cortex: CortexStore, storage) -> int:
    """Fill the cortex from the facts table. Returns record count."""
    cortex.records = []
    for row in storage.load_facts():
        emb = row["embedding"]
        cortex.records.append(CortexRecord(
            entity=row["entity"],
            attribute=row["attribute"],
            value=row["value"],
            polarity=row["polarity"],
            confidence=row["confidence"],
            status=row["status"],
            provenance=set(row["provenance"] or []),
            asserted_at=row["asserted_at"],
            last_confirmed=row["last_confirmed"],
            supersedes_value=row["supersedes_value"],
            superseded_by_value=row["superseded_by_value"],
            superseded_at=row["superseded_at"],
            embedding=(torch.as_tensor(emb, dtype=torch.float32)
                       if emb is not None else None),
            support=set(row["support"] or []),
            **_stamp_from_row(row),
        ))
    cortex.supersession_log = list(storage.meta_get(_CORTEX_LOG_KEY, []) or [])
    cortex.dream_cursor = float(storage.meta_get(_CORTEX_CURSOR_KEY, 0.0) or 0.0)
    cortex._reindex_current()
    return len(cortex.records)


# ── world cortex (schema v9) ──────────────────────────────────────────────

def _world_record_to_row(r) -> dict[str, Any]:
    from pseudolife_memory.memory.cortex import _norm_key

    return {
        "entity": r.entity,
        "attribute": r.attribute,
        "entity_norm": _norm_key(r.entity),
        "attribute_norm": _norm_key(r.attribute),
        "value": r.value,
        "polarity": r.polarity,
        "status": r.status,
        "confidence": float(r.confidence),
        "origin": "source",
        "support": ["source"],
        "provenance": [],
        "asserted_at": float(r.asserted_at),
        "last_confirmed": float(r.last_confirmed),
        "supersedes_value": r.supersedes_value,
        "superseded_by_value": r.superseded_by_value,
        "superseded_at": r.superseded_at,
        "embedding": r.embedding,
        "source_url": r.source_url or "",
        "source_quote": r.source_quote or "",
        "retrieved_at": float(r.retrieved_at or 0.0),
        "freshness_class": r.freshness_class or "volatile",
        "content_hash": r.content_hash,
        "source_doc_id": r.source_doc_id,
        **_stamp_to_row(r),
    }


def snapshot_world_cortex(world, storage) -> int:
    """Transactionally rewrite the world_facts table from the in-memory world cortex.
    No entity-graph auto-linking in v1 (world facts are external)."""
    rows = [_world_record_to_row(r) for r in world.records]
    storage.replace_world_facts(rows)
    return len(rows)


def hydrate_world_cortex(world, storage) -> int:
    """Fill the world cortex from the world_facts table. Returns record count."""
    from pseudolife_memory.memory.world_cortex import WorldRecord

    world.records = []
    for row in storage.load_world_facts():
        emb = row["embedding"]
        world.records.append(WorldRecord(
            entity=row["entity"],
            attribute=row["attribute"],
            value=row["value"],
            polarity=row["polarity"],
            confidence=row["confidence"],
            status=row["status"],
            source_url=row["source_url"] or "",
            source_quote=row["source_quote"] or "",
            freshness_class=row["freshness_class"] or "volatile",
            retrieved_at=row["retrieved_at"] or 0.0,
            content_hash=row["content_hash"],
            source_doc_id=row["source_doc_id"],
            asserted_at=row["asserted_at"],
            last_confirmed=row["last_confirmed"],
            supersedes_value=row["supersedes_value"],
            superseded_by_value=row["superseded_by_value"],
            superseded_at=row["superseded_at"],
            embedding=(torch.as_tensor(emb, dtype=torch.float32)
                       if emb is not None else None),
            **_stamp_from_row(row),
        ))
    world._current = {
        r.key: i for i, r in enumerate(world.records) if r.status == "current"
    }
    return len(world.records)


# ── procedural / outcome memory (lessons, schema v10) ──────────────────────

def _lesson_record_to_row(r) -> dict[str, Any]:
    from pseudolife_memory.memory.cortex import _norm_key

    return {
        "entity": r.entity,
        "attribute": r.attribute,
        "entity_norm": _norm_key(r.entity),
        "attribute_norm": _norm_key(r.attribute),
        "value": r.value,
        "about": r.about,
        "polarity": r.polarity,
        "outcome": r.outcome,
        "status": r.status,
        "confidence": float(r.confidence),
        "origin": r.origin or None,
        "support": sorted(r.support),
        "provenance": sorted(r.provenance),
        "asserted_at": float(r.asserted_at),
        "last_confirmed": float(r.last_confirmed),
        "supersedes_value": r.supersedes_value,
        "superseded_by_value": r.superseded_by_value,
        "superseded_at": r.superseded_at,
        "embedding": r.embedding,
        "entity_id": None,          # linked by snapshot_lessons
        "object_entity_id": None,   # linked by snapshot_lessons
        **_stamp_to_row(r),
    }


def snapshot_lessons(lessons, storage) -> int:
    """Transactionally rewrite the lessons table from the in-memory store.

    Graph linking: the task-type (``entity``) resolves to its entity id, and the
    ``about`` tool/source — when it names a known entity/alias — to the object id,
    so the lesson row and the ``prefers``/``avoids`` graph edge stay joined.
    """
    from pseudolife_memory.graph import norm_name

    rows = [_lesson_record_to_row(r) for r in lessons.records]
    emap = storage.entity_id_map()
    for row in rows:
        row["entity_id"] = emap.get(norm_name(row["entity"]))
        if row.get("about"):
            row["object_entity_id"] = emap.get(norm_name(row["about"]))
    storage.replace_lessons(rows)
    return len(rows)


def hydrate_lessons(lessons, storage) -> int:
    """Fill the lesson store from the lessons table. Returns record count."""
    from pseudolife_memory.memory.lessons import LessonRecord

    lessons.records = []
    for row in storage.load_lessons():
        emb = row["embedding"]
        lessons.records.append(LessonRecord(
            entity=row["entity"],
            attribute=row["attribute"],
            value=row["value"],
            about=row["about"],
            polarity=row["polarity"],
            outcome=row["outcome"],
            confidence=row["confidence"],
            status=row["status"],
            origin=row["origin"],
            support=set(row["support"] or []),
            provenance=set(row["provenance"] or []),
            asserted_at=row["asserted_at"],
            last_confirmed=row["last_confirmed"],
            supersedes_value=row["supersedes_value"],
            superseded_by_value=row["superseded_by_value"],
            superseded_at=row["superseded_at"],
            embedding=(torch.as_tensor(emb, dtype=torch.float32)
                       if emb is not None else None),
            **_stamp_from_row(row),
        ))
    lessons._current = {
        r.key: i for i, r in enumerate(lessons.records) if r.status == "current"
    }
    return len(lessons.records)
