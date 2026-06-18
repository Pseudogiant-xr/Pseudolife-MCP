"""Sibling cortex store — slot-keyed canonical-fact layer (schema v7, additive).

The continuum (CMS / MIRAS bands) is the *hippocampus*: graded, decaying,
similarity-ranked, every turn an episode. This module is the *cortex*: a small
store of canonical facts keyed by an ``(entity, attribute)`` slot, where

* **identity, not similarity** — a fact dedups to its slot;
* **supersession, not decay** — a new value retires the old (kept for audit);
  facts never fade with disuse;
* **currency, not frequency** — ``lookup`` returns the one ``current`` value.

It deliberately reuses the existing :class:`pseudolife_memory.memory.slots.Slot`
``(entity, attribute, value, polarity)`` primitive as the key, and the
text-link supersession idiom already used across the codebase
(``superseded_by_text`` → here ``superseded_by_value``) rather than introducing
uuids.

This store is **not** a MIRAS band: it has no MLP, no promotion chain, and no
decay sweep, so "decay-exempt" is structural, not a guard. Embeddings are
supplied by the caller (dependency injection) so the store stays embedder-
agnostic and unit-testable without loading a sentence-transformer.

Phase 1 scope: the store + write/read/persist paths. The dream pass that
*populates* it (LLM/regex claim extraction over recent memories) lives in
``memory/dream.py`` as a pluggable extractor (regex floor → optional LLM).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import re
import time

import torch

from pseudolife_memory.memory.slots import Slot

SCHEMA_VERSION = 8

# Any run of separators (space . _ - /) is one boundary, so trivial naming
# variants collapse to ONE slot identity. Without this the dream extractor forks
# the same fact across NEBULA-SERPENT / nebula serpent / nebula_serpent / nebula.x.
_KEY_SEP_RE = re.compile(r"[\s._\-/]+")


def _norm_key(s: str) -> str:
    """Normalise an entity/attribute for slot identity: casefold + collapse every
    run of separators to a single hyphen. Identity only — the record keeps its
    original-case ``entity``/``attribute`` for display and embedding."""
    s = _KEY_SEP_RE.sub("-", (s or "").strip().casefold())
    return s.strip("-")


def _norm_value(s: str) -> str:
    """Normalise a value for equivalence testing."""
    return (s or "").strip().casefold()


# Provenance-of-kind: which tier asserted a fact. Precedence high → low. A fact
# the user stated outranks one the agent merely *did*, which outranks one the
# agent only *said*. ``origin`` returns the strongest tier in a record's
# ``support`` set, so a fact the agent guessed and the user later confirmed
# reports ``origin == "user"`` (corroboration), not ``"agent"``.
SUPPORT_PRECEDENCE = ("user", "action", "agent")


def _norm_support(s: str | None) -> str | None:
    s = (s or "").strip().casefold()
    return s if s in SUPPORT_PRECEDENCE else None


# Provenance tier rank for the supersession guard. A write may only SUPERSEDE a
# slot whose current value is backed by an equal-or-weaker tier; a weaker-tier
# write is parked as a contender instead of silently overwriting. Unknown/"" = 0.
_TIER_RANK = {"user": 3, "action": 2, "agent": 1}


def _rank(origin: str | None) -> int:
    return _TIER_RANK.get((origin or "").strip().casefold(), 0)


@dataclass
class CortexRecord:
    """One canonical fact at a slot, with lifecycle + provenance.

    ``status`` is ``current`` | ``superseded`` | ``retired``. Superseded records
    are never deleted — they are the audit trail / revert path. ``provenance``
    is the set of episode ids the claim was extracted or confirmed from.
    """

    entity: str
    attribute: str
    value: str
    polarity: str = "+"
    confidence: float = 0.7
    status: str = "current"
    provenance: set[str] = field(default_factory=set)
    asserted_at: float = 0.0
    last_confirmed: float = 0.0
    supersedes_value: str | None = None
    superseded_by_value: str | None = None
    superseded_at: float | None = None
    embedding: torch.Tensor | None = None
    # Value-free slot embedding (entity+attribute) for paraphrase-robust dream
    # slot resolution; None on legacy (pre-v8) records, lazily backfilled.
    slot_embedding: torch.Tensor | None = None
    # Tiers that have asserted/confirmed this fact: {"user","action","agent"}.
    support: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, str]:
        return (_norm_key(self.entity), _norm_key(self.attribute))

    @property
    def origin(self) -> str:
        """Strongest tier that backs this fact (user > action > agent), or ""."""
        for tier in SUPPORT_PRECEDENCE:
            if tier in self.support:
                return tier
        return ""


@dataclass
class WriteResult:
    """Outcome of :meth:`CortexStore.write_fact`."""

    action: str  # "inserted" | "confirmed" | "superseded" | "contested"
    record: CortexRecord


class CortexStore:
    """Slot-keyed canonical-fact store. Not a band; no decay; single current
    record per ``(entity, attribute)`` slot."""

    def __init__(
        self,
        supersede_confidence_margin: float = 0.15,
        reinforce_rate: float = 0.34,
        protect_provenance: bool = True,
    ) -> None:
        self.supersede_confidence_margin = float(supersede_confidence_margin)
        self.reinforce_rate = float(reinforce_rate)
        # When True (default), a conflicting write weaker than the slot's current
        # tier (or below the confidence margin) is parked as a contender rather
        # than superseding. False -> pure newer-wins (legacy behavior).
        self.protect_provenance = bool(protect_provenance)
        self.records: list[CortexRecord] = []
        # slot key -> index into ``records`` of the *current* record.
        self._current: dict[tuple[str, str], int] = {}
        # (old_value, new_value, entity, attribute, decision, reason,
        #  confidence_delta, timestamp) — instrumentation for §10.
        self.supersession_log: list[dict] = []
        # High-water timestamp of episodic turns already consolidated by the
        # dream pass. dream_pull returns turns newer than this; dream_commit
        # advances it. Persisted with the cortex so consolidation is once-only.
        self.dream_cursor: float = 0.0

    # ------------------------------------------------------------------
    # Write path — canonicalise → insert / confirm / supersede / contest
    # ------------------------------------------------------------------

    def write_fact(
        self,
        slot: Slot,
        embedding: torch.Tensor,
        *,
        confidence: float = 0.7,
        provenance: Iterable[str] = (),
        support: str | None = None,
        now: float | None = None,
        slot_embedding: torch.Tensor | None = None,
    ) -> WriteResult:
        t = time.time() if now is None else float(now)
        prov = {p for p in provenance if p}
        sup = _norm_support(support)
        emb = embedding.detach().to("cpu", torch.float32).clone()
        semb = (slot_embedding.detach().to("cpu", torch.float32).clone()
                if slot_embedding is not None else None)
        key = (_norm_key(slot.entity), _norm_key(slot.attribute))

        idx = self._current.get(key)
        if idx is None:
            return WriteResult("inserted", self._insert(slot, emb, confidence, prov, t, support=sup, slot_embedding=semb))

        cur = self.records[idx]
        if _norm_value(cur.value) == _norm_value(slot.value):
            # Same fact reasserted → confirm, never duplicate. Union the support
            # tier so corroboration (agent guess → user confirm) is recorded, and
            # let a higher-tier confirmation lift confidence past plain reinforce.
            cur.last_confirmed = t
            cur.provenance |= prov
            if sup:
                cur.support.add(sup)
            cur.confidence = min(1.0, max(self._reinforce(cur.confidence), float(confidence)))
            return WriteResult("confirmed", cur)

        # Genuine conflict at the same slot. Provenance guard: only a write whose
        # tier is >= the current value's tier may supersede; a weaker-tier write
        # (or one below the confidence margin) is parked as a contender instead of
        # silently overwriting. (Guard off -> tier ignored, pure newer-wins.)
        tier_ok = (not self.protect_provenance) or _rank(sup) >= _rank(cur.origin)
        if tier_ok and self._should_supersede(cur, confidence, t):
            cur.status = "superseded"
            cur.superseded_at = t
            cur.superseded_by_value = slot.value
            self._log(cur, slot.value, confidence, t, "supersede", "newer_wins")
            new = self._insert(slot, emb, confidence, prov, t, supersedes=cur.value, support=sup, slot_embedding=semb)
            return WriteResult("superseded", new)

        reason = "tier_downgrade" if not tier_ok else "below_confidence_margin"
        if not self.protect_provenance:
            # Legacy behavior: drop the conflicting value, keep current.
            self._log(cur, slot.value, confidence, t, "contested", reason)
            return WriteResult("contested", cur)
        return self._contend(cur, slot, emb, confidence, prov, t, sup, reason, semb)

    def _insert(
        self,
        slot: Slot,
        emb: torch.Tensor,
        confidence: float,
        prov: set[str],
        t: float,
        supersedes: str | None = None,
        support: str | None = None,
        slot_embedding: torch.Tensor | None = None,
    ) -> CortexRecord:
        rec = CortexRecord(
            entity=slot.entity,
            attribute=slot.attribute,
            value=slot.value,
            polarity=getattr(slot, "polarity", "+"),
            confidence=float(confidence),
            status="current",
            provenance=set(prov),
            asserted_at=t,
            last_confirmed=t,
            supersedes_value=supersedes,
            embedding=emb,
            slot_embedding=slot_embedding,
            support={support} if support else set(),
        )
        self.records.append(rec)
        self._current[rec.key] = len(self.records) - 1
        return rec

    def _reinforce(self, c: float) -> float:
        return min(1.0, c + (1.0 - c) * self.reinforce_rate)

    def _should_supersede(
        self, current: CortexRecord, candidate_conf: float, candidate_t: float,
    ) -> bool:
        # Newer wins, unless the candidate is materially less confident.
        if candidate_t < current.asserted_at:
            return False
        if candidate_conf < current.confidence - self.supersede_confidence_margin:
            return False
        return True

    def _log(self, cur, new_value, new_conf, t, decision, reason):
        self.supersession_log.append({
            "entity": cur.entity,
            "attribute": cur.attribute,
            "old_value": cur.value,
            "new_value": new_value,
            "decision": decision,
            "reason": reason,
            "confidence_delta": round(float(new_conf) - float(cur.confidence), 4),
            "timestamp": t,
        })

    # ------------------------------------------------------------------
    # Contenders — a conflicting write that may not supersede is parked here
    # ------------------------------------------------------------------

    def _active_contender(self, key: tuple[str, str]) -> "CortexRecord | None":
        """The one active (status='contested') contender at a slot, or None."""
        for r in self.records:
            if r.key == key and r.status == "contested":
                return r
        return None

    def contenders_for(self, entity: str, attribute: str) -> list["CortexRecord"]:
        """Active contenders at a slot (0 or 1 under the at-most-one invariant)."""
        key = (_norm_key(entity), _norm_key(attribute))
        return [r for r in self.records if r.key == key and r.status == "contested"]

    def _contend(self, cur, slot, emb, confidence, prov, t, sup, reason, slot_embedding=None):
        """Park a conflicting value as a contender at ``cur``'s slot rather than
        superseding. Keeps the current value canonical. At most one active
        contender per slot: a matching value confirms (reinforces) the existing
        contender; a different value supersedes the prior contender."""
        existing = self._active_contender(cur.key)
        if existing is not None and _norm_value(existing.value) == _norm_value(slot.value):
            existing.last_confirmed = t
            existing.provenance |= prov
            if sup:
                existing.support.add(sup)
            existing.confidence = min(
                1.0, max(self._reinforce(existing.confidence), float(confidence)),
            )
            self._log(cur, slot.value, confidence, t, "contested", "contender_confirmed")
            return WriteResult("contested", existing)
        supersedes_val = None
        if existing is not None:
            existing.status = "superseded"
            existing.superseded_at = t
            existing.superseded_by_value = slot.value
            supersedes_val = existing.value
        rec = CortexRecord(
            entity=slot.entity,
            attribute=slot.attribute,
            value=slot.value,
            polarity=getattr(slot, "polarity", "+"),
            confidence=float(confidence),
            status="contested",
            provenance=set(prov),
            asserted_at=t,
            last_confirmed=t,
            supersedes_value=supersedes_val,
            embedding=emb,
            slot_embedding=slot_embedding,
            support={sup} if sup else set(),
        )
        self.records.append(rec)   # deliberately NOT registered in self._current
        self._log(cur, slot.value, confidence, t, "contested", reason)
        return WriteResult("contested", rec)

    def resolve(self, entity, attribute, accept: bool, now: float | None = None):
        """Resolve the active contender at a slot. ``accept=True`` promotes it to
        current (old current -> superseded; contender stamped user-confirmed);
        ``accept=False`` retires it (current untouched). Returns a ``WriteResult``
        or ``None`` when there is no active contender."""
        key = (_norm_key(entity), _norm_key(attribute))
        t = time.time() if now is None else float(now)
        c_idx = next(
            (i for i, r in enumerate(self.records)
             if r.key == key and r.status == "contested"),
            None,
        )
        if c_idx is None:
            return None
        contender = self.records[c_idx]
        cur_idx = self._current.get(key)
        cur = self.records[cur_idx] if cur_idx is not None else None
        if accept:
            if cur is not None:
                cur.status = "superseded"
                cur.superseded_at = t
                cur.superseded_by_value = contender.value
            contender.status = "current"
            contender.support.add("user")
            contender.last_confirmed = t
            contender.supersedes_value = cur.value if cur is not None else contender.supersedes_value
            self._current[key] = c_idx
            self._log(cur or contender, contender.value, contender.confidence, t,
                      "resolved", "accepted")
            return WriteResult("superseded", contender)
        contender.status = "retired"
        contender.superseded_at = t
        self._log(cur or contender, contender.value, contender.confidence, t,
                  "resolved", "rejected")
        return WriteResult("contested", cur or contender)

    # ------------------------------------------------------------------
    # Read path — lookup (exact slot) + search (fuzzy, current only)
    # ------------------------------------------------------------------

    def lookup(self, entity: str, attribute: str) -> CortexRecord | None:
        idx = self._current.get((_norm_key(entity), _norm_key(attribute)))
        if idx is None:
            return None
        rec = self.records[idx]
        return rec if rec.status == "current" else None

    def records_for(self, entity: str, attribute: str) -> list[CortexRecord]:
        key = (_norm_key(entity), _norm_key(attribute))
        return [r for r in self.records if r.key == key]

    def current_records(self) -> list[CortexRecord]:
        """All ``current`` facts (insertion order) — for dump / introspection."""
        return [r for r in self.records if r.status == "current"]

    def vocab(self, limit: int = 120) -> list[str]:
        """Sorted, normalised ``entity.attribute`` slot keys currently in use —
        handed to the dream extractor so it REUSES existing keys instead of
        reinventing them (the other half of key-stability)."""
        keys = {
            "%s.%s" % (_norm_key(r.entity), _norm_key(r.attribute))
            for r in self.records if r.status == "current"
        }
        return sorted(keys)[: max(0, int(limit))]

    def forget(self, entity: str, attribute: str | None = None) -> int:
        """Hard-delete every record (current AND superseded) at an entity, or at
        one exact ``(entity, attribute)`` slot. Unlike supersession this leaves no
        audit trail — it is for purging test/garbage facts, not normal updates.
        Returns the number of records removed."""
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
            self._current = {}
            for i, r in enumerate(self.records):
                if r.status == "current":
                    self._current[r.key] = i
        return removed

    def search(
        self,
        query_embedding: torch.Tensor,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[CortexRecord, float]]:
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
            (rec, float(s)) for rec, s in zip(current, sims) if float(s) >= min_score
        ]
        scored.sort(key=lambda rs: rs[1], reverse=True)
        return scored[: max(0, int(top_k))]

    # ------------------------------------------------------------------
    # Persistence — co-located sibling of cms_state.pt; torch.save round-trip
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "version": SCHEMA_VERSION,
            "supersede_confidence_margin": self.supersede_confidence_margin,
            "reinforce_rate": self.reinforce_rate,
            "dream_cursor": self.dream_cursor,
            "supersession_log": self.supersession_log,
            "records": [
                {
                    "entity": r.entity,
                    "attribute": r.attribute,
                    "value": r.value,
                    "polarity": r.polarity,
                    "confidence": r.confidence,
                    "status": r.status,
                    "provenance": sorted(r.provenance),
                    "asserted_at": r.asserted_at,
                    "last_confirmed": r.last_confirmed,
                    "supersedes_value": r.supersedes_value,
                    "superseded_by_value": r.superseded_by_value,
                    "superseded_at": r.superseded_at,
                    "embedding": r.embedding,
                    "slot_embedding": r.slot_embedding,
                    "support": sorted(r.support),
                }
                for r in self.records
            ],
        }
        torch.save(state, str(path))

    def load(self, path: str | Path) -> None:
        path = Path(path)
        if not path.exists():
            return
        try:
            state = torch.load(str(path), weights_only=False)
        except TypeError:  # older torch without the kwarg
            state = torch.load(str(path))
        self.supersede_confidence_margin = state.get(
            "supersede_confidence_margin", self.supersede_confidence_margin,
        )
        self.reinforce_rate = state.get("reinforce_rate", self.reinforce_rate)
        self.dream_cursor = float(state.get("dream_cursor", 0.0))
        self.supersession_log = list(state.get("supersession_log", []))
        self.records = []
        self._current = {}
        for d in state.get("records", []):
            rec = CortexRecord(
                entity=d["entity"],
                attribute=d["attribute"],
                value=d["value"],
                polarity=d.get("polarity", "+"),
                confidence=d.get("confidence", 0.7),
                status=d.get("status", "current"),
                provenance=set(d.get("provenance", [])),
                asserted_at=d.get("asserted_at", 0.0),
                last_confirmed=d.get("last_confirmed", 0.0),
                supersedes_value=d.get("supersedes_value"),
                superseded_by_value=d.get("superseded_by_value"),
                superseded_at=d.get("superseded_at"),
                embedding=d.get("embedding"),
                slot_embedding=d.get("slot_embedding"),
                support=set(d.get("support", [])),
            )
            self.records.append(rec)
        self._reindex_current()

    def _reindex_current(self) -> None:
        """Rebuild the slot -> current index and self-heal the one-record-per-status
        invariants. If two records share a normalised slot at the same LIVE status
        (``current`` or ``contested``) — e.g. legacy facts written before key
        normalisation, like ``NEBULA-SERPENT`` vs ``nebula-serpent`` — keep the
        most-recently-confirmed and demote the rest to ``superseded``."""
        self._current = {}
        seen_contested: dict[tuple[str, str], int] = {}

        def _demote(keep: int, drop: int) -> None:
            loser = self.records[drop]
            loser.status = "superseded"
            if loser.superseded_at is None:
                loser.superseded_at = self.records[keep].last_confirmed
            loser.superseded_by_value = self.records[keep].value

        for i, rec in enumerate(self.records):
            if rec.status == "current":
                prev = self._current.get(rec.key)
                if prev is None:
                    self._current[rec.key] = i
                else:
                    keep, drop = ((i, prev) if rec.last_confirmed >= self.records[prev].last_confirmed
                                 else (prev, i))
                    _demote(keep, drop)
                    self._current[rec.key] = keep
            elif rec.status == "contested":
                prev = seen_contested.get(rec.key)
                if prev is None:
                    seen_contested[rec.key] = i
                else:
                    keep, drop = ((i, prev) if rec.last_confirmed >= self.records[prev].last_confirmed
                                 else (prev, i))
                    _demote(keep, drop)
                    seen_contested[rec.key] = keep

    def stats(self) -> dict:
        current = sum(1 for r in self.records if r.status == "current")
        superseded = sum(1 for r in self.records if r.status == "superseded")
        return {
            "total_records": len(self.records),
            "current": current,
            "superseded": superseded,
            "slots": len(self._current),
        }
