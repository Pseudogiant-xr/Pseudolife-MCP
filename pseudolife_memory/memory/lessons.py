"""Procedural / outcome memory — slot-keyed store of *lessons* the agent learns
from its own work: what worked, what was a dead end, what got corrected.

Third sibling of the personal :class:`pseudolife_memory.memory.cortex.CortexStore`
and the :class:`pseudolife_memory.memory.world_cortex.WorldCortexStore`, kept
deliberately separate (its rows live in the ``lessons`` table) for blast-radius
isolation. It REUSES the cortex's slot-identity normalisation (``_norm_key`` /
``_norm_value``), but the slot is ``(task-type, aspect)`` rather than
``(entity, attribute)``, and each record carries an ``outcome``
(``success`` | ``failure`` | ``correction``) alongside ``polarity``
(``+`` do-this / ``-`` avoid-this dead end).

Write semantics are simple — like the world cortex, there is no provenance-tier
guard and no contender parking, because lessons are written by a single author:
the dream pass (spec 2026-06-20). A newer lesson at a slot supersedes the older
value; re-deriving the same lesson confirms it (merges provenance, lifts
confidence). Embeddings are injected by the caller (embedder-agnostic, so the
store is unit-testable without a sentence-transformer).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import torch

from pseudolife_memory.memory.cortex import _norm_key, _norm_value

SCHEMA_VERSION = 10

# Recognised outcome classes (the signal that produced the lesson).
OUTCOMES = ("success", "failure", "correction")


@dataclass
class LessonRecord:
    """One canonical lesson at a ``(task-type, aspect)`` slot.

    ``status`` is ``current`` | ``superseded`` (no contender state — a single
    author). Superseded records are kept for audit / revert. ``provenance`` is
    the set of episode + signal ids the lesson was synthesised from.
    """

    entity: str          # the task-type ("deploy engine to host")
    attribute: str       # the aspect ("approach", "pitfall", "tool-choice")
    value: str           # the actionable lesson text
    about: str | None = None  # the tool/source the lesson is about (edge object)
    polarity: str = "+"  # + do-this / - avoid (dead end)
    outcome: str = "success"  # success | failure | correction
    confidence: float = 0.7
    status: str = "current"
    origin: str | None = None
    support: set[str] = field(default_factory=set)
    provenance: set[str] = field(default_factory=set)
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
    def is_negative(self) -> bool:
        return self.polarity == "-"


class LessonStore:
    """Slot-keyed lesson store: one ``current`` record per ``(task-type, aspect)``;
    newer supersedes; reads expose the current lesson with its outcome/polarity."""

    def __init__(self) -> None:
        self.records: list[LessonRecord] = []
        self._current: dict[tuple[str, str], int] = {}

    # ── write ───────────────────────────────────────────────────────────
    def write_fact(
        self,
        entity: str,
        attribute: str,
        value: str,
        embedding: torch.Tensor | None = None,
        *,
        about: str | None = None,
        outcome: str = "success",
        polarity: str = "+",
        confidence: float = 0.7,
        origin: str | None = None,
        provenance: set[str] | list[str] | None = None,
        support: set[str] | list[str] | None = None,
        now: float | None = None,
        hlc: tuple[int, int] | None = None,
        tx_time: float | None = None,
        valid_time: float | None = None,
        writer_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[str, LessonRecord]:
        t = time.time() if now is None else float(now)
        txt = t if tx_time is None else float(tx_time)
        vt = txt if valid_time is None else float(valid_time)
        stamp = dict(hlc=hlc, tx_time=txt, valid_time=vt,
                     writer_id=writer_id, session_id=session_id)
        emb = (
            embedding.detach().to("cpu", torch.float32).clone()
            if embedding is not None else None
        )
        prov = set(provenance or [])
        supp = set(support or [])
        outcome = outcome if outcome in OUTCOMES else "success"
        key = (_norm_key(entity), _norm_key(attribute))
        idx = self._current.get(key)

        if idx is None:
            return ("inserted", self._insert(
                entity, attribute, value, about, emb, outcome, polarity,
                confidence, origin, prov, supp, t, **stamp))

        cur = self.records[idx]
        if _norm_value(cur.value) == _norm_value(value):
            # Same lesson, re-derived → confirm: refresh, lift confidence, merge
            # provenance/support, and adopt the latest outcome/polarity framing.
            cur.last_confirmed = t
            cur.confidence = min(1.0, max(cur.confidence, float(confidence)))
            cur.provenance |= prov
            cur.support |= supp
            cur.outcome = outcome
            cur.polarity = polarity
            if about:
                cur.about = about
            if origin:
                cur.origin = origin
            if emb is not None:
                cur.embedding = emb
            # Re-deriving advances tx_time + last writer; valid_time is preserved.
            cur.tx_time = txt
            if hlc is not None:
                cur.hlc_phys, cur.hlc_logical = hlc
            if writer_id:
                cur.writer_id = writer_id
            if session_id:
                cur.session_id = session_id
            return ("confirmed", cur)

        # Different value → the newer lesson wins (single author, no tier guard,
        # but HLC is the ordering authority — an out-of-order write with an older
        # hlc than the current record must not clobber it, mirroring the personal
        # cortex's _should_supersede.
        #
        # Reachability: under the shipped single-daemon writer this is DORMANT
        # defensive code — every write is stamped with a fresh, monotonic
        # `self._hlc.tick()`, so a later write always has a >= hlc and the gate
        # never fires. (Note valid_time is bitemporal DISPLAY metadata and does
        # NOT enter this comparison; only hlc, then wall-clock `t` on an hlc tie.)
        # It becomes live under the future multi-writer occ path, where two
        # writers can present out-of-order stamps — same status as the cortex's
        # own gate.
        cur_hlc = (cur.hlc_phys or 0, cur.hlc_logical or 0)
        cand_hlc = hlc or (0, 0)
        if cand_hlc < cur_hlc:
            return ("stale", cur)
        if cand_hlc == cur_hlc and t < cur.asserted_at:
            return ("stale", cur)

        cur.status = "superseded"
        cur.superseded_at = t
        cur.superseded_by_value = value
        new = self._insert(
            entity, attribute, value, about, emb, outcome, polarity, confidence,
            origin, prov, supp, t, supersedes=cur.value, **stamp)
        return ("superseded", new)

    def _insert(self, entity, attribute, value, about, emb, outcome, polarity,
                confidence, origin, provenance, support, t,
                supersedes: str | None = None,
                hlc: tuple[int, int] | None = None,
                tx_time: float | None = None, valid_time: float | None = None,
                writer_id: str | None = None,
                session_id: str | None = None) -> LessonRecord:
        rec = LessonRecord(
            entity=entity, attribute=attribute, value=value, about=about,
            polarity=polarity, outcome=outcome, confidence=float(confidence),
            status="current", origin=origin, support=set(support),
            provenance=set(provenance), asserted_at=t, last_confirmed=t,
            supersedes_value=supersedes, embedding=emb,
            tx_time=tx_time, valid_time=valid_time,
            hlc_phys=(hlc[0] if hlc else None),
            hlc_logical=(hlc[1] if hlc else None),
            writer_id=writer_id, session_id=session_id,
        )
        self.records.append(rec)
        self._current[rec.key] = len(self.records) - 1
        return rec

    # ── read ────────────────────────────────────────────────────────────
    def lookup(self, entity: str, attribute: str) -> LessonRecord | None:
        idx = self._current.get((_norm_key(entity), _norm_key(attribute)))
        if idx is None:
            return None
        rec = self.records[idx]
        return rec if rec.status == "current" else None

    def current_records(self) -> list[LessonRecord]:
        return [r for r in self.records if r.status == "current"]

    def search(
        self, query_embedding: torch.Tensor, top_k: int = 5, min_score: float = 0.0,
    ) -> list[tuple[LessonRecord, float]]:
        current = [
            r for r in self.records
            if r.status == "current" and r.embedding is not None
        ]
        if not current:
            return []
        q = query_embedding.detach().to("cpu", torch.float32).reshape(-1)
        q = q / (q.norm() + 1e-12)
        mat = torch.stack([r.embedding.reshape(-1) for r in current])
        mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
        sims = (mat @ q).tolist()
        scored = [
            (r, float(s)) for r, s in zip(current, sims) if float(s) >= min_score
        ]
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
        neg = sum(1 for r in self.records if r.status == "current" and r.is_negative)
        return {
            "total_records": len(self.records),
            "current": cur,
            "superseded": len(self.records) - cur,
            "negative": neg,
            "slots": len(self._current),
        }
