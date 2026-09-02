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

from pseudolife_memory.memory import freshness
from pseudolife_memory.memory.labels import INHERIT
from pseudolife_memory.memory.slots import Slot

# Version of the file-mode cortex *snapshot* format (``cortex_state.pt``) — bumped
# only when that on-disk layout changes. NOT the Postgres bank schema version
# (that is ``storage.schema.SCHEMA_META_VERSION``, a separate and much larger
# number). The two are independent; don't conflate them.
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


def _norm_freshness(c: str | None) -> str:
    """Like :func:`freshness.normalize_class`, but unknown falls back to
    *evergreen* rather than *volatile*.

    The shared helper's fallback is right for world facts, which rot by
    default, and wrong here: a typo'd or unrecognised class on a personal
    fact must not quietly start it decaying (schema v23)."""
    c = (c or "").strip().casefold()
    return c if c in freshness.FRESHNESS_CLASSES else "evergreen"


def _norm_value(s: str) -> str:
    """Normalise a value for equivalence testing."""
    return (s or "").strip().casefold()


_VALUE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# Fraction of a shorter value's tokens that must already appear in the
# standing value for the write to count as a re-statement (echo) rather than
# a conflict. Tuned on the 2026-08-05 contested-slot audit: all four dream
# echoes clear it, all three genuine conflicts fall below it.
_ECHO_CONTAINMENT = 0.75
# A negated value is never judged by token containment: "knob not retired"
# is built entirely from tokens of "... knob retired ... not yet deployed"
# yet inverts its meaning, and dropping the "not" from a negated standing
# value ("not deployed" -> "deployed") inverts it just as silently. Either
# side carrying a negator routes the write down the normal conflict path
# (contender), which is only ever the pre-echo behavior.
_ECHO_NEGATORS = frozenset({"not", "no", "never", "none", "without"})
# A one- or two-token value ("healthy", "v5 deployed") contains too little
# signal for containment to distinguish echo from update — such writes also
# keep the conflict path.
_ECHO_MIN_TOKENS = 3


def _is_compression_echo(new_value: str, cur_value: str) -> bool:
    """True when ``new_value`` is a strict compression of ``cur_value`` — a
    shorter re-statement whose tokens are (almost) all already present in the
    standing value. The dream re-extracting a slot from the same status entry
    produces exactly this shape, and parking it as a contender manufactures a
    conflict where there is none. Disqualified outright: a novel digit-bearing
    token (a changed number, version, or id), a negator on either side, or a
    new value of fewer than ``_ECHO_MIN_TOKENS`` tokens."""
    ns, cs = _norm_value(new_value), _norm_value(cur_value)
    if not ns or len(ns) >= len(cs):
        return False
    nt = set(_VALUE_TOKEN_RE.findall(ns))
    if len(nt) < _ECHO_MIN_TOKENS:
        return False
    ct = set(_VALUE_TOKEN_RE.findall(cs))
    if (nt | ct) & _ECHO_NEGATORS:
        return False
    if any(any(ch.isdigit() for ch in tok) for tok in (nt - ct)):
        return False
    return len(nt & ct) / len(nt) >= _ECHO_CONTAINMENT


# Number-led values ("32", "27 species", "$1,500") — the class the C2-op gate
# measured being destroyed by scalar->set conversion (evals/results/
# c2op-gate-verdict.json). Currency sign then optional sign then a digit.
_AGGREGATE_VALUE_RE = re.compile(r"^[$€£]?[+-]?\d")


def _is_aggregate_value(value: str) -> bool:
    """True when a scalar value reads as a number-led quantity."""
    return bool(_AGGREGATE_VALUE_RE.match((value or "").strip()))


# Provenance-of-kind: which tier asserted a fact. Precedence high → low. A fact
# the user stated outranks one the agent merely *did*, which outranks one the
# agent only *said*. ``origin`` returns the strongest tier in a record's
# ``support`` set, so a fact the agent guessed and the user later confirmed
# reports ``origin == "user"`` (corroboration), not ``"agent"``.
SUPPORT_PRECEDENCE = ("user", "action", "agent")

# In-RAM cap on the supersession audit log. Persistence already stores only
# the newest 200 (storage/sync.py); without this in-place trim the list grew
# for the daemon's whole uptime — same growth class as superseded rows.
SUPERSESSION_LOG_CAP = 200

# Set-valued slots (v1, schema v26). A slot holds either one scalar current
# record OR many "member" current records — never both; a scalar occupying a
# slot converts one-way to a member the first time add_member targets it.
# MEMBER_DEDUP_COSINE: a member add whose normalised value doesn't exact-match
# an existing member still confirms (not duplicates) when its embedding is
# this close to an existing member's — same paraphrase-collapse idea as
# dedup_siblings, at member granularity.
MEMBER_DEDUP_COSINE = 0.9
# MAX_CURRENT_MEMBERS: hard cap on live members per slot — an unbounded set
# slot is an unbounded-growth foot-gun (dream extraction retried against a
# noisy transcript could mint hundreds of near-duplicate "tags"). Beyond the
# cap, further adds are dropped (action "member_capped"), not queued.
MAX_CURRENT_MEMBERS = 100


def compose_set_value(
    member_values: list[str], ranked: list[tuple[int, float]],
) -> tuple[str, float | None]:
    """Compose the one-entry-per-slot serving line for a set-valued slot
    (Task 6). ``member_values`` is the FULL current membership of the slot,
    in insertion order; ``ranked`` pairs an index into that list with the
    score the member earned wherever it individually ranked (dense search,
    or dense+BM25 fusion). Members that ranked come first, score-descending;
    any current member that did not individually rank is appended after —
    the slot still surfaces its whole membership, not just the fraction that
    happened to score above the caller's floor.

    Returns ``(value_string, max_score)`` — ``value_string`` is
    ``"m1; m2; m3 (3 members)"``; ``max_score`` is the highest score among
    ``ranked`` (``None`` if ``ranked`` is empty, which should not happen in
    practice since grouping only starts once at least one member ranks).

    Shared verbatim by ``service.cortex_search`` (live path, ``CortexRecord``
    objects) and ``evals/rebuild_contexts.rebuild_fact_lines`` (offline
    replay over a dumped bank's dicts) so the composed line is identical
    between the two by construction, not by two independent
    implementations staying in sync — the failure mode
    ``tests/test_cortex_bm25.py::test_rebuild_fact_ranking_matches_service_fusion``
    exists to catch."""
    ranked_sorted = sorted(ranked, key=lambda p: p[1], reverse=True)
    ranked_idx = [i for i, _ in ranked_sorted]
    ranked_set = set(ranked_idx)
    ordered = [member_values[i] for i in ranked_idx] + [
        v for i, v in enumerate(member_values) if i not in ranked_set]
    value = "; ".join(ordered) + f" ({len(member_values)} members)"
    score = ranked_sorted[0][1] if ranked_sorted else None
    return value, score


def _norm_support(s: str | None) -> str | None:
    s = (s or "").strip().casefold()
    return s if s in SUPPORT_PRECEDENCE else None


