"""World-knowledge cortex — slot-keyed canonical store for *external* facts.

Sibling of the personal :class:`pseudolife_memory.memory.cortex.CortexStore`, kept
deliberately separate (its rows live in the ``world_facts`` table, not ``facts``) so
a runaway research ingest can be truncated without touching what the user/projects
taught the agent. It REUSES the cortex's slot-identity normalisation (``_norm_key`` /
``_norm_value``) so a world fact dedups to the same ``(entity, attribute)`` slot the
rest of the system uses — but the write semantics are simpler than the personal
cortex: every world fact is ``origin='source'`` (external-but-cited), so there is no
user/action/agent tier guard and no contender parking. A newer source simply
supersedes an older value at a slot.

Currency is enforced at READ time via :mod:`pseudolife_memory.memory.freshness`:
``effective_confidence`` decays a fact by its age + freshness class, and ``is_stale``
flags facts past 2×TTL. Embeddings are injected by the caller (embedder-agnostic,
unit-testable without a sentence-transformer).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import torch

from pseudolife_memory.memory import freshness
from pseudolife_memory.memory.cortex import _norm_key, _norm_value

SCHEMA_VERSION = 9
ORIGIN = "source"


@dataclass
class WorldRecord:
    """One canonical world fact at a slot, with citation + freshness.

    ``status`` is ``current`` | ``superseded`` (no contender state — world facts have
    a single provenance tier). Superseded records are kept for audit / revert.
    """

    entity: str
    attribute: str
    value: str
    polarity: str = "+"
    confidence: float = 0.7
    status: str = "current"
    # provenance / freshness (quote-not-page; spec 2026-06-13 D5)
    source_url: str = ""
    source_quote: str = ""
    freshness_class: str = "volatile"
    retrieved_at: float = 0.0
    content_hash: str | None = None
    source_doc_id: int | None = None
    asserted_at: float = 0.0
    last_confirmed: float = 0.0
    supersedes_value: str | None = None
    superseded_by_value: str | None = None
    superseded_at: float | None = None
    embedding: torch.Tensor | None = None
    # v11 writer-aware temporal stamp (see memory/hlc.py + the v0.4 design).
    tx_time: float | None = None
    valid_time: float | None = None
    hlc_phys: int | None = None
    hlc_logical: int | None = None
    writer_id: str | None = None
    session_id: str | None = None
    version: int = 1

    @property
    def key(self) -> tuple[str, str]:
        return (_norm_key(self.entity), _norm_key(self.attribute))

    @property
    def origin(self) -> str:
        return ORIGIN

    def effective_confidence(self, now: float | None = None) -> float:
        """Stored confidence scaled by age-based decay for its freshness class."""
        return freshness.effective_confidence(
            self.confidence, self.retrieved_at, self.freshness_class, now,
        )

    def is_stale(self, now: float | None = None) -> bool:
        return freshness.is_stale(self.freshness_class, self.retrieved_at, now)


class WorldCortexStore:
    """Slot-keyed world-fact store: one ``current`` record per ``(entity, attribute)``;
    newer source supersedes; reads expose age-decayed effective confidence."""

    def __init__(self) -> None:
        self.records: list[WorldRecord] = []
        self._current: dict[tuple[str, str], int] = {}

    # ── write ───────────────────────────────────────────────────────────
    def write_fact(
        self,
        entity: str,
        attribute: str,
        value: str,
        embedding: torch.Tensor | None = None,
        *,
        confidence: float = 0.7,
        source_url: str = "",
        source_quote: str = "",
        freshness_class: str = "volatile",
        retrieved_at: float | None = None,
        content_hash: str | None = None,
        source_doc_id: int | None = None,
        polarity: str = "+",
        now: float | None = None,
    ) -> tuple[str, WorldRecord]:
        t = time.time() if now is None else float(now)
        ra = t if retrieved_at is None else float(retrieved_at)
        fc = freshness.normalize_class(freshness_class)
        emb = (
            embedding.detach().to("cpu", torch.float32).clone()
            if embedding is not None else None
        )
        key = (_norm_key(entity), _norm_key(attribute))
        idx = self._current.get(key)

        if idx is None:
            return ("inserted", self._insert(
                entity, attribute, value, emb, confidence, source_url, source_quote,
                fc, ra, content_hash, source_doc_id, polarity, t))

        cur = self.records[idx]
        if _norm_value(cur.value) == _norm_value(value):
            # Same fact, re-sourced → confirm: refresh retrieval time + citation,
            # lift confidence, do not duplicate.
            cur.last_confirmed = t
            cur.retrieved_at = ra
            cur.freshness_class = fc
            cur.confidence = min(1.0, max(cur.confidence, float(confidence)))
            if source_url:
                cur.source_url = source_url
            if source_quote:
                cur.source_quote = source_quote
            if content_hash:
                cur.content_hash = content_hash
            if source_doc_id is not None:
                cur.source_doc_id = source_doc_id
            return ("confirmed", cur)

        # Different value → the newer source wins (no tier guard for world facts).
        cur.status = "superseded"
        cur.superseded_at = t
        cur.superseded_by_value = value
        new = self._insert(
            entity, attribute, value, emb, confidence, source_url, source_quote,
            fc, ra, content_hash, source_doc_id, polarity, t, supersedes=cur.value)
        return ("superseded", new)

    def _insert(self, entity, attribute, value, emb, confidence, source_url,
                source_quote, fc, ra, content_hash, source_doc_id, polarity, t,
                supersedes: str | None = None) -> WorldRecord:
        rec = WorldRecord(
            entity=entity, attribute=attribute, value=value, polarity=polarity,
            confidence=float(confidence), status="current",
            source_url=source_url, source_quote=source_quote, freshness_class=fc,
            retrieved_at=ra, content_hash=content_hash, source_doc_id=source_doc_id,
            asserted_at=t, last_confirmed=t, supersedes_value=supersedes, embedding=emb,
        )
        self.records.append(rec)
        self._current[rec.key] = len(self.records) - 1
        return rec

    # ── read ────────────────────────────────────────────────────────────
    def lookup(self, entity: str, attribute: str) -> WorldRecord | None:
        idx = self._current.get((_norm_key(entity), _norm_key(attribute)))
        if idx is None:
            return None
        rec = self.records[idx]
        return rec if rec.status == "current" else None

    def current_records(self) -> list[WorldRecord]:
        return [r for r in self.records if r.status == "current"]

    def search(
        self, query_embedding: torch.Tensor, top_k: int = 5, min_score: float = 0.0,
    ) -> list[tuple[WorldRecord, float]]:
        current = [r for r in self.records if r.status == "current" and r.embedding is not None]
        if not current:
            return []
        q = query_embedding.detach().to("cpu", torch.float32).reshape(-1)
        q = q / (q.norm() + 1e-12)
        mat = torch.stack([r.embedding.reshape(-1) for r in current])
        mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
        sims = (mat @ q).tolist()
        scored = [(r, float(s)) for r, s in zip(current, sims) if float(s) >= min_score]
        scored.sort(key=lambda rs: rs[1], reverse=True)
        return scored[: max(0, int(top_k))]

    def forget(self, entity: str, attribute: str | None = None) -> int:
        ne = _norm_key(entity)
        na = _norm_key(attribute) if attribute is not None else None
        keep, removed = [], 0
        for r in self.records:
            ke, ka = r.key
            if ke == ne and (na is None or ka == na):
                removed += 1
                continue
            keep.append(r)
        if removed:
            self.records = keep
            self._current = {
                r.key: i for i, r in enumerate(self.records) if r.status == "current"
            }
        return removed

    def stats(self) -> dict:
        cur = sum(1 for r in self.records if r.status == "current")
        return {
            "total_records": len(self.records),
            "current": cur,
            "superseded": len(self.records) - cur,
            "slots": len(self._current),
        }
