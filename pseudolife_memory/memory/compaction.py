"""Superseded-row compaction over the canonical stores (spec 2026-07-14).

Duck-typed over CortexStore / WorldCortexStore / LessonStore: each has a
flat ``records`` list, a ``_current`` slot index, and a ``dirty_slots``
set consumed by the per-slot PG sync (``replace_slot_*`` deletes every
row at a dirty slot and reinserts the in-memory survivors, so marking a
purged slot dirty IS the delete path — no separate SQL).

Policy (uniform across the three stores): per slot, pool the non-live
records (status ``superseded`` or ``retired``), keep the newest
``keep_per_slot`` unconditionally, and purge the rest only when their
``superseded_at`` is older than ``min_age_days``. ``current`` and
``contested`` records are never touched; entries and edges are out of
scope (bounded eviction / load-bearing tombstones — see the spec).
"""
from __future__ import annotations

import time

_NON_LIVE = ("superseded", "retired")


def compact_store(store, *, keep_per_slot: int, min_age_days: float,
                  now: float | None = None) -> int:
    """Purge old non-live records from ``store``. Returns the purge count."""
    keep = max(0, int(keep_per_slot))
    t = time.time() if now is None else float(now)
    cutoff = t - max(0.0, float(min_age_days)) * 86400.0

    # Pool non-live records per slot with their insertion ordinal — the
    # ordinal breaks timestamp ties deterministically (the slot-index lesson).
    pools: dict[tuple[str, str], list[tuple[float, float, int]]] = {}
    for i, r in enumerate(store.records):
        if r.status in _NON_LIVE:
            pools.setdefault(r.key, []).append(
                (r.superseded_at or 0.0, r.asserted_at, i))

    victims: set[int] = set()
    for pool in pools.values():
        pool.sort(reverse=True)               # newest first
        for sup_at, _asserted, idx in pool[keep:]:
            if sup_at < cutoff:
                victims.add(idx)

    if not victims:
        return 0

    purged_slots = {store.records[i].key for i in victims}
    store.records = [r for i, r in enumerate(store.records)
                     if i not in victims]
    # Rebuild the slot -> index map. CortexStore's own rebuild keeps its
    # duplicate-healing semantics; the world/lesson stores use the plain
    # comprehension their hydrators use.
    reindex = getattr(store, "_reindex_current", None)
    if callable(reindex):
        reindex()
    else:
        store._current = {r.key: i for i, r in enumerate(store.records)
                          if r.status == "current"}
    # The invalidation hook: the next per-slot sync rewrites these slots,
    # deleting the purged rows from PG.
    store.dirty_slots |= purged_slots
    return len(victims)