def _pick(label, rec, field_name: str):
    """v35 label resolution: ``INHERIT`` takes the label of the record the
    write lands on (``None`` when there is nothing to inherit from); an
    explicit value — including ``None``, which clears — wins. This is the
    one place the inherit-unless-restated rule lives for facts."""
    if label is INHERIT:
        return getattr(rec, field_name, None) if rec is not None else None
    return label


# Provenance tier rank for the supersession guard. A write may only SUPERSEDE a
# slot whose current value is backed by an equal-or-weaker tier; a weaker-tier
# write is parked as a contender instead of silently overwriting. Unknown/"" = 0.
_TIER_RANK = {"user": 3, "action": 2, "agent": 1}


def _rank(origin: str | None) -> int:
    return _TIER_RANK.get((origin or "").strip().casefold(), 0)


@dataclass
class CortexRecord:
    """One canonical fact at a slot, with lifecycle + provenance.

    ``status`` is ``current`` | ``superseded`` | ``retired`` | ``contested`` |
    ``removed``. Superseded/removed records are never deleted — they are the
    audit trail / revert path. ``removed`` is member-only (schema v26): a
    member the user retracted, timestamped via the existing
    ``superseded_at`` field (no separate "removed_at"). ``provenance`` is the
    set of episode ids the claim was extracted or confirmed from.

    ``kind`` distinguishes the two slot models sharing this dataclass:
    ``"scalar"`` (the original one-current-record-per-slot fact) or
    ``"member"`` (one of possibly many current records at a set-valued slot,
    see :meth:`CortexStore.add_member`).
    """

    entity: str
    attribute: str
    value: str
    polarity: str = "+"
    confidence: float = 0.7
    status: str = "current"
    kind: str = "scalar"  # "scalar" | "member"
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
    # v11 writer-aware temporal stamp. (hlc_phys, hlc_logical) is the ordering
    # authority (see memory/hlc.py); tx_time is wall-clock display; valid_time is
    # event time; writer_id/session_id record who wrote this version; version is
    # the OCC counter (dormant until write_mode='occ'). All default to legacy/None.
    tx_time: float | None = None
    valid_time: float | None = None
    hlc_phys: int | None = None
    hlc_logical: int | None = None
    writer_id: str | None = None
    session_id: str | None = None
    version: int = 1
    # v23 read-time currency, same curve as the world cortex. Default
    # ``evergreen`` — deliberately NOT the world cortex's ``volatile`` —
    # because personal facts are mostly durable; defaulting to volatile would
    # silently re-rank an existing bank. Set ``volatile`` on facts about
    # transient state (deployment status, what is "currently" running) so they
    # lose trust as they age instead of reading as gospel forever.
    freshness_class: str = "evergreen"
    # v29 epistemic stance: the source's own hedge words ("probably",
    # "per the runbook"), kept verbatim and separate from ``value``. None =
    # asserted plainly. Follows the LATEST asserting write: a confirm or
    # supersede without a stance clears it. Reader metadata only — never an
    # input to confidence, ranking, or supersession (it is model-emitted and
    # steerable by note text, the same trust class as a claim's ``origin``).
    stance: str | None = None
    # v35 write-time label pair (memory/labels.py). ``authority`` is the
    # speech act of the SOURCE ("directive" | "observation" | "quoted";
    # None = observation), ``distortion_tolerance`` the fidelity class
    # ("constraint" ... "episodic"; None = unlabelled). Unlike stance
    # they follow inherit-unless-restated: a superseding or confirming
    # write keeps the slot's label unless it passes one (None clears).
    # ``distortion_tolerance == "constraint"`` is a RANKING input — the
    # one deliberate exception to the stance rule — because pinning it
    # ahead of cosine is the whole point (TypeRetrieve); neither label
    # ever touches confidence or supersession routing.
    authority: str | None = None
    distortion_tolerance: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (_norm_key(self.entity), _norm_key(self.attribute))

    def effective_confidence(self, now: float | None = None) -> float:
        """Stored confidence scaled by age decay for this fact's class.

        Anchored on ``last_confirmed``, not ``asserted_at``: re-confirming a
        long-standing fact should restore its trust, otherwise something still
        true reads as rotten purely for having been written a while ago.
        """
        return freshness.effective_confidence(
            self.confidence, self.last_confirmed or self.asserted_at,
            self.freshness_class, now,
        )

    def is_stale(self, now: float | None = None) -> bool:
        """True past 2xTTL — a lead to re-verify, not truth. Never for evergreen."""
        return freshness.is_stale(
            self.freshness_class, self.last_confirmed or self.asserted_at, now,
        )

    @property
    def origin(self) -> str:
        """Strongest tier that backs this fact (user > action > agent), or ""."""
        for tier in SUPPORT_PRECEDENCE:
            if tier in self.support:
                return tier
        return ""


@dataclass
class WriteResult:
    """Outcome of :meth:`CortexStore.write_fact` / the member-model writes."""

    action: str
    # write_fact: "inserted" | "confirmed" | "superseded" | "contested"
    # add_member: "member_added" | "member_confirmed" | "member_capped" |
    #             "member_invalid" | "contested" (aggregate-conversion guard
    #             parked the add as a contender) | "confirmed" (the add
    #             equalled the protected aggregate scalar)
    # remove_member: "member_removed" | "member_not_found"
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
        # slot key -> indices into ``records`` of the *current* member rows
        # (insertion order), for set-valued slots. A key never appears in
        # both ``_current`` and ``_members`` at once: ``add_member``
        # converts a scalar occupying the slot to a member (one-way), and
        # ``write_fact`` actively enforces the reverse — it raises
        # ValueError when ``self._members.get(key)`` is non-empty, rather
        # than silently inserting a parallel current scalar.
        self._members: dict[tuple[str, str], list[int]] = {}
        # Slots mutated since the last storage sync (2026-07-02 P1 per-slot
        # persistence). Every mutation path MUST mark the slot(s) it touches;
        # sync_cortex_slots persists exactly these and clears the set on
        # success. meta_dirty covers the supersession log + dream cursor.
        self.dirty_slots: set[tuple[str, str]] = set()
        self.meta_dirty: bool = False
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
        hlc: tuple[int, int] | None = None,
        tx_time: float | None = None,
        valid_time: float | None = None,
        writer_id: str | None = None,
        session_id: str | None = None,
        freshness_class: str = "evergreen",
        force_contend: bool = False,
        stance: str | None = None,
        authority=INHERIT,
        distortion_tolerance=INHERIT,
    ) -> WriteResult:
        """Write a scalar fact at ``(slot.entity, slot.attribute)`` — the
        canonical insert/confirm/supersede/contest path (see the module
        docstring).

        ``stance`` (v29) follows the latest asserting write on every
        outcome — insert, confirm, supersede, and contend all stamp the
        written/confirmed record with this call's stance, where ``None``
        means "asserted plainly" and clears any stored hedge. It is never
        consulted by the routing logic below.

        ``authority`` / ``distortion_tolerance`` (v35) follow
        inherit-unless-restated instead: the default ``INHERIT`` keeps
        whatever the record the write lands on already carries (the
        current on confirm/supersede, the active contender on a
        contender-confirm, the current's label on a fresh park), a
        string restates it, and an explicit ``None`` clears it. Neither
        is consulted by the routing logic below either.

        ``force_contend`` (consolidation quarantine, spec 2026-08-09):
        a conflicting or brand-new value is parked as a contender
        regardless of tier — including at an EMPTY slot, where it parks
        currentless (``resolve(accept=True)`` promotes it to first
        current). A value matching the standing current still confirms:
        corroborating the canonical value is never a conflict.

        Raises ``ValueError`` if the slot currently holds set members (a
        prior :meth:`add_member` populated or converted it): scalar writes
        and set membership are mutually exclusive at one slot, and this
        store does not silently pick a resolution. Callers — the service
        layer, in particular — are expected to catch this and route the
        write through :meth:`add_member`/:meth:`remove_member` instead.
        """
        key = (_norm_key(slot.entity), _norm_key(slot.attribute))
        if self._members.get(key):
            raise ValueError(
                "slot holds a set; use add_member/remove_member — scalar "
                "writes are rejected"
            )
        t = time.time() if now is None else float(now)
        txt = t if tx_time is None else float(tx_time)
        vt = txt if valid_time is None else float(valid_time)
        stamp = dict(hlc=hlc, tx_time=txt, valid_time=vt,
                     writer_id=writer_id, session_id=session_id)
        prov = {p for p in provenance if p}
        sup = _norm_support(support)
        lab = dict(authority=authority, distortion_tolerance=distortion_tolerance)
        emb = embedding.detach().to("cpu", torch.float32).clone()
        semb = (slot_embedding.detach().to("cpu", torch.float32).clone()
                if slot_embedding is not None else None)
        self.dirty_slots.add(key)

        idx = self._current.get(key)
        if idx is None:
            if force_contend:
                # Quarantine: even a brand-new slot's first value must not
                # take current — park it currentless.
                return self._contend(None, slot, emb, confidence, prov, t,
                                     sup, "quarantine_low_trust", semb,
                                     freshness_class=freshness_class,
                                     stance=stance, **lab, **stamp)
            return WriteResult("inserted", self._insert(
                slot, emb, confidence, prov, t, support=sup,
                slot_embedding=semb, freshness_class=freshness_class,
                stance=stance, **lab, **stamp))

        cur = self.records[idx]
        if _norm_value(cur.value) == _norm_value(slot.value):
            # Same fact reasserted → confirm, never duplicate.
            return self._confirm(cur, prov, sup, confidence, t, txt, hlc,
                                 writer_id, session_id, semb, stance=stance, **lab)

        if _is_compression_echo(slot.value, cur.value):
            # A shorter re-statement of the standing value (dream echo) is
            # corroboration, not conflict: confirm the richer current value
            # instead of parking the compression as a contender.
            self._log(cur, slot.value, confidence, t, "confirm",
                      "compression_echo", writer_id=writer_id,
                      session_id=session_id)
            return self._confirm(cur, prov, sup, confidence, t, txt, hlc,
                                 writer_id, session_id, semb, stance=stance, **lab)

        if force_contend:
            # Quarantine: park unconditionally — tier is not consulted,
            # because the whole point is that a low-trust write must not
            # win the slot on tier arithmetic.
            return self._contend(cur, slot, emb, confidence, prov, t, sup,
                                 "quarantine_low_trust", semb,
                                 freshness_class=freshness_class,
                                 stance=stance, **lab, **stamp)

        # Genuine conflict at the same slot. Provenance guard: only a write whose
        # tier is >= the current value's tier may supersede; a weaker-tier write
        # (or one below the confidence margin) is parked as a contender instead of
        # silently overwriting. (Guard off -> tier ignored, pure newer-wins.)
        tier_ok = (not self.protect_provenance) or _rank(sup) >= _rank(cur.origin)
        if tier_ok and self._should_supersede(cur, confidence, hlc, t):
            cur.status = "superseded"
            cur.superseded_at = t
            cur.superseded_by_value = slot.value
            self._log(cur, slot.value, confidence, t, "supersede", "newer_wins",
                      writer_id=writer_id, session_id=session_id)
            new = self._insert(slot, emb, confidence, prov, t, supersedes=cur.value,
                               inherit_from=cur, support=sup, slot_embedding=semb,
                               freshness_class=freshness_class,
                               stance=stance, **lab, **stamp)
            return WriteResult("superseded", new)

        reason = "tier_downgrade" if not tier_ok else "below_confidence_margin"
        if not self.protect_provenance:
            # Legacy behavior: drop the conflicting value, keep current.
            self._log(cur, slot.value, confidence, t, "contested", reason,
                      writer_id=writer_id, session_id=session_id)
            return WriteResult("contested", cur)
        return self._contend(cur, slot, emb, confidence, prov, t, sup, reason, semb,
                             freshness_class=freshness_class,
                             stance=stance, **lab, **stamp)

    def _confirm(self, cur, prov, sup, confidence, t, txt, hlc,
                 writer_id, session_id, semb,
                 stance: str | None = None, authority=INHERIT,
                 distortion_tolerance=INHERIT) -> WriteResult:
        """Confirm the standing record: union support tier so corroboration
        (agent guess → user confirm) is recorded, and let a higher-tier
        confirmation lift confidence past plain reinforce."""
        # v29: the latest asserting write owns the stance — a plain
        # restatement (stance=None) clears a stored hedge.
        cur.stance = stance
        # v35: inherit-unless-restated (see _pick).
        cur.authority = _pick(authority, cur, "authority")
        cur.distortion_tolerance = _pick(
            distortion_tolerance, cur, "distortion_tolerance")
        cur.last_confirmed = t
        cur.provenance |= prov
        if sup:
            cur.support.add(sup)
        cur.confidence = min(1.0, max(self._reinforce(cur.confidence), float(confidence)))
        # Backfill slot_embedding on first confirmation via a caller that supplies
        # one — covers records auto-promoted (pre-v8) without a slot embedding.
        if semb is not None and cur.slot_embedding is None:
            cur.slot_embedding = semb
        # Re-confirmation advances the ordering clock + last toucher; tx_time
        # tracks the latest touch. valid_time (when it first became true) is
        # NOT moved — re-asserting the same value doesn't change when it held.
        cur.tx_time = txt
        if hlc is not None:
            cur.hlc_phys, cur.hlc_logical = hlc
        if writer_id:
            cur.writer_id = writer_id
        if session_id:
            cur.session_id = session_id
        return WriteResult("confirmed", cur)

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
        hlc: tuple[int, int] | None = None,
        tx_time: float | None = None,
        valid_time: float | None = None,
        writer_id: str | None = None,
        session_id: str | None = None,
        freshness_class: str = "evergreen",
        stance: str | None = None,
        authority=INHERIT,
        distortion_tolerance=INHERIT,
        inherit_from: "CortexRecord | None" = None,
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
            tx_time=tx_time,
            valid_time=valid_time,
            hlc_phys=(hlc[0] if hlc else None),
            hlc_logical=(hlc[1] if hlc else None),
            writer_id=writer_id,
            session_id=session_id,
            freshness_class=_norm_freshness(freshness_class),
            stance=stance,
            authority=_pick(authority, inherit_from, "authority"),
            distortion_tolerance=_pick(
                distortion_tolerance, inherit_from, "distortion_tolerance"),
        )
        self.records.append(rec)
        self._current[rec.key] = len(self.records) - 1
        return rec

    def _reinforce(self, c: float) -> float:
        return min(1.0, c + (1.0 - c) * self.reinforce_rate)

    def _should_supersede(
        self, current: CortexRecord, candidate_conf: float,
        candidate_hlc: tuple[int, int] | None, candidate_t: float,
    ) -> bool:
        # HLC is the ordering authority (immune to wall-clock steps). Fall back to
        # wall-clock only when neither side carries an HLC (legacy records).
        cur_hlc = (current.hlc_phys or 0, current.hlc_logical or 0)
        cand_hlc = candidate_hlc or (0, 0)
        if cand_hlc < cur_hlc:
            return False                      # strictly-earlier HLC never wins
        if cand_hlc == cur_hlc and candidate_t < current.asserted_at:
            return False                      # legacy/no-HLC tiebreak (old behaviour)
        if candidate_conf < current.confidence - self.supersede_confidence_margin:
            return False                      # materially less confident
        return True

    def _log(self, cur, new_value, new_conf, t, decision, reason,
             writer_id=None, session_id=None):
        self.meta_dirty = True
        self.supersession_log.append({
            "entity": cur.entity,
            "attribute": cur.attribute,
            "old_value": cur.value,
            "new_value": new_value,
            "decision": decision,
            "reason": reason,
            "confidence_delta": round(float(new_conf) - float(cur.confidence), 4),
            "timestamp": t,
            # Who made the change (v0.4 writer keying) — None on legacy/no-context.
            "writer_id": writer_id,
            "session_id": session_id,
        })
        if len(self.supersession_log) > SUPERSESSION_LOG_CAP:
            del self.supersession_log[:-SUPERSESSION_LOG_CAP]

    # ------------------------------------------------------------------
    # Set-valued slots — member add/remove/read (schema v26)
    # ------------------------------------------------------------------

    def add_member(
        self,
        slot: Slot,
        embedding: torch.Tensor,
        *,
        confidence: float = 0.7,
        provenance: Iterable[str] = (),
        support: str | None = None,
        now: float | None = None,
        hlc: tuple[int, int] | None = None,
        writer_id: str | None = None,
        session_id: str | None = None,
    ) -> WriteResult:
        """Add (or confirm) a member of the set-valued slot ``(slot.entity,
        slot.attribute)``.

        Unlike :meth:`write_fact`, members are never contested; conflicting
        adds either confirm an existing member or insert a new one — there is
        no contender path for members (v1 decision; a differing value at an
        already-populated set slot is simply a second current member, not a
        dispute to resolve). Dedup is exact normalised-value match OR cosine
        similarity >= ``MEMBER_DEDUP_COSINE`` against an existing current
        member. A scalar record already occupying the slot is converted
        one-way to a member first (the scalar row survives as an
        audit-visible superseded record; there is no path back to scalar).
        The conversion does NOT carry a non-evergreen scalar's
        ``freshness_class`` onto the member — set members are structurally
        evergreen (see :meth:`_insert_member`) — and stamps the drop on the
        conversion's supersession-log entry as ``dropped_freshness_class``.
        Exception: when the current scalar is a number-led aggregate value
        (``_is_aggregate_value``), the slot is NOT converted; the incoming
        member is parked as a contender (reason
        ``member_add_blocked_aggregate``) and the total stays canonical.
        ``resolve(accept=True)`` remains the explicit path to overwrite it.
        If the incoming value is the SAME as the current scalar (normalised),
        it confirms the scalar instead (``"confirmed"``, mirroring
        :meth:`write_fact`'s own confirm branch — confidence/provenance/
        support only; tx_time/hlc/writer are not advanced) rather than
        parking a contender identical to itself.
        Beyond ``MAX_CURRENT_MEMBERS`` current members, further adds are
        dropped (``"member_capped"``).

        A value that normalises to empty is rejected outright
        (``"member_invalid"``) rather than stored: Postgres unique indexes
        treat NULLs as distinct, so a member row with an empty/NULL
        normalised value would silently bypass the per-slot uniqueness
        constraint on persistence (Task 1 review finding).

        ``slot.polarity`` (e.g. ``"-"`` for a negated add) is accepted and
        preserved verbatim on the inserted/converted member in v1 — it is
        NOT interpreted. Routing a negated add to an implicit
        :meth:`remove_member` call is explicitly out of scope here; callers
        that mean "no longer" must call ``remove_member`` themselves.
        """
        t = time.time() if now is None else float(now)
        key = (_norm_key(slot.entity), _norm_key(slot.attribute))
        if not _norm_value(slot.value):
            # Rejected before touching any state — no dirty_slots write, no
            # slot rewrite scheduled for an add that never happened.
            return WriteResult("member_invalid", CortexRecord(
                entity=slot.entity, attribute=slot.attribute, value=slot.value,
                kind="member",
            ))
        emb = embedding.detach().to("cpu", torch.float32).clone()
        # Scalar at this slot -> one-way conversion (spec rule 1), UNLESS the
        # scalar is a number-led aggregate ("total species: 32"): converting
        # destroys a stated total that no enumeration of members recovers
        # (measured: evals/results/c2op-gate-verdict.json). Park the incoming
        # member as a contender instead — auditable, and resolve(accept=True)
        # remains the explicit human path to overwrite the total.
        idx = self._current.get(key)
        if idx is not None:
            cur = self.records[idx]
            if _is_aggregate_value(cur.value):
                self.dirty_slots.add(key)
                if _norm_value(slot.value) == _norm_value(cur.value):
                    # The same total re-asserted through add_member -> confirm
                    # the scalar, never park a contender identical to itself
                    # (review finding). Confidence/provenance/support only;
                    # tx_time/hlc are deliberately left untouched — a
                    # member-channel echo corroborates the total but is not a
                    # scalar re-assertion through write_fact's stamped path.
                    cur.last_confirmed = t
                    cur.provenance |= {p for p in provenance if p}
                    sup = _norm_support(support)
                    if sup:
                        cur.support.add(sup)
                    cur.confidence = min(
                        1.0, max(self._reinforce(cur.confidence), float(confidence)))
                    return WriteResult("confirmed", cur)
                return self._contend(cur, slot, emb, confidence,
                                     {p for p in provenance if p}, t,
                                     _norm_support(support),
                                     "member_add_blocked_aggregate",
                                     cur.slot_embedding,
                                     writer_id=writer_id,
                                     session_id=session_id,
                                     hlc=hlc, tx_time=t, valid_time=t)
            self.dirty_slots.add(key)
            cur.status = "superseded"
            cur.superseded_at = t
            cur.superseded_by_value = "(converted to set)"
            del self._current[key]
            self._log(cur, slot.value, confidence, t, "convert_to_set",
                      "member_add_to_scalar", writer_id=writer_id,
                      session_id=session_id)
            # Set members are evergreen-only (see _insert_member), so a
            # non-evergreen scalar's freshness class does NOT ride through
            # the conversion. The loss is deliberate — stamp it on the
            # conversion's audit entry rather than dropping it silently.
            # The superseded scalar row above keeps its own class for audit.
            if cur.freshness_class and cur.freshness_class != "evergreen":
                self.supersession_log[-1]["dropped_freshness_class"] = \
                    cur.freshness_class
            # v35: members carry no labels (v1 scope), so a labelled
            # scalar's pair is dropped by the conversion too — stamped the
            # same way, never silently (a converted constraint stops being
            # pinned, and the audit row must say so).
            for fld in ("authority", "distortion_tolerance"):
                if getattr(cur, fld, None):
                    self.supersession_log[-1][f"dropped_{fld}"] = getattr(cur, fld)
            self._insert_member(Slot(cur.entity, cur.attribute, cur.value,
                                     cur.polarity),
                                cur.embedding, cur.confidence,
                                set(cur.provenance), cur.asserted_at,
                                hlc=hlc, writer_id=cur.writer_id,
                                session_id=cur.session_id,
                                support=cur.origin or None)
        # Dedup against current members: exact norm OR cosine >= threshold.
        members = self.members(slot.entity, slot.attribute)
        for m in members:
            same_norm = _norm_value(m.value) == _norm_value(slot.value)
            cos = float((m.embedding.reshape(-1) @ emb.reshape(-1))
                        / ((m.embedding.norm() * emb.norm()) + 1e-12)) \
                if m.embedding is not None else 0.0
            if same_norm or cos >= MEMBER_DEDUP_COSINE:
                self.dirty_slots.add(key)
                m.last_confirmed = t
                m.provenance |= {p for p in provenance if p}
                m.confidence = min(1.0, max(m.confidence, float(confidence)))
                return WriteResult("member_confirmed", m)
        if len(members) >= MAX_CURRENT_MEMBERS:
            # Rejected: return an unpersisted record carrying the OFFENDING
            # value (never an unrelated existing member), and log it against
            # itself so old_value/new_value both read as the rejected value
            # rather than pointing at an unrelated member — nothing was
            # actually superseded. No dirty_slots write: nothing changed.
            rejected = CortexRecord(
                entity=slot.entity, attribute=slot.attribute, value=slot.value,
                kind="member", confidence=float(confidence),
            )
            self._log(rejected, slot.value, confidence, t, "member_capped",
                      "max_current_members", writer_id=writer_id,
                      session_id=session_id)
            return WriteResult("member_capped", rejected)
        self.dirty_slots.add(key)
        rec = self._insert_member(slot, emb, confidence,
                                  {p for p in provenance if p}, t, hlc=hlc,
                                  writer_id=writer_id, session_id=session_id,
                                  support=support)
        return WriteResult("member_added", rec)

    def _insert_member(
        self,
        slot: Slot,
        emb: torch.Tensor | None,
        confidence: float,
        prov: set[str],
        t: float,
        hlc: tuple[int, int] | None = None,
        writer_id: str | None = None,
        session_id: str | None = None,
        support: str | None = None,
    ) -> CortexRecord:
        """Append one current member row and register it in ``self._members``.
        Mirrors :meth:`_insert` but never touches ``self._current`` — many of
        these can coexist at the same key.

        Unlike :meth:`_insert` there is deliberately no ``freshness_class``
        parameter: set members are structurally evergreen. Staleness decay
        exists to age scalar values that change without notice; a set's
        "no longer true" channel is the explicit :meth:`remove_member`
        retraction, and a group-level policy transform could not honour the
        stale-policy contract that fresh payloads stay byte-identical (see
        docs/guide/memory-model.md, "Conversion rules", and the pin in
        tests/test_stale_policy.py)."""
        rec = CortexRecord(
            entity=slot.entity,
            attribute=slot.attribute,
            value=slot.value,
            polarity=getattr(slot, "polarity", "+"),
            kind="member",
            confidence=float(confidence),
            status="current",
            provenance=set(prov),
            asserted_at=t,
            last_confirmed=t,
            embedding=emb,
            support={support} if support else set(),
            tx_time=t,
            valid_time=t,
            hlc_phys=(hlc[0] if hlc else None),
            hlc_logical=(hlc[1] if hlc else None),
            writer_id=writer_id,
            session_id=session_id,
            freshness_class="evergreen",
        )
        self.records.append(rec)
        self._members.setdefault(rec.key, []).append(len(self.records) - 1)
        return rec

    def remove_member(
        self, entity: str, attribute: str, member: str, *,
        now: float | None = None,
    ) -> WriteResult:
        """Retract one current member by normalised-value match. The row is
        kept (``status`` -> ``"removed"``, ``superseded_at`` stamped) as the
        audit trail, same idiom as scalar supersession — never hard-deleted.
        Returns ``"member_not_found"`` (record not persisted anywhere) when no
        current member at the slot matches."""
        t = time.time() if now is None else float(now)
        key = (_norm_key(entity), _norm_key(attribute))
        nv = _norm_value(member)
        idxs = self._members.get(key, [])
        pos = next(
            (p for p, i in enumerate(idxs) if _norm_value(self.records[i].value) == nv),
            None,
        )
        if pos is None:
            return WriteResult("member_not_found", CortexRecord(
                entity=entity, attribute=attribute, value=member, kind="member",
            ))
        self.dirty_slots.add(key)
        idx = idxs.pop(pos)
        rec = self.records[idx]
        rec.status = "removed"
        rec.superseded_at = t
        self._log(rec, member, rec.confidence, t, "member_removed", "user_removed")
        return WriteResult("member_removed", rec)

    def retire_current(
        self, entity: str, attribute: str, *, now: float | None = None,
    ) -> WriteResult | None:
        """Retire the current SCALAR at a slot (``status`` -> ``"retired"``,
        audit row kept) so the slot reads as absent again — the reverse of
        an ``inserted`` write, used by dream-run rollback (schema v27).
        ``forget`` cannot serve here (it hard-deletes the slot's whole
        history) and ``resolve`` only touches contenders. Returns ``None``
        when no current scalar exists; never touches members — a set slot
        is unwound member-by-member via :meth:`remove_member`."""
        t = time.time() if now is None else float(now)
        key = (_norm_key(entity), _norm_key(attribute))
        idx = self._current.get(key)
        if idx is None:
            return None
        rec = self.records[idx]
        if rec.status != "current" or rec.kind != "scalar":
            return None
        rec.status = "retired"
        rec.superseded_at = t
        del self._current[key]
        self.dirty_slots.add(key)
        self._log(rec, rec.value, rec.confidence, t, "retired", "rollback")
        return WriteResult("retired", rec)

    def members(
        self, entity: str, attribute: str, include_removed: bool = False,
    ) -> list[CortexRecord]:
        """Members of a set-valued slot. Current only by default (insertion
        order); ``include_removed=True`` also returns removed member rows
        (audit view), in overall record order."""
        key = (_norm_key(entity), _norm_key(attribute))
        if include_removed:
            return [r for r in self.records if r.key == key and r.kind == "member"]
        return [self.records[i] for i in self._members.get(key, [])]

    def slot_kind(self, entity: str, attribute: str) -> str | None:
        """``"scalar"`` if the slot holds a current scalar record,
        ``"set"`` if it has current members (or historical member rows and
        no current scalar), else ``None``.

        Conversion is one-way *while members are current*: once ALL members
        at a slot are removed, the slot reverts to scalar life —
        :meth:`write_fact` may write a fresh scalar there (its guard checks
        for CURRENT members only), and once it does, ``_current`` wins the
        check here and this reports ``"scalar"`` again. The removed member
        rows are never deleted; they remain as audit
        (``members(..., include_removed=True)``), just no longer reflected
        in ``slot_kind``. Consult ``_current`` FIRST so a slot's most recent
        write always wins the answer, rather than a stale historical
        member row perpetually pinning it to ``"set"``.
        """
        key = (_norm_key(entity), _norm_key(attribute))
        if key in self._current:
            return "scalar"
        if self._members.get(key) or any(
            r.key == key and r.kind == "member" for r in self.records
        ):
            return "set"
        return None

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

    def _contend(self, cur, slot, emb, confidence, prov, t, sup, reason,
                 slot_embedding=None, writer_id=None, session_id=None,
                 hlc=None, tx_time=None, valid_time=None,
                 freshness_class="evergreen", stance=None,
                 authority=INHERIT, distortion_tolerance=INHERIT):
        """Park a conflicting value as a contender at ``cur``'s slot rather than
        superseding. Keeps the current value canonical. At most one active
        contender per slot: a matching value confirms (reinforces) the existing
        contender; a different value supersedes the prior contender.

        The contender carries the write's own temporal stamps
        (hlc/tx_time/valid_time) and freshness class — parking must not
        strip them, or a later promotion serves an unstamped fact. A
        contender-confirm advances tx_time/hlc like :meth:`_confirm` but
        never moves valid_time (when it first held is not when it was
        re-stated).

        ``cur`` may be None (quarantine's empty-slot park): the key is
        derived from ``slot`` and log rows anchor on the contender itself —
        there is no current record to anchor on."""
        key = cur.key if cur is not None else (
            _norm_key(slot.entity), _norm_key(slot.attribute))
        existing = self._active_contender(key)
        if existing is not None and _norm_value(existing.value) == _norm_value(slot.value):
            # v29: same rule as _confirm — the latest asserting write owns
            # the contender's stance.
            existing.stance = stance
            existing.authority = _pick(authority, existing, "authority")
            existing.distortion_tolerance = _pick(
                distortion_tolerance, existing, "distortion_tolerance")
            existing.last_confirmed = t
            existing.provenance |= prov
            if sup:
                existing.support.add(sup)
            existing.confidence = min(
                1.0, max(self._reinforce(existing.confidence), float(confidence)),
            )
            if writer_id:
                existing.writer_id = writer_id
            if session_id:
                existing.session_id = session_id
            if tx_time is not None:
                existing.tx_time = float(tx_time)
            if hlc is not None:
                existing.hlc_phys, existing.hlc_logical = hlc
            self._log(cur if cur is not None else existing, slot.value,
                      confidence, t, "contested", "contender_confirmed",
                      writer_id=writer_id, session_id=session_id)
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
            tx_time=tx_time,
            valid_time=valid_time,
            hlc_phys=(hlc[0] if hlc else None),
            hlc_logical=(hlc[1] if hlc else None),
            writer_id=writer_id,
            session_id=session_id,
            freshness_class=_norm_freshness(freshness_class),
            stance=stance,
            # A fresh park inherits from the CURRENT it contests (None on
            # an empty-slot quarantine park); explicit labels restate.
            authority=_pick(authority, cur, "authority"),
            distortion_tolerance=_pick(
                distortion_tolerance, cur, "distortion_tolerance"),
        )
        self.records.append(rec)   # deliberately NOT registered in self._current
        self._log(cur if cur is not None else rec, slot.value, confidence, t,
                  "contested", reason,
                  writer_id=writer_id, session_id=session_id)
        return WriteResult("contested", rec)

    def resolve(self, entity, attribute, accept: bool, now: float | None = None,
                support: str = "user", hlc: tuple[int, int] | None = None):
        """Resolve the active contender at a slot. ``accept=True`` promotes it to
        current (old current -> superseded; the contender's support gains
        ``support`` — "user" for the explicit MCP resolve, "agent" when the
        consolidation quarantine promotes on an independent second witness,
        so an automated promotion is never stamped as a human act);
        ``accept=False`` retires it (current untouched). Returns a ``WriteResult``
        or ``None`` when there is no active contender.

        Promotion is itself a transaction: tx_time moves to the promotion
        time, and ``hlc`` — a fresh tick from the caller that owns the clock
        (the service layer) — becomes the promoted fact's ordering stamp, so
        it can defend the slot in :meth:`_should_supersede` against a later
        write replaying a pre-promotion HLC. Without ``hlc`` the contender's
        parked stamp stands. valid_time is never moved: when the fact became
        true is not when it was accepted.

        Service-adjacent routing guard (Task 4): if the slot was converted to
        a set (:meth:`add_member`) after this contender was parked against the
        scalar it used to hold, the contender's original ``cur`` no longer
        exists in ``self._current`` — promoting it here would silently
        register a second current record for the key (bypassing the
        write_fact scalar/set exclusivity guard) instead of routing through
        :meth:`add_member`/:meth:`remove_member` like every other write to a
        set slot. Refused (``WriteResult("refused", contender)``) without
        touching any state; nothing is marked dirty.
        """
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
        if self._members.get(key):
            return WriteResult("refused", contender)
        self.dirty_slots.add(key)
        cur_idx = self._current.get(key)
        cur = self.records[cur_idx] if cur_idx is not None else None
        if accept:
            if cur is not None:
                cur.status = "superseded"
                cur.superseded_at = t
                cur.superseded_by_value = contender.value
            contender.status = "current"
            # Fail CLOSED on an unrecognised tier: an invalid explicit
            # ``support`` lands as "agent" (the weakest asserting tier),
            # never "user" — the promotion path's stamp must not be
            # steerable upward by malformed input (2026-08-09 review).
            contender.support.add(_norm_support(support) or "agent")
            contender.last_confirmed = t
            contender.tx_time = t
            if hlc is not None:
                contender.hlc_phys, contender.hlc_logical = hlc
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

    def vocab_ranked(self, query_embedding: torch.Tensor | None,
                     limit: int = 120) -> list[str]:
        """Slot keys ranked by cosine of their value-free ``slot_embedding``
        against ``query_embedding`` (the dream batch), most-relevant first —
        so the keys shown in the extractor's vocab hint are the ones this
        batch plausibly updates. The plain :meth:`vocab` is alphabetical, and
        on a bank bigger than the hint window that starved the prompt of the
        very keys the batch was about (the 2026-07-06 coreference miss: the
        sidecar's ``version`` slot never appeared, so the extractor couldn't
        reuse it). Records without a slot embedding follow alphabetically;
        no embedding at all falls back to :meth:`vocab`."""
        cur = [r for r in self.records if r.status == "current"]
        with_emb = [r for r in cur if r.slot_embedding is not None]
        if query_embedding is None or not with_emb:
            return self.vocab(limit)
        q = query_embedding.detach().to("cpu", torch.float32).reshape(-1)
        q = q / (q.norm() + 1e-12)
        mat = torch.stack([r.slot_embedding.reshape(-1) for r in with_emb])
        mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
        sims = (mat @ q).tolist()
        keys: list[str] = []
        seen: set[str] = set()
        for i in sorted(range(len(with_emb)), key=lambda i: -sims[i]):
            k = "%s.%s" % with_emb[i].key
            if k not in seen:
                seen.add(k)
                keys.append(k)
        tail = {"%s.%s" % r.key for r in cur if r.slot_embedding is None}
        keys.extend(k for k in sorted(tail) if k not in seen)
        return keys[: max(0, int(limit))]

    def facts_ranked(self, query_embedding: torch.Tensor | None,
                     limit: int = 20,
                     value_chars: int = 120) -> list[tuple[str, str, str]]:
        """Current ``(entity, attribute, value)`` triples for the top-``limit``
        slots, ranked like :meth:`vocab_ranked` — the dream extractor's
        known-facts window (docs/specs/2026-07-10-known-facts-window-design.md).
        Values are truncated to ``value_chars`` to bound prompt size. Display
        forms (not normalised keys) so the prompt reads naturally. Records
        without a slot embedding follow alphabetically; no embedding at all
        falls back to alphabetical-by-key."""
        if limit <= 0:
            return []

        def _triple(r: CortexRecord) -> tuple[str, str, str]:
            v = r.value if len(r.value) <= value_chars else \
                r.value[:value_chars - 1] + "…"
            return (r.entity, r.attribute, v)

        cur = [r for r in self.records if r.status == "current"]
        with_emb = [r for r in cur if r.slot_embedding is not None]
        if query_embedding is None or not with_emb:
            ranked = sorted(cur, key=lambda r: "%s.%s" % r.key)
            return [_triple(r) for r in ranked[: int(limit)]]
        q = query_embedding.detach().to("cpu", torch.float32).reshape(-1)
        q = q / (q.norm() + 1e-12)
        mat = torch.stack([r.slot_embedding.reshape(-1) for r in with_emb])
        mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
        sims = (mat @ q).tolist()
        out = [_triple(with_emb[i])
               for i in sorted(range(len(with_emb)), key=lambda i: -sims[i])]
        tail = sorted((r for r in cur if r.slot_embedding is None),
                      key=lambda r: "%s.%s" % r.key)
        out.extend(_triple(r) for r in tail)
        return out[: int(limit)]

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
                self.dirty_slots.add(r.key)   # sync deletes the slot's rows
                continue
            keep.append(r)
        if removed:
            self.records = keep
            self._current = {}
            self._members = {}
            for i, r in enumerate(self.records):
                if r.status != "current":
                    continue
                if r.kind == "member":
                    self._members.setdefault(r.key, []).append(i)
                else:
                    self._current[r.key] = i
        return removed

    def clear(self) -> None:
        """Empty the store in memory — records, both slot indexes, the
        supersession log, the dirty-slot set, and the dream cursor.

        Test-support API: ``tests/conftest.py``'s ``pristine_service`` calls it
        to hand each test an empty cortex on a module-scoped service (the
        embedder stays warm). Production code supersedes, retires, or
        :meth:`forget`s instead — this leaves no audit trail whatsoever.

        In-memory only, by design. It DROPS ``dirty_slots`` rather than filling
        it, so a later ``sync_cortex_slots`` deletes nothing: on a PG-backed
        service the rows survive in storage and the next hydration brings them
        back. Durable deletion is :meth:`forget`.

        ``dream_cursor`` IS reset to 0.0. It is a high-water mark over the
        episodic turns that produced these records; leaving it set past an
        emptied store would silently skip consolidation of anything re-seeded
        afterwards at an older timestamp. The construction knobs
        (``supersede_confidence_margin``, ``reinforce_rate``,
        ``protect_provenance``) are configuration, not state, and are kept.
        """
        self.records = []
        self._reindex_current()   # rebuilds _current + _members — both empty
        self.supersession_log = []
        self.dirty_slots = set()
        self.meta_dirty = False
        self.dream_cursor = 0.0

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

    def candidates_for(
        self,
        entity: str,
        attribute: str,
        query_embedding: torch.Tensor | None = None,
        *,
        top_k: int = 5,
        min_score: float = 0.35,
    ) -> list[dict]:
        """Ranked nearby slots for an EMPTY slot lookup — leads, not answers.

        Same-entity current facts first (other attributes, most recently
        asserted/confirmed first, ``score=None``), then embedding-similar
        slots above ``min_score`` (``score`` = cosine). Never includes the
        queried slot itself.
        """
        key_ent, key_attr = _norm_key(entity), _norm_key(attribute)
        same = [r for r in self.current_records()
                if _norm_key(r.entity) == key_ent
                and _norm_key(r.attribute) != key_attr]
        same.sort(key=lambda r: -max(r.asserted_at, r.last_confirmed))
        out = [{"entity": r.entity, "attribute": r.attribute, "value": r.value,
                "score": None, "why": "same_entity"} for r in same[:top_k]]
        if query_embedding is not None and len(out) < top_k:
            seen = {r.key for r in same}
            seen.add((key_ent, key_attr))
            for rec, s in self.search(query_embedding, top_k=top_k * 2,
                                      min_score=min_score):
                if rec.key in seen:
                    continue
                seen.add(rec.key)
                out.append({"entity": rec.entity, "attribute": rec.attribute,
                            "value": rec.value, "score": round(float(s), 4),
                            "why": "similar_slot"})
                if len(out) >= top_k:
                    break
        return out

    def resolve_slot(
        self, slot_embedding: torch.Tensor, threshold: float,
    ) -> tuple[str, str] | None:
        """Best current slot whose value-free ``slot_embedding`` matches
        ``slot_embedding`` at cosine >= ``threshold`` — for paraphrase-robust dream
        resolution. Returns the canonical ``(entity, attribute)`` or ``None``.
        ``threshold <= 0`` disables (returns ``None``). Records without a stored
        slot embedding are ignored."""
        if threshold is None or float(threshold) <= 0.0:
            return None
        cands = [
            r for r in self.records
            if r.status == "current" and r.slot_embedding is not None
        ]
        if not cands:
            return None
        q = slot_embedding.detach().to("cpu", torch.float32).reshape(-1)
        q = q / (q.norm() + 1e-12)
        mat = torch.stack([r.slot_embedding.reshape(-1) for r in cands])
        mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
        sims = (mat @ q).tolist()
        best = max(range(len(cands)), key=lambda i: sims[i])
        if sims[best] >= float(threshold):
            return (cands[best].entity, cands[best].attribute)
        return None

    def dedup_siblings(self, threshold: float, *, apply: bool) -> list[dict]:
        """Collapse current slots whose value-free slot embeddings match at cosine
        >= ``threshold`` — paraphrase fragments of one fact, as past regex
        auto-promotes forked. Per cluster, keep the canonical (strongest provenance
        tier, then most-recent) and retire the rest (``status`` -> ``superseded``;
        audit trail kept). Returns a report of ``{"canonical", "retired"}`` per
        merged cluster; only mutates when ``apply`` is True. Records without a
        ``slot_embedding`` are skipped — backfill first (the service does).
        ``kind == "member"`` records are excluded outright (schema v26,
        bank-corrupting fix): members never carry a ``slot_embedding`` of
        their own, so a caller that backfilled one (the value-free
        ``f"{entity} {attribute}"`` embedding, same for every member of a
        slot) would make every member of that slot cosine-identical to its
        siblings and this method would cluster and supersede all but one —
        silently destroying the set."""
        cands = [r for r in self.current_records()
                 if r.slot_embedding is not None and r.kind != "member"]
        if len(cands) < 2:
            return []
        mat = torch.stack([r.slot_embedding.reshape(-1) for r in cands])
        mat = mat / (mat.norm(dim=1, keepdim=True) + 1e-12)
        sims = mat @ mat.t()                       # NxN cosine (rows L2-normed)

        parent = list(range(len(cands)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                if float(sims[i][j]) >= float(threshold):
                    parent[find(i)] = find(j)

        clusters: dict[int, list[int]] = {}
        for i in range(len(cands)):
            clusters.setdefault(find(i), []).append(i)

        report: list[dict] = []
        now = time.time()
        changed = False
        for members in clusters.values():
            if len(members) < 2:
                continue
            recs = [cands[m] for m in members]
            canonical = max(
                recs,
                key=lambda r: (_rank(r.origin), r.last_confirmed or r.asserted_at),
            )
            losers = [r for r in recs if r is not canonical]
            report.append({
                "canonical": (canonical.entity, canonical.attribute, canonical.value),
                "retired": [(r.entity, r.attribute, r.value) for r in losers],
            })
            if apply:
                for r in losers:
                    r.status = "superseded"
                    r.superseded_by_value = canonical.value
                    r.superseded_at = now
                    self.dirty_slots.add(r.key)
                self.dirty_slots.add(canonical.key)
                changed = True

        if apply and changed:
            self._current = {}
            self._members = {}
            for i, r in enumerate(self.records):
                if r.status != "current":
                    continue
                if r.kind == "member":
                    self._members.setdefault(r.key, []).append(i)
                else:
                    self._current[r.key] = i
        return report

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
                    # v26 (set-valued slots): "scalar" | "member". Omitting
                    # this from the snapshot dict is the file-mode half of
                    # the traced failure (Task 2 review) — every member
                    # would reload as kind="scalar" and _reindex_current's
                    # duplicate-scalar healing would demote all but one.
                    "kind": r.kind,
                    "provenance": sorted(r.provenance),
                    "asserted_at": r.asserted_at,
                    "last_confirmed": r.last_confirmed,
                    "supersedes_value": r.supersedes_value,
                    "superseded_by_value": r.superseded_by_value,
                    "superseded_at": r.superseded_at,
                    "embedding": r.embedding,
                    "slot_embedding": r.slot_embedding,
                    "support": sorted(r.support),
                    # v11 temporal stamp + v23 freshness. Omitting these was
                    # the file-mode half of the contender-stamps fix: every
                    # restart reloaded facts at HLC (0,0), so any stale write
                    # could supersede them, and freshness reset to evergreen.
                    "tx_time": r.tx_time,
                    "valid_time": r.valid_time,
                    "hlc_phys": r.hlc_phys,
                    "hlc_logical": r.hlc_logical,
                    "writer_id": r.writer_id,
                    "session_id": r.session_id,
                    "version": r.version,
                    "freshness_class": r.freshness_class,
                    # v29: nullable stance; omitting it here would silently
                    # strip every hedge on restart (the same file-mode
                    # round-trip class as the stamp/freshness fix above).
                    "stance": r.stance,
                    # v35: same file-mode round-trip rule as stance.
                    "authority": r.authority,
                    "distortion_tolerance": r.distortion_tolerance,
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
            # weights_only=True: cortex snapshot is tensors + plain containers;
            # never unpickle arbitrary objects from a possibly-tampered .pt (CWE-502).
            state = torch.load(str(path), weights_only=True)
        except TypeError:  # older torch without the kwarg (defaults to safe in 2.6+)
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
                kind=d.get("kind", "scalar"),
                provenance=set(d.get("provenance", [])),
                asserted_at=d.get("asserted_at", 0.0),
                last_confirmed=d.get("last_confirmed", 0.0),
                supersedes_value=d.get("supersedes_value"),
                superseded_by_value=d.get("superseded_by_value"),
                superseded_at=d.get("superseded_at"),
                embedding=d.get("embedding"),
                slot_embedding=d.get("slot_embedding"),
                support=set(d.get("support", [])),
                tx_time=d.get("tx_time"),
                valid_time=d.get("valid_time"),
                hlc_phys=d.get("hlc_phys"),
                hlc_logical=d.get("hlc_logical"),
                writer_id=d.get("writer_id"),
                session_id=d.get("session_id"),
                version=d.get("version", 1) or 1,
                freshness_class=d.get("freshness_class", "evergreen"),
                # v29; pre-v29 snapshots have no key -> None ("plainly").
                stance=d.get("stance"),
                authority=d.get("authority"),
                distortion_tolerance=d.get("distortion_tolerance"),
            )
            self.records.append(rec)
        self._reindex_current()

    def _reindex_current(self) -> None:
        """Rebuild the slot -> current index (and the slot -> members index)
        and self-heal the one-record-per-status invariant for SCALARS. If two
        scalar records share a normalised slot at the same LIVE status
        (``current`` or ``contested``) — e.g. legacy facts written before key
        normalisation, like ``NEBULA-SERPENT`` vs ``nebula-serpent`` — keep the
        most-recently-confirmed and demote the rest to ``superseded``. Member
        rows (``kind="member"``) are exempt from that healing: many current
        members legitimately share a slot, so they are simply collected into
        ``self._members`` in record order, never demoted."""
        self._current = {}
        self._members = {}
        seen_contested: dict[tuple[str, str], int] = {}

        def _demote(keep: int, drop: int) -> None:
            loser = self.records[drop]
            loser.status = "superseded"
            if loser.superseded_at is None:
                loser.superseded_at = self.records[keep].last_confirmed
            loser.superseded_by_value = self.records[keep].value
            self.dirty_slots.add(loser.key)   # persist load-time healing

        for i, rec in enumerate(self.records):
            if rec.status == "current" and rec.kind == "member":
                self._members.setdefault(rec.key, []).append(i)
            elif rec.status == "current":
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
