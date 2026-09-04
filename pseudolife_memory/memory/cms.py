"""Continuum Memory System (CMS) — N-band MIRAS orchestration.

Implements the Continuum Memory System from "Nested Learning: The Illusion of
Deep Learning" (Behrouz et al., NeurIPS 2025 / arXiv 2512.24695): memory as a
spectrum of modules, each updating at a different frequency.

In v0.5 the bands are :class:`src.memory.miras.MIRASBand` instances whose
update rule, objective, memory module, and retention policy are all
configurable per band — see :mod:`src.memory.miras` for the framework and
:mod:`src.memory.miras.presets` for the canonical preset specifications.

Architecture
------------
* The CMS holds ``self.bands: list[MIRASBand]`` ordered from fastest to
  slowest. New memories enter ``bands[0]``; promotion walks the chain
  pairwise (band[i] → band[i+1]) when an entry's access count or surprise
  crosses the source band's promotion thresholds.
* Update intervals are interpreted relative to the global interaction
  counter — ``bands[i]`` runs a consolidation pass every
  ``bands[i].update_interval`` interactions.
* For backwards compat with v0.4.x code, when bands ``[0..2]`` are named
  ``instant`` / ``short_term`` / ``long_term`` the CMS exposes those as
  attribute shims (``cms.instant``, ``cms.short_term``, ``cms.long_term``).

Reference bank (4th tier, ChromaDB) is unchanged from v0.4.x — it sits
outside the MIRAS spectrum (no gradient updates, documents not memories).
"""

from __future__ import annotations

import heapq
import logging
import math
import random
import re
import time
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import torch

from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.memory.miras.band import MIRASBand, build_band
from pseudolife_memory.memory.miras.retention import now_seconds
from pseudolife_memory.memory.meta_filter import is_meta_statement
from pseudolife_memory.memory.contradiction import detect_contradictions, decay_contradicted_entries
from pseudolife_memory.memory.slots import extract_slots
from pseudolife_memory.memory.bm25 import BM25Index, normalize_scores
from pseudolife_memory.memory.episodes import EpisodeManager, normalize_tags
from pseudolife_memory.utils.config import FUSION_MODES, MemoryConfig

if TYPE_CHECKING:
    from pseudolife_memory.memory.nli import NLIContradictionScorer
    from pseudolife_memory.memory.reranker import CrossEncoderReranker

logger = logging.getLogger(__name__)


# Temporal-cue lexicon for the timeline retrieval channel (agg-recall
# Phase 1, spec 2026-08-03-aggregation-aware-recall-design.md). Word-
# boundary matched, casefolded. Deliberate omissions: "may" (modal verb
# far more often than the month), bare weekday names (high-frequency in
# scheduling chatter that isn't asking about order). A false positive
# costs only chronological presentation of the memory portion, so the
# list leans inclusive elsewhere.
_TEMPORAL_CUE_RE = re.compile(
    r"\b(first|last|when|earliest|latest|before|after|since|until|ago|"
    r"how many times|how long|what order|in order|order in which|"
    r"sequence|chronolog\w*|timeline|"
    r"january|february|march|april|june|july|august|september|october|"
    r"november|december)\b",
    re.IGNORECASE,
)


def has_temporal_cue(text: str) -> bool:
    """True when ``text`` carries an ordinal/temporal cue — the trigger
    for the timeline retrieval channel. Pure and cheap (single regex)."""
    return bool(_TEMPORAL_CUE_RE.search(text or ""))


# Aggregation cues (2026-08-06-aggregation-serving-design.md): a SEPARATE
# predicate, deliberately not a widening of _TEMPORAL_CUE_RE — that regex
# also fires the timeline channel, which failed its gates and measured
# harmful on spurious firing. This one only widens chronicle-event
# serving, which measured harmless-when-present (ev2 gate 4). Bare
# "total"/"count"/"all the" are omitted as too frequent outside counting
# questions; "total" only fires with an explicit quantity noun. The
# total-<noun>/average/the-most widening covers the five cue-miss rows
# in evals/results/events-coverage-audit-0806.json.
_AGGREGATION_CUE_RE = re.compile(
    r"\b(how many|how much|how often|what percentage|in total|"
    r"total (?:number|amount|distance|cost|sum|time|money)|"
    r"altogether|each time|every time|average|the most)\b",
    re.IGNORECASE,
)


def has_aggregation_cue(text: str) -> bool:
    """True when ``text`` asks for a count/amount over occurrences — the
    trigger for full-list (uncapped-to-30) chronicle event serving."""
    return bool(_AGGREGATION_CUE_RE.search(text or ""))


# Explicit-date cue (2026-08-12 soak-review finding): "what happened on
# 2026-08-08?" carries none of the _TEMPORAL_CUE_RE words, so the
# strongest possible temporal cue served no events. A SEPARATE predicate
# for the same reason as the aggregation one — it widens only chronicle
# serving, never the gate-failed timeline channel. Year-first full dates
# only: month-day forms ("08-08") collide with ranges and issue numbers,
# and phone-number shapes ("0412-345-678") must not fire.
_DATE_CUE_RE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b")


def has_date_cue(text: str) -> bool:
    """True when ``text`` contains an explicit year-first calendar date —
    a chronicle-serving trigger equivalent to a temporal cue word."""
    return bool(_DATE_CUE_RE.search(text or ""))


# Saved-state schema versions. Bump when the on-disk layout changes in a
# way the loader needs to branch on.
#
#   v1 (v0.4.x) — top-level instant/short_term/long_term keys, raw torch
#                 optimiser state per band.
#   v2 (v0.5.x) — ``bands`` name-keyed dict; wrapped optimiser state
#                 ``{"name": ..., "opt": ...}``; ``axes`` block per band.
#   v3 (v0.6+)  — additive: entries carry ``last_logical_turn`` and
#                 ``chain_residual`` is recorded in the top-level saved
#                 config. Loaders pre-v3 ignore both new fields (default
#                 None / False on load).
#   v4 (v0.7+)  — additive: entries carry ``slots`` — a list of structured
#                 ``(entity, attribute, value, polarity)`` triples extracted
#                 at store time by :mod:`src.memory.slots`. Pre-v4 entries
#                 default to ``[]`` on load.
#   v5 (v0.7.6) — additive: entries carry ``superseded_by_text`` — the text
#                 of the newer memory that triggered this entry's
#                 supersession. Populated by
#                 :func:`src.memory.contradiction.decay_contradicted_entries`.
#                 Pre-v5 entries default to ``None`` on load.
#                 (Pre-existing bug fixed in v6: v5 declared this field but
#                 ``MIRASBand.get_state_dict`` never actually persisted it.
#                 v6 fixes this on the same pass.)
#   v6 (Tier C) — additive: entries carry ``episode_id`` / ``episode_title``
#                 (episode anchoring) and ``tags`` (multi-valued labels
#                 alongside the single-string ``source``). Top-level
#                 ``episodes`` block holds the :class:`EpisodeManager`
#                 state. Pre-v6 entries default to ``None`` / ``[]`` on
#                 load; pre-v6 ``episodes`` block defaults to empty.
SCHEMA_VERSION = 6

# Shared tokenizer for the slot-query pool (Pool 1.5): the query side and
# the entry-slot-token index below must use the identical rule, or the two
# silently drift apart and the index stops finding matches the old
# full-scan version would have found.
_SLOT_TOKEN_STOP_WORDS = {
    "the", "and", "you", "your", "for", "have", "had", "has",
    "with", "from", "this", "that", "what", "where", "when",
    "who", "why", "how", "are", "was", "were", "been", "being",
    "into", "onto", "out", "did", "does", "doing", "say", "said",
    "can", "will", "would", "should", "could", "may", "might",
    "any", "some", "all", "not", "yes", "tell", "tells", "told",
}
_SLOT_TOKEN_RE = re.compile(r"[a-z']{3,}")


def _content_tokens(text: str) -> set[str]:
    """Lowercase content-word tokens (≥3 chars, stop-words dropped)."""
    return {
        t for t in _SLOT_TOKEN_RE.findall(text.lower())
        if t not in _SLOT_TOKEN_STOP_WORDS
    }


def _entry_slot_tokens(entry: "MemoryEntry") -> set[str]:
    """Content tokens across an entry's slot (entity, value) pairs —
    attribute is skipped (usually a structural label like "type"/"breed",
    less informative for query matching)."""
    tokens: set[str] = set()
    for s_entity, _s_attr, s_value, _polarity in entry.slots:
        tokens |= _content_tokens(f"{s_entity} {s_value}")
    return tokens


class ContinuumMemorySystem:
    """Multi-band MIRAS memory system with frequency-based updates.

    Each band is a separate :class:`MIRASBand` with its own update rule,
    objective, memory module, and retention policy — see
    :mod:`src.memory.miras`. The chain of bands creates a spectrum from
    fast reactive memory (high LR, every-message updates) to slow
    consolidated memory (low LR, infrequent updates).
    """

    def __init__(
        self,
        config: MemoryConfig,
        reference_bank=None,
        nli_scorer: "NLIContradictionScorer | None" = None,
        reranker: "CrossEncoderReranker | None" = None,
        storage=None,
    ) -> None:
        self.config = config
        # Optional write-through backend (PostgresStorage). When set,
        # every entry mutation lands in storage before returning; the
        # in-memory bands are a cache hydrated at startup.
        self.storage = storage
        self._nli_scorer = nli_scorer
        self._nli_candidate_cap: int = (
            getattr(config.nli, "max_candidates", 8) if hasattr(config, "nli") else 8
        )
        # Optional cross-encoder reranker. Constructed by the caller
        # (MemoryService) only when ``config.reranker.enabled = True`` or
        # ``rerank=True`` is passed per-call to :meth:`retrieve`. The
        # reranker itself lazy-loads its model on the first ``rerank()``,
        # so the cost of attaching an unused reranker is zero.
        self._reranker = reranker
        device = "cuda" if torch.cuda.is_available() else "cpu"

        # ── Construct the N-band chain from the MIRAS config ──────────────────
        # The default ``titans`` preset produces 3 bands with the same shapes
        # as the v0.4.x flat TITANS defaults, so behaviour is unchanged for
        # users who don't opt into a different preset.
        _retention_boost = getattr(getattr(config, "traces", None),
                                   "retention_boost", 0.0)
        self.bands: list[MIRASBand] = [
            build_band(spec, embedding_dim=config.embedding_dim, device=device,
                       retention_boost=_retention_boost)
            for spec in config.miras.bands
        ]
        if not self.bands:
            raise ValueError(
                "ContinuumMemorySystem requires at least one MIRAS band. "
                "Check memory.miras.bands in config.yaml."
            )
        # Capacity eviction hands the entry down the chain (see
        # :meth:`_on_band_evict`), so the handler needs to know which band
        # overflowed — bind the index rather than the bare method.
        for i, b in enumerate(self.bands):
            b.on_evict = partial(self._on_band_evict, band_idx=i)

        # ── v0.4.x attribute shims ────────────────────────────────────────────
        # Code paths from before v0.5 read ``cms.instant`` / ``cms.short_term``
        # / ``cms.long_term`` directly (notably the test suite and a couple of
        # API routes). Expose those as named aliases when the first 3 bands
        # carry the conventional names — falls back to None for non-titans
        # presets where the band names differ.
        named: dict[str, MIRASBand] = {b.name: b for b in self.bands}
        self.instant = named.get("instant", self.bands[0])
        self.short_term = named.get("short_term", self.bands[1] if len(self.bands) > 1 else self.bands[0])
        self.long_term = named.get("long_term", self.bands[-1])

        self._interaction_count = 0

        # Logical-turn counter — separate from ``_interaction_count`` (which
        # ticks per :meth:`store`) so an agentic deployment that emits many
        # bookkeeping stores per logical turn (tool_call + tool_result +
        # llm_thinking + agent_action per agent step) could consolidate on
        # logical boundaries instead of blowing through six tiers in one
        # user-facing turn.
        #
        # DORMANT since the 2026-07-30 dead-code sweep: the
        # ``begin_logical_turn`` / ``end_logical_turn`` methods that opened a
        # turn had no callers and were removed, so nothing sets the flag and
        # the counter never advances. The rest of the seam is deliberately
        # intact — ``logical_turn_count`` rides the persisted state (pinned by
        # tests/test_cms_pt_schema.py), ``last_logical_turn`` is a live column in
        # storage/schema.py, and ``retrieve(min_logical_turn=...)`` still
        # filters on it. Re-adding the two setters re-activates the feature.
        self._logical_turn_count = 0
        self._in_logical_turn = False

        # Reference bank (4th tier — ChromaDB RAG, optional).
        self.reference = reference_bank

        # Introspection: surprise history per band (last 100). Keyed by band
        # name so new presets with custom names don't need code changes here.
        self._surprise_history: dict[str, list[float]] = {b.name: [] for b in self.bands}
        self._max_history = 100

        # Introspection: consolidation events (last 50).
        self._consolidation_events: list[dict] = []
        self._max_events = 50

        # Per-tier retrieval-hit instrumentation. Lets ``/api/memory/stats``
        # expose actual usage so we can measure whether deeper continua help.
        # Each band's counter is bumped when :meth:`retrieve` returns one of
        # its entries in the top-k merge.
        self._tier_hits: dict[str, int] = {b.name: 0 for b in self.bands}
        self._tier_queries: int = 0
        # True drops: capacity evictions with no deeper band to demote
        # into — the only way an entry leaves the system at capacity.
        # Under the flat default every eviction is one; surfaced in
        # stats() so real capacity pressure is never silent.
        self._true_drops: int = 0

        # Rolling coreference anchor for slot extraction (v0.7+). Tracks
        # the last named entity / type referent so that "I gave him away"
        # can attach a gender slot to the right entity even when the text
        # itself doesn't name it.
        self._last_entity_seen: str | None = None

        # Episode log (schema v6, Tier C). Owns the current-open episode
        # pointer + the per-episode metadata persisted alongside band state.
        # Stamped onto every entry that lands while an episode is open.
        self.episodes = EpisodeManager()

        # v0.2: set when the weights file (and its backup) failed to load
        # and the band MLPs restarted from fresh init. Entries are NOT
        # affected (they live in storage); surfaced via stats().
        self.weights_reset: bool = False

        # Slot-token inverted index (Pool 1.5 candidate gathering,
        # 2026-07-12 perf fix): token -> (ordinal, containing band, entry)
        # for every slotted entry, across every band. Built lazily on
        # first query instead of scanning every entry in every band on
        # every ``query_text`` search. Maintenance: a store EXTENDS it in
        # place (a new entry only ever adds tokens), while removals
        # (evict / delete / promote / clear) and wholesale replacement
        # (load / hydrate) flag it dirty for a lazy full rebuild. The
        # ordinal preserves band-then-insertion order so equal-score
        # ties rank deterministically (set/dict iteration order varies
        # with PYTHONHASHSEED across processes); the band name keys the
        # ``bands=`` filter on actual containment, not the entry's
        # ``bank`` stamp (stale after a preset-change hydration).
        self._slot_token_index: dict[str, list[tuple[int, str, MemoryEntry]]] = {}
        self._slot_index_dirty: bool = True
        self._slot_index_ordinal: int = 0
        # Divergences caught by the sampled shadow check (see
        # :meth:`_slot_index_shadow_check`). Surfaced via stats() so a
        # non-zero count is visible without grepping daemon logs.
        self._slot_index_shadow_divergences: int = 0
        # Dedicated RNG for the shadow sampler: drawing from the
        # module-global generator would perturb any consumer that seeds
        # ``random`` globally for reproducibility (PR #145 review note).
        self._shadow_rng = random.Random()

    @property
    def total_memories(self) -> int:
        total = sum(b.size for b in self.bands)
        if self.reference:
            total += self.reference.size
        return total

    # ------------------------------------------------------------------
    # Store path
    # ------------------------------------------------------------------

    def store(
        self,
        text: str,
        embedding: torch.Tensor,
        source: str = "",
        tags: list[str] | None = None,
        session_key: str | None = None,
        attribution_episode_id: str | None = None,
        authority: str | None = None,
        distortion_tolerance: str | None = None,
    ) -> tuple[bool, float]:
        """Store a new memory through the CMS pipeline.

        Order of operations:

        1. Filter self-referential meta-statements.
        2. Compute surprise across all bands (for telemetry + gating).
        3. Run contradiction detection against every band. Any entry
           flagged here is both decayed and marked ``superseded_at`` so
           retrieval hides it from the LLM.
        4. If a contradiction was found, **bypass the surprise gate**:
           the correction must land even when it is semantically
           near-identical to the fact it replaces. Otherwise apply the
           normal gate.
        5. Store in the first (fastest) band and periodically promote.

        ``attribution_episode_id`` (identity tier 2, spec 2026-07-18):
        overrides the ``session_key``-derived episode stamp with this
        specific (already-validated open) episode id — the handle wins
        attribution even when ``session_key`` resolves the header's own
        (different) session episode. Applied BEFORE the write-through
        insert and the promotion walk below, so it is what gets persisted
        and what survives promotion (:meth:`_consolidate` copies
        ``episode_id`` off the entry, not off ``session_key``) — doing
        this after :meth:`store` returns would race the very promotion it
        triggers internally, since a promoted entry is a new object.

        ``authority`` / ``distortion_tolerance`` (schema v35): the write-time
        label pair, already resolved by the caller (``service.store`` runs
        the heuristic and the inheritance rules); stamped on the entry
        before the write-through so the row carries them.

        Returns:
            Tuple of ``(was_stored, surprise_score)``.
        """
        if self.config.meta_filter.enabled and is_meta_statement(text, role=source):
            return False, 0.0

        # ── Surprise telemetry (min across bands) ─────────────────────────────
        per_band_surprise = [b.compute_surprise(embedding) for b in self.bands]
        overall_surprise = min(per_band_surprise)
        for b, s in zip(self.bands, per_band_surprise):
            history = self._surprise_history.setdefault(b.name, [])
            history.append(s)
            if len(history) > self._max_history:
                self._surprise_history[b.name] = history[-self._max_history:]

        # ── Contradiction detection (runs BEFORE the surprise gate) ───────────
        # Corrections are often semantically near-identical to the fact
        # they replace ("dog is Rex" → "dog is Max"), so their surprise is
        # LOW. If we gated first, the write would be silently dropped and
        # the old fact would live on forever. Instead: detect first, and
        # if anything is flagged, force the write through regardless of
        # surprise.
        device = "cuda" if torch.cuda.is_available() else "cpu"
        # Extracted once, here, rather than after the write: the slot-identity
        # path needs them, and they are reused for the entry stamp below —
        # extraction is regex-heavy and was previously run twice per store.
        extracted_slots = extract_slots(
            text, last_entity_context=self._last_entity_seen,
        )
        new_slots = [
            (s.entity, s.attribute, s.value, s.polarity) for s in extracted_slots
        ]
        contradiction_found = False
        all_contradicted: list[MemoryEntry] = []
        for band in self.bands:
            contradicted = detect_contradictions(
                text, embedding, band.entries,
                similarity_threshold=0.7, device=device,
                nli_scorer=self._nli_scorer,
                nli_candidate_cap=self._nli_candidate_cap,
                new_slots=new_slots,
            )
            if contradicted:
                all_contradicted.extend(contradicted)
                # Decay factor is band-policy-specific; pull it from the band's
                # retention policy rather than hardcoding 0.3.
                # ``superseding_text=text`` records the new memory's text on
                # each superseded entry (schema v5, v0.7.6) so the context
                # builder can show the correction inline even when the new
                # memory's own embedding misses retrieval.
                decay_contradicted_entries(
                    contradicted,
                    decay_factor=band.retention.decay_factor_on_contradiction,
                    superseding_text=text,
                )
                contradiction_found = True

        if not contradiction_found and overall_surprise < self.config.surprise_threshold:
            return False, overall_surprise

        # Write-through: persist supersession marks set by the
        # contradiction decay above (entries already have rows).
        if self.storage is not None:
            for c in all_contradicted:
                if c.db_id is not None:
                    self.storage.update_entry(
                        c.db_id,
                        superseded_at=c.superseded_at,
                        superseded_by_text=c.superseded_by_text,
                        surprise=float(c.surprise_score),
                    )

        # ── Land the write in the first band ──────────────────────────────────
        self.bands[0].store(text, embedding, source=source, surprise=overall_surprise)
        if self.bands[0].entries:
            entry = self.bands[0].entries[-1]
            # Stamp logical turn (schema v3 — None when no turn open).
            if self._in_logical_turn:
                entry.last_logical_turn = self._logical_turn_count + 1
            # Stamp episode (schema v6, Tier C). Routes to the CALLER's session
            # episode (session_key) under concurrency; falls back to the global
            # current leaf when no key is supplied (embedded / legacy). No-op
            # when nothing is open. Carries context through promotion.
            self.episodes.stamp(entry, session_key)
            # Attribution override (identity tier 2): a valid episode handle
            # targets ITS episode regardless of what session_key stamped
            # above. Must land before the write-through insert and the
            # promotion walk further down — both key off the entry object,
            # not off session_key, so this is the only point that survives.
            if attribution_episode_id is not None:
                entry.episode_id = attribution_episode_id
                target_ep = self.episodes.episodes.get(attribution_episode_id)
                if target_ep is not None:
                    entry.episode_title = target_ep.title
            # Tag stamp (schema v6, Tier C). Normalised once here so
            # downstream filters can do plain set-intersection.
            entry.tags = normalize_tags(tags)
            # Write-time label pair (schema v35), resolved upstream.
            entry.authority = authority
            entry.distortion_tolerance = distortion_tolerance
            # Structured slots (schema v4), extracted above the contradiction
            # scan because the slot-identity path needs them. Reused here
            # rather than re-extracted — ``last_entity_context`` threads
            # recent-entity coreference across messages, letting "I gave him
            # away" inherit the previous turn's "Jacque" anchor, and it has
            # not moved between the two points.
            entry.slots = list(new_slots)
            # Slot-index upkeep: a new entry can only ADD tokens, so a
            # live (non-dirty) index is extended in place rather than
            # flagged for rebuild — the daemon's steady state interleaves
            # store/search, and dirtying on every store would rebuild on
            # nearly every search. A slotless entry (the common case;
            # slot extraction is precision-gated) contributes nothing.
            # If the index is already dirty, the pending rebuild will
            # pick this entry up. Removals still flag dirty.
            if entry.slots and not self._slot_index_dirty:
                self._slot_index_add(self.bands[0].name, entry)
            # Update the rolling coreference anchor — last text's first
            # named entity (if any) becomes the default referent for the
            # next message's pronouns.
            for ent, attr, _val, _pol in entry.slots:
                if attr in ("name", "type"):
                    self._last_entity_seen = ent
                    break
            # Write-through: the entry is fully stamped (turn / episode /
            # tags / slots) — persist it now and remember its row id so
            # later promotion / supersession / deletion can address it.
            if self.storage is not None:
                from pseudolife_memory.storage.sync import entry_to_row
                entry.db_id = self.storage.insert_entry(entry_to_row(entry))
        self._interaction_count += 1

        # ── Walk the promotion chain (band[i] → band[i+1]) ────────────────────
        # Per-store consolidation cadence. The logical-turn branch is dormant
        # (see ``_in_logical_turn`` in __init__): with no way to open a turn,
        # this guard is always taken.
        if not self._in_logical_turn:
            self._consolidate_eligible(self._interaction_count)

        return True, overall_surprise

    def _consolidate_eligible(self, counter: int) -> None:
        """Walk the promotion chain, firing each tier whose interval is hit."""
        for i in range(len(self.bands) - 1):
            destination = self.bands[i + 1]
            if counter % destination.update_interval == 0:
                self._consolidate(i, i + 1)

    # ------------------------------------------------------------------
    # Retrieval path
    # ------------------------------------------------------------------

    def temporal_neighbors(
        self, entry: MemoryEntry, n_each: int
    ) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
        """Stream-adjacent neighbors of ``entry``, ordered by timestamp.

        Neighborhood is the entry's episode when it has one, else its
        source (an episode-less entry never matches episode-carrying
        neighbors — the fallback is a scope, not a superset). Returns
        ``(before, after)``: ``before`` ends with the nearest earlier
        neighbor, ``after`` starts with the nearest later one.

        Per-query linear scan over band entries — the same cost profile
        the BM25 candidate pool already accepts (see ``retrieve``);
        deliberately no derived index, so there is no maintenance policy
        to get wrong.
        """
        if n_each <= 0:
            return [], []
        hide_superseded = bool(getattr(self.config, "hide_superseded", False))
        pool: list[MemoryEntry] = []
        seen: set[str] = {entry.text}
        for band in self.bands:
            for e in band.entries:
                if e.text in seen:
                    continue
                if hide_superseded and e.superseded_at is not None:
                    continue
                if entry.episode_id:
                    if e.episode_id != entry.episode_id:
                        continue
                elif e.episode_id or e.source != entry.source:
                    continue
                pool.append(e)
                seen.add(e.text)
        # (timestamp, seq): the wall clock cannot order same-tick stores —
        # ``MemoryEntry.seq`` exists precisely for this tie (see its
        # docstring) and survives band promotion.
        anchor = (entry.timestamp, entry.seq)
        stream_key = lambda e: (e.timestamp, e.seq)  # noqa: E731
        before = sorted(
            (e for e in pool if stream_key(e) <= anchor), key=stream_key)
        after = sorted(
            (e for e in pool if stream_key(e) > anchor), key=stream_key)
        return before[-n_each:], after[:n_each]

    def retrieve_with_trace(
        self,
        query_embedding: torch.Tensor,
        top_k: int | None = None,
        *,
        bands: list[str] | None = None,
        sources: list[str] | None = None,
        episodes: list[str] | None = None,
        tags: list[str] | None = None,
        min_logical_turn: int | None = None,
        query_text: str | None = None,
        min_score: float | None = None,
        disable_recency_boost: bool = False,
        rerank: bool | None = None,
        bm25: bool | None = None,
    ) -> tuple[RetrievalResult, dict]:
        """Like :meth:`retrieve` but also returns a structured trace dict
        describing exactly what happened — per-tier scores + per-entry
        breakdown of recency / source-weight / chain-residual / reranker
        contributions.

        Used by the ``GET /api/memory/trace`` endpoint for debugging
        retrieval misses ("why didn't it recall X?") and by tests that
        want to verify ranking behaviour. Identical ranking semantics to
        :meth:`retrieve` — the trace is purely additive instrumentation.
        """
        trace: dict = {
            "config": {
                "preset": getattr(self.config.miras, "preset", None),
                "chain_residual": getattr(self.config.miras, "chain_residual", False),
                "top_k": top_k or self.config.top_k,
            },
            "filters": {
                "bands": list(bands) if bands else None,
                "sources": list(sources) if sources else None,
                # Tier C filters — normalised by the caller so the trace
                # reflects exactly what the inner loop applied.
                "episodes": list(episodes) if episodes else None,
                "tags": (
                    normalize_tags(tags) if tags else None
                ),
                "min_logical_turn": min_logical_turn,
            },
            "tiers": [],
            # Filled by retrieve(): pool width, fusion mode, cut position.
            "candidate_pool": {},
            "chain_residual": {"enabled": False, "synthetic_hits": []},
            "bm25": {"fired": False, "hits": []},
            "reference_pool": [],
            "reranker": {"fired": False, "candidates": []},
            "final_topk": [],
        }
        result = self.retrieve(
            query_embedding,
            top_k=top_k,
            bands=bands,
            sources=sources,
            episodes=episodes,
            tags=tags,
            min_logical_turn=min_logical_turn,
            query_text=query_text,
            min_score=min_score,
            disable_recency_boost=disable_recency_boost,
            rerank=rerank,
            bm25=bm25,
            _trace=trace,
        )
        return result, trace

    def retrieve(
        self,
        query_embedding: torch.Tensor,
        top_k: int | None = None,
        *,
        bands: list[str] | None = None,
        sources: list[str] | None = None,
        episodes: list[str] | None = None,
        tags: list[str] | None = None,
        min_logical_turn: int | None = None,
        query_text: str | None = None,
        min_score: float | None = None,
        disable_recency_boost: bool = False,
        rerank: bool | None = None,
        bm25: bool | None = None,
        timeline: bool | None = None,
        _trace: dict | None = None,
    ) -> RetrievalResult:
        """Retrieve from CMS bands and merge results.

        Two-pool design preserved from v0.4.x:

        * **Neural pool** (every band in the continuum): guaranteed
          ``top_k`` slots, ranked by blended (cosine × source × recency)
          score. Entries from earlier (faster) bands get a recency
          boost; later bands rely on raw similarity.
        * **Reference pool** (ChromaDB documents): capped at
          ``ref_top_k`` slots, appended after the neural pool.

        Args:
            query_embedding: The encoded query.
            top_k: Maximum neural results. Falls back to ``config.top_k``.
            bands: When provided, restrict the neural pool to bands with
                these names — e.g. ``["working", "instant"]`` for "just the
                fast tiers" or ``["forever"]`` for identity recall only.
                ``None`` (default) queries every band.
            sources: When provided, drop entries whose ``source`` field is
                not in the list — e.g. ``["tool_result"]`` for knowledge
                lookup or ``["user_msg"]`` for "what did the user say".
                ``None`` (default) keeps all sources.
            min_logical_turn: When provided, drop entries with
                ``last_logical_turn < min_logical_turn`` — useful for
                "what changed this session" queries. ``None`` (default)
                keeps all turns.

        Filters compose: ``bands`` is applied first, then ``sources``, then
        ``min_logical_turn``, then the score-based ranking.
        """
        MIN_SCORE = 0.25 if min_score is None else float(min_score)
        # An explicitly-passed floor is a contract over the whole result
        # set, including BM25-only injections (which otherwise bypass the
        # dense pool's gate entirely). The *default* floor deliberately
        # does not bound them: injected scores are ``weight × normalised``
        # (≤0.3 at the shipped weight), so applying 0.25 to them would
        # admit only the single top lexical hit per query.
        explicit_floor = min_score is not None
        # Gentle penalty for assistant-authored memories so user-authored
        # facts outrank assistant restatements of the same fact.
        ASSISTANT_SCORE_MULT = 0.85
        # v0.7.3: superseded entries are no longer hidden from retrieval.
        # They surface with the same context as the entry that
        # invalidated them so the LLM (and downstream context builder)
        # can describe the historical sequence — "you used to have X,
        # then you said Y" — instead of pretending X never existed.
        # The score multiplier keeps current facts ranked higher than
        # their historical equivalents so abstention questions about
        # current state don't get drowned in old context.
        #
        # Set ``memory.hide_superseded = True`` to restore the v0.7.2
        # filter behaviour.
        SUPERSEDED_SCORE_MULT = 0.55

        def _source_mult(entry: MemoryEntry) -> float:
            return ASSISTANT_SCORE_MULT if entry.source == "assistant" else 1.0

        k = top_k or self.config.top_k
        ref_k = getattr(self.config, "ref_top_k", 3)

        # ── Candidate-pool shape (2026-09-04) ────────────────────────────
        # Resolved HERE rather than beside the channels that read them,
        # because the dense width has to be known before the band walk and
        # the cut position before the merge. ``getattr`` guards mirror the
        # bm25/reranker idiom below: eval harnesses pass config objects
        # predating the ``search`` block.
        search_cfg = getattr(self.config, "search", None)
        # max(1, …): a 0 or negative multiplier would silently narrow the
        # pool below the served width — fail safe to the shipped default
        # instead, and say so in ``params`` so the log is not a lie.
        pool_mult = max(1, int(
            getattr(search_cfg, "candidate_pool_multiplier", 1) or 1))
        fusion_mode = str(getattr(search_cfg, "fusion", "weighted_sum")
                          or "weighted_sum")
        # Belt. ``SearchConfig.__post_init__`` rejects a bad mode at LOAD
        # so config.yaml typos fail at startup rather than once per query;
        # this catches the objects that never ran it — per-attribute
        # setattr, eval harnesses, anything hand-built.
        if fusion_mode not in FUSION_MODES:
            raise ValueError(
                f"memory.search.fusion: unknown mode {fusion_mode!r} "
                f"(expected one of {', '.join(map(repr, FUSION_MODES))})")
        # Reciprocal rank fusion's smoothing constant. 60 is the value from
        # the method's original publication (Cormack, Clarke & Buettcher,
        # SIGIR 2009, "Reciprocal Rank Fusion outperforms Condorcet and
        # individual Rank Learning Methods") and the de-facto standard in
        # every hybrid-search implementation since; it is NOT tuned here,
        # and no local measurement justifies moving it.
        RRF_K = 60
        # Dense pool per band. ``band.retrieve`` already caps at band size,
        # and the cosine matmul runs over every entry regardless of top_k —
        # widening costs only a wider ``torch.topk`` selection, not a
        # second pass over the bank.
        pool_k = k * pool_mult
        pool_size = 0
        rerank_enabled = (
            rerank
            if rerank is not None
            else getattr(self.config.reranker, "enabled", False)
            if hasattr(self.config, "reranker")
            else False
        )
        # Today's order is truncate-then-rerank: the cross-encoder's
        # ``top_n`` (20) budget only ever saw the ~k+ref_k entries that
        # survived the cut. That stays the default — flipping it under
        # multiplier 1 would change the shipped path, which this change
        # deliberately does not. With a widened pool the reranker sees the
        # fused pool BEFORE the cut, which is the point of widening it.
        rerank_before_cut = bool(pool_mult > 1 and rerank_enabled)

        # v0.7.3: superseded entries are included in retrieval by
        # default. ``memory.hide_superseded`` (config field + console
        # knob, default False) restores the v0.7.2-and-earlier filter.
        # Hiding is the behaviour that caused the cat-Jacque
        # category-query failure, where the only entry mentioning the
        # category word was hard-filtered after a later supersession
        # event — so it is opt-in, for debugging and audit.
        # ``getattr`` (not an attribute read) because library callers
        # and eval harnesses pass config objects predating the field.
        hide_superseded = bool(getattr(self.config, "hide_superseded", False))

        def _keep(entry: MemoryEntry) -> bool:
            if not hide_superseded:
                return True
            return entry.superseded_at is None

        # Filter bands by name when requested. We still iterate by *depth*
        # in the original chain (not by filter-list order) so the recency
        # ramp lines up with the band's actual position in the continuum.
        band_filter: set[str] | None = set(bands) if bands else None
        source_filter: set[str] | None = set(sources) if sources else None
        # Tier C filters — None or empty list both mean "no filter" so a
        # typo doesn't silently drop every result.
        episode_filter: set[str] | None = (
            set(episodes) if episodes else None
        )
        tag_filter: set[str] | None = (
            set(normalize_tags(tags)) if tags else None
        )

        # ── Pool 1: neural memories — N bands, recency-weighted by depth ──────
        # The earlier the band, the stronger the recency boost. We schedule
        # the boost coefficient as a linear ramp: bands[0] gets boost=0.4 with
        # half-life 1 hour, bands[-1] gets boost=0 (no recency mod). Half-life
        # scales geometrically with depth.
        neural: list[tuple[MemoryEntry, float, float]] = []
        seen_texts: set[str] = set()
        n = len(self.bands)
        hit_band_names: set[str] = set()
        # Per-entry ranking components, keyed by text like ``via_map`` below
        # (texts are deduped by ``seen_texts``, so the key is unique within
        # a result). These are the fusion's INPUTS — the retrieval log
        # persists them because they cannot be recovered later: band
        # recency, supersession flags and access counts all mutate between
        # the serve and any offline replay. Built unconditionally: a dict of
        # floats per *kept* candidate, nothing recomputed.
        comps: dict[str, dict] = {}
        # Per-channel RANK lists for reciprocal rank fusion, in the order
        # each channel produced them. Built unconditionally (four list
        # appends) so the weighted-sum path is unchanged and the rrf path
        # has nothing to recompute. The dense list carries ``relevance``
        # (recency-modified cosine), NOT ``adjusted`` — the source and
        # supersession multipliers are applied once, at fusion time, so
        # rrf never double-counts them.
        dense_rank_src: list[tuple[str, float]] = []
        slot_rank_order: list[str] = []
        bm25_rank_order: list[str] = []
        timeline_rank_order: list[str] = []

        for depth, band in enumerate(self.bands):
            if band_filter is not None and band.name not in band_filter:
                if _trace is not None:
                    _trace["tiers"].append({
                        "name": band.name, "depth": depth, "filtered_out": True,
                        "candidates": [],
                    })
                continue

            # Ramp from (0.4, 3600s) at depth=0 down to (0.0, ∞) at depth=n-1.
            # Off by default since 2026-07-25 — depth is a proxy for age only
            # if promotion tracks age, and absent retrieval it tracks surprise
            # instead, so the ramp can invert similarity ordering (measured:
            # up to 18 points on the LongMemEval naive-RAG arm). Re-enable
            # with ``memory.recency_boost_enabled``.
            if n == 1 or disable_recency_boost or not self.config.recency_boost_enabled:
                boost, half_life = 0.0, float("inf")
            else:
                frac = depth / (n - 1)
                boost = 0.4 * (1.0 - frac)
                # Geometric half-life: base → 2×base → 4×base … (skip
                # recency at depth=n-1 anyway because boost=0). Base is
                # config-driven: 1h chat default, 24h in the MCP build.
                half_life = self.config.recency_base_half_life_s * (2.0 ** depth)

            band_result = band.retrieve(query_embedding, top_k=pool_k)
            pool_size = max(pool_size, len(band_result.entries))
            tier_trace: dict | None = None
            if _trace is not None:
                tier_trace = {
                    "name": band.name, "depth": depth, "filtered_out": False,
                    "boost": round(boost, 4), "half_life_s": half_life,
                    "candidates": [],
                }
                _trace["tiers"].append(tier_trace)

            for entry, score, surprise in zip(
                band_result.entries, band_result.scores, band_result.surprises
            ):
                # Reasons an entry might be dropped — surface in the trace so
                # callers can see WHY their fact isn't being recalled.
                cand: dict | None = None
                if tier_trace is not None:
                    cand = {
                        "text_preview": entry.text[:80] + ("…" if len(entry.text) > 80 else ""),
                        "source": entry.source,
                        "raw_score": round(float(score), 4),
                        "superseded": entry.superseded_at is not None,
                        "kept": False,
                        "drop_reason": None,
                    }
                    tier_trace["candidates"].append(cand)

                if entry.text in seen_texts:
                    if cand is not None: cand["drop_reason"] = "duplicate"
                    continue
                if not _keep(entry):
                    if cand is not None: cand["drop_reason"] = "superseded"
                    continue
                if source_filter is not None and entry.source not in source_filter:
                    if cand is not None: cand["drop_reason"] = f"source≠{sorted(source_filter)}"
                    continue
                if episode_filter is not None and entry.episode_id not in episode_filter:
                    if cand is not None:
                        cand["drop_reason"] = f"episode∉{sorted(episode_filter)}"
                    continue
                if tag_filter is not None and not (set(entry.tags) & tag_filter):
                    if cand is not None:
                        cand["drop_reason"] = f"tags∩{sorted(tag_filter)}=∅"
                    continue
                if min_logical_turn is not None:
                    entry_turn = getattr(entry, "last_logical_turn", None)
                    if entry_turn is None or entry_turn < min_logical_turn:
                        if cand is not None: cand["drop_reason"] = "logical_turn<min"
                        continue

                src_mult = _source_mult(entry)
                # Superseded entries surface but rank below their
                # current-state successors so abstention questions
                # ("Do I have a cat?") don't get drowned in history.
                supersession_mult = (
                    SUPERSEDED_SCORE_MULT if entry.superseded_at is not None else 1.0
                )
                # Recency is a relevance modifier — apply it before the
                # threshold. Source / supersession multipliers are
                # ranking-only modifiers and must NOT push a
                # semantically-relevant entry below the keep threshold,
                # because doing so silently dropped superseded entries
                # whose toy or low-similarity embeddings already
                # hovered around MIN_SCORE.
                if boost > 0.0:
                    recency = _recency_weight(entry.timestamp, half_life=half_life)
                    relevance = score * (1.0 + boost * recency)
                    if cand is not None:
                        cand["recency"] = round(recency, 4)
                else:
                    recency = 0.0
                    relevance = score
                adjusted = relevance * src_mult * supersession_mult

                if cand is not None:
                    cand["source_mult"] = src_mult
                    cand["supersession_mult"] = supersession_mult
                    cand["relevance"] = round(float(relevance), 4)
                    cand["adjusted_score"] = round(float(adjusted), 4)

                # Keep-decision is on the relevance (recency-modified
                # raw similarity), not on the further-multiplied
                # ranking score. ``adjusted`` still drives ordering.
                if relevance >= MIN_SCORE:
                    neural.append((entry, adjusted, surprise))
                    dense_rank_src.append((entry.text, float(relevance)))
                    seen_texts.add(entry.text)
                    # Counts POOL candidates, not served ones: with the
                    # multiplier on, a band lands here for entries the
                    # final cut to ``top_k`` then drops.
                    hit_band_names.add(band.name)
                    comps[entry.text] = {
                        "channel": "dense",
                        "dense": float(score),
                        "recency": float(recency),
                        "recency_boost": float(boost),
                        "source_mult": float(src_mult),
                        "supersession_mult": float(supersession_mult),
                        "surprise": float(surprise),
                        "band": band.name,
                        "band_depth": depth,
                    }
                    if cand is not None:
                        cand["kept"] = True
                else:
                    if cand is not None:
                        cand["drop_reason"] = f"relevance<{MIN_SCORE}"

        # ── Pool 1.5: slot-graph deterministic channel ────────────────────────
        # v0.7.3 Slice B. Embedding similarity is a probabilistic signal —
        # under low-volume training (fresh install, sparse memory) or
        # adversarial phrasings, the relevance score for the *right*
        # entry can land below ``MIN_SCORE`` even when the answer is
        # right there in the user's history.
        #
        # The slot store (v0.7-3) extracts deterministic
        # ``(entity, attribute, value, polarity)`` triples at write
        # time, but they're only used for context formatting today. Add
        # them as a parallel retrieval pool: any entry whose slot
        # entities or values share content tokens with the query text
        # gets pulled in with a confidence-scored slot hit.
        #
        # This is the cat-Jacque fix's belt to Slice A's suspenders:
        # even if the embedding for "I have a Ragdoll cat named Jacque"
        # somehow misses the "Do I have a cat?" query (because the
        # toy embedder's tokens drift, or the band's MLP is still
        # warming up), the slot ``Jacque.type=cat`` deterministically
        # routes the entry into the result set.
        if query_text:
            slot_hits = self._slot_query_pool(
                query_text=query_text,
                k=k,
                seen_texts=seen_texts,
                source_filter=source_filter,
                band_filter=band_filter,
                episode_filter=episode_filter,
                tag_filter=tag_filter,
                _trace=_trace,
            )
            for entry, score, surprise in slot_hits:
                # Slot hits carry their own 0.55-0.95 scale and skip the
                # dense pool's gate; an explicit caller floor still applies.
                if explicit_floor and score < MIN_SCORE:
                    continue
                neural.append((entry, score, surprise))
                # ``_slot_query_pool`` returns its hits already ranked
                # (confidence, then the index ordinal's deterministic
                # tie-break), so its emission order IS the rank list.
                slot_rank_order.append(entry.text)
                seen_texts.add(entry.text)
                # Slot hits carry their own 0.55-0.95 confidence scale, not a
                # cosine — logged under its own key so a learned head never
                # reads it as a bi-encoder score.
                comps[entry.text] = {
                    "channel": "slot",
                    "slot": float(score),
                    "surprise": float(surprise),
                    "band": entry.bank,
                }
                if entry.bank:
                    hit_band_names.add(entry.bank)

        # ── Pool 1.75: BM25 sparse lexical channel (Tier B2) ─────────────────
        # Dense embeddings underweight rare-but-exact tokens
        # (function names, version strings, error codes). BM25 weights
        # tokens by IDF so those tokens count for a lot. Runs in
        # parallel with the dense+slot pools, then weighted-sum-fuses
        # with the existing neural pool: entries in both pools get a
        # boost; entries only BM25 found enter at weight × normalised
        # score (below a typical dense hit).
        #
        # Off by default. Enable via config.bm25.enabled or pass
        # bm25=True per call.
        bm25_enabled = (
            bm25
            if bm25 is not None
            else getattr(self.config.bm25, "enabled", False)
            if hasattr(self.config, "bm25")
            else False
        )
        # ── Pool 1.9: timeline channel resolution (agg-recall Phase 1) ───────
        # Resolved before BM25 runs because both lexical channels share one
        # candidate pool. ``search_cfg`` was resolved with the pool-shape
        # knobs above.
        timeline_enabled = (
            timeline
            if timeline is not None
            else bool(getattr(search_cfg, "timeline_channel", False))
        )
        timeline_fired = bool(
            timeline_enabled and query_text and has_temporal_cue(query_text)
        )
        via_map: dict[str, str] = {}

        # Shared lexical candidate pool: every entry across every (filtered)
        # band. Cheaper than rebuilding the slot graph; rebuild-per-query
        # is acceptable up to ~tens of thousands of entries.
        candidates: list[MemoryEntry] = []
        if query_text and (bm25_enabled or timeline_fired):
            for band in self.bands:
                if band_filter is not None and band.name not in band_filter:
                    continue
                for entry in band.entries:
                    if not _keep(entry):
                        continue
                    if source_filter is not None and entry.source not in source_filter:
                        continue
                    if episode_filter is not None and entry.episode_id not in episode_filter:
                        continue
                    if tag_filter is not None and not (set(entry.tags) & tag_filter):
                        continue
                    if min_logical_turn is not None:
                        entry_turn = getattr(entry, "last_logical_turn", None)
                        if entry_turn is None or entry_turn < min_logical_turn:
                            continue
                    candidates.append(entry)

        if bm25_enabled and query_text:
            bm25_cfg = self.config.bm25
            if candidates:
                idx = BM25Index(candidates, k1=bm25_cfg.k1, b=bm25_cfg.b)
                raw_hits = idx.score(query_text, top_k=bm25_cfg.top_n)
                norm_hits = normalize_scores(raw_hits)

                # Build an entry-text → normalised score map for fusion.
                bm25_lookup: dict[str, float] = {
                    e.text: s for e, s in norm_hits if s >= bm25_cfg.min_score
                }

                # The lexical RANK list, for reciprocal rank fusion: the
                # gated hits in BM25's own descending order. Under "rrf"
                # this list — not the weighted sum below — is the channel's
                # entire contribution.
                bm25_rank_order = [e.text for e, s in norm_hits
                                   if s >= bm25_cfg.min_score]

                # Boost entries already in the neural pool. Skipped under
                # "rrf": adding ``weight x normalised`` to a cosine is the
                # weighted-sum fusion, and doing it as well as the rank
                # fusion would count the lexical channel twice. The
                # ``comps`` write below is instrumentation and happens
                # either way.
                #
                # Honest note: this guard does not currently change any
                # output — RRF replaces every pre-fusion score, so the boost
                # is dead there (verified 2026-09-04 by removing the guard:
                # tests/test_retrieval_pool.py stays green). It is kept
                # because the exclusivity is the design, and the first
                # future reader of the pre-fusion score under "rrf" would
                # otherwise silently get a double-counted lexical channel.
                boosted: list[tuple[MemoryEntry, float, float]] = []
                for entry, score, surprise in neural:
                    boost = bm25_lookup.get(entry.text, 0.0)
                    if boost > 0.0 and fusion_mode == "weighted_sum":
                        boosted.append((entry, score + bm25_cfg.weight * boost, surprise))
                    else:
                        boosted.append((entry, score, surprise))
                    # Set even at 0.0: "the lexical channel scored this
                    # entry at nothing" is a different feature from "the
                    # channel never looked at it" (key absent). Absent is
                    # NOT the same as "bm25 disabled" — reference-pool
                    # entries and the empty-candidate-pool path never get
                    # the key either; ``params.bm25.enabled`` is the
                    # authority on whether the channel ran at all.
                    if entry.text in comps:
                        comps[entry.text]["bm25"] = float(boost)
                neural = boosted

                # Inject BM25-only matches not yet in the pool.
                bm25_only_added: list[dict] = []
                for entry, norm_score in norm_hits:
                    if entry.text in seen_texts:
                        continue
                    if norm_score < bm25_cfg.min_score:
                        continue
                    # Score = weight × normalised BM25 — intentionally low
                    # so BM25-only hits don't displace strong dense hits,
                    # but high enough to outrank weak dense matches.
                    injected_score = bm25_cfg.weight * norm_score
                    if explicit_floor and injected_score < MIN_SCORE:
                        continue
                    neural.append((entry, injected_score, 0.0))
                    seen_texts.add(entry.text)
                    comps[entry.text] = {
                        "channel": "bm25",
                        "bm25": float(norm_score),
                        "surprise": 0.0,
                        "band": entry.bank,
                    }
                    if entry.bank:
                        hit_band_names.add(entry.bank)
                    bm25_only_added.append({
                        "text_preview": entry.text[:80] + (
                            "…" if len(entry.text) > 80 else ""
                        ),
                        "normalized_score": round(float(norm_score), 4),
                        "injected_score": round(float(injected_score), 4),
                    })

                if _trace is not None:
                    _trace["bm25"] = {
                        "fired": True,
                        "k1": bm25_cfg.k1,
                        "b": bm25_cfg.b,
                        "weight": bm25_cfg.weight,
                        "min_score": bm25_cfg.min_score,
                        "candidates_scored": len(candidates),
                        "raw_hits": len(raw_hits),
                        "hits": [
                            {
                                "text_preview": e.text[:80] + (
                                    "…" if len(e.text) > 80 else ""
                                ),
                                "raw_bm25": round(float(r), 4),
                                "normalized": round(float(n), 4),
                            }
                            for (e, r), (_, n) in zip(raw_hits, norm_hits)
                        ],
                        "injected": bm25_only_added,
                    }
            elif _trace is not None:
                _trace["bm25"] = {
                    "fired": False,
                    "reason": "no_candidates_after_filters",
                }

        # ── Pool 1.9: timeline channel injection (agg-recall Phase 1) ────────
        # Lexically-relevant entries for a temporally-cued query enter the
        # pool exactly like BM25-only injections: ``weight × normalised``
        # score (low, so they never displace strong dense hits), the
        # explicit caller floor still applies, the default floor
        # deliberately does not. The channel's second half — chronological
        # presentation — happens after the final merge below.
        if timeline_fired and candidates:
            TIMELINE_TOP_N = 6
            TIMELINE_WEIGHT = 0.3
            t_cfg = self.config.bm25  # scorer params only; independent of enabled
            t_idx = BM25Index(candidates, k1=t_cfg.k1, b=t_cfg.b)
            t_raw = t_idx.score(query_text, top_k=TIMELINE_TOP_N)
            t_norm = normalize_scores(t_raw)
            injected_n = 0
            for entry, norm_score in t_norm:
                if entry.text in seen_texts or norm_score <= 0.0:
                    continue
                injected_score = TIMELINE_WEIGHT * norm_score
                if explicit_floor and injected_score < MIN_SCORE:
                    continue
                neural.append((entry, injected_score, 0.0))
                seen_texts.add(entry.text)
                # Scored by the BM25 index too, but at the timeline
                # channel's own weight — the channel marker says which.
                comps[entry.text] = {
                    "channel": "timeline",
                    "bm25": float(norm_score),
                    "surprise": 0.0,
                    "band": entry.bank,
                }
                via_map[entry.text] = "timeline"
                timeline_rank_order.append(entry.text)
                if entry.bank:
                    hit_band_names.add(entry.bank)
                injected_n += 1
            if _trace is not None:
                _trace["timeline"] = {"fired": True, "injected": injected_n}
        elif _trace is not None and timeline_enabled and query_text:
            _trace["timeline"] = {
                "fired": False,
                "reason": ("no_temporal_cue" if not timeline_fired
                           else "no_candidates_after_filters"),
            }

        if fusion_mode == "rrf":
            # ── Reciprocal rank fusion ───────────────────────────────────
            # The four channels score on incommensurate scales (cosine,
            # 0.55-0.95 slot confidence, weight x normalised BM25); RRF
            # merges their RANK lists instead, so no scale has to be
            # reconciled. Each channel contributes 1/(RRF_K + rank).
            #
            # Ranking-only modifiers stay ranking-only: the source and
            # supersession multipliers are applied as a final MULTIPLICATIVE
            # adjustment on the fused score, not as a rank penalty. A rank
            # penalty would move an entry a whole position regardless of how
            # close the fused scores were — turning a tie-break into a hard
            # demotion, and leaving "a rank in WHICH list?" undefined for an
            # entry that only one channel found. Multiplying preserves
            # today's semantics: wide fused gaps are untouched, near-ties go
            # to the user-authored / current-state entry. Recency needs no
            # such step — it rides inside ``relevance``, which IS the dense
            # channel's rank order.
            #
            # ``min_score`` is deliberately NOT re-applied here. It is a
            # contract over each channel's own scale (the dense floor gates
            # cosines above; the explicit floor gates ``weight x normalised``
            # injections at their channel), and a fused score of ~0.03 has no
            # meaning against a 0.25 cosine floor — comparing them would empty
            # every result set and would drop lexical-only hits on a gate they
            # were never scored by.
            dense_ranked = [t for t, _ in sorted(
                dense_rank_src, key=lambda p: p[1], reverse=True)]
            fused_scores: dict[str, float] = {}
            for order in (dense_ranked, slot_rank_order,
                          bm25_rank_order, timeline_rank_order):
                for rank0, text in enumerate(order):
                    fused_scores[text] = (fused_scores.get(text, 0.0)
                                          + 1.0 / (RRF_K + rank0 + 1))
            neural = [
                # Multipliers read off the LIVE entry, at query time, exactly
                # as the weighted-sum path does — supersession can be flipped
                # between two queries on the same CMS and must move the entry.
                (entry,
                 fused_scores.get(entry.text, 0.0) * _source_mult(entry)
                 * (SUPERSEDED_SCORE_MULT if entry.superseded_at is not None
                    else 1.0),
                 surprise)
                for entry, _score, surprise in neural
            ]

        # Stable sort: ties keep insertion order, which is the band walk by
        # depth then the band's own ranking, then slot, then BM25, then
        # timeline — the tie-break determinism the raw sort provided before.
        neural.sort(key=lambda x: x[1], reverse=True)
        if not rerank_before_cut:
            neural = neural[:k]

        # Update per-tier instrumentation. ``hit_band_names`` is the set of
        # tiers that contributed at least one entry to the *post-merge*
        # result — gives a usage-rate signal we can surface via /api/memory/stats.
        self._tier_queries += 1
        for name in hit_band_names:
            self._tier_hits[name] = self._tier_hits.get(name, 0) + 1

        # ── Pool 2: reference documents ───────────────────────────────────────
        # Kept separate so they can NEVER displace neural memories.
        ref_pool: list[tuple[MemoryEntry, float, float]] = []
        if self.reference:
            ref_result = self.reference.retrieve(query_embedding, top_k=ref_k)
            for entry, score, surprise in zip(
                ref_result.entries, ref_result.scores, ref_result.surprises
            ):
                if entry.text not in seen_texts and score >= MIN_SCORE:
                    ref_pool.append((entry, score, surprise))
                    seen_texts.add(entry.text)
                    comps[entry.text] = {
                        "channel": "reference",
                        "dense": float(score),
                        "surprise": float(surprise),
                        "band": entry.bank,
                    }
            ref_pool = ref_pool[:ref_k]
            if _trace is not None:
                _trace["reference_pool"] = [
                    {
                        "text_preview": e.text[:80] + ("…" if len(e.text) > 80 else ""),
                        "score": round(float(s), 4),
                    }
                    for e, s, _ in ref_pool
                ]

        combined = neural + ref_pool

        # ── Pool 3: optional cross-encoder reranking ─────────────────────────
        # Tier B. When enabled (via config.reranker.enabled or rerank=True
        # per call), re-score the top-N combined candidates with a
        # cross-encoder and fuse with the bi-encoder score. Only fires when
        # we have query_text — without it the cross-encoder has nothing to
        # attend over. Falls through silently if the reranker is unavailable
        # (no model loaded, hub down) so retrieval never breaks because of
        # an optional component.
        # ``rerank_enabled`` was resolved with the pool-shape knobs above —
        # the cut position depends on it.
        #
        # Knob snapshot for the retrieval log. ``fired``/``skip_reason``
        # explain the per-entry ``ce: None`` a reader will meet below.
        rerank_log: dict = {
            "enabled": bool(rerank_enabled), "fired": False,
            "skip_reason": None,
        }
        if (
            rerank_enabled
            and self._reranker is not None
            and query_text
            and combined
            and self._reranker.is_available()
        ):
            top_n = getattr(self.config.reranker, "top_n", 20)
            head = combined[:top_n]
            tail = combined[top_n:]
            head_texts = [e.text for e, _, _ in head]
            head_orig_scores = [float(s) for _, s, _ in head]
            # Margin gate: when the two best bi-encoder scores are already
            # decisively separated, the cross-encoder can only reshuffle a
            # ranking that wasn't in doubt — skip the ~200ms pass. The head
            # is neural + reference CONCATENATED (not globally sorted), so
            # the gap must be measured on sorted scores. A single-candidate
            # head is trivially unambiguous. skip_margin=0 disables the gate.
            skip_margin = (
                float(getattr(self.config.reranker, "skip_margin", 0.0))
                if hasattr(self.config, "reranker")
                else 0.0
            )
            skip_for_margin = False
            rerank_log.update({
                "top_n": top_n,
                "skip_margin": skip_margin,
                "fusion_weight": getattr(
                    self.config.reranker, "fusion_weight", None),
                "model": getattr(self.config.reranker, "model_name", None),
            })
            if skip_margin > 0.0:
                ranked = sorted(head_orig_scores, reverse=True)
                margin = (
                    ranked[0] - ranked[1] if len(ranked) >= 2 else float("inf")
                )
                skip_for_margin = margin >= skip_margin
                # isfinite, not `!= inf`: a NaN would serialise as a bare
                # `NaN` literal that PG's jsonb input rejects, losing the
                # whole event row into the write-error counter.
                rerank_log["margin"] = (
                    float(margin) if math.isfinite(margin) else None
                )
            if skip_for_margin:
                ce_scores: list[float] = []
                rerank_log["skip_reason"] = "unambiguous_margin"
                if _trace is not None:
                    _trace["reranker"] = {
                        "fired": False,
                        "reason": "unambiguous_margin",
                        "margin": (
                            round(margin, 4) if margin != float("inf") else None
                        ),
                        "skip_margin": skip_margin,
                    }
            else:
                ce_scores = self._reranker.rerank(query_text, head_texts)
            # ``ce`` is set on every head entry either way: a float when the
            # pass ran, an explicit None when the margin gate (or an
            # unavailable model) skipped it — "the bi-encoder order was
            # served unrefined" is training signal, not a missing value.
            # Tail entries beyond top_n never had a ce score and get no key.
            for i, (entry, _, _) in enumerate(head):
                c = comps.get(entry.text)
                if c is not None:
                    c["ce"] = (float(ce_scores[i])
                               if i < len(ce_scores) else None)
            if ce_scores:
                rerank_log["fired"] = True
                fused = self._reranker.fuse(head_orig_scores, ce_scores)
                reranked = [
                    (entry, fused_s, surprise)
                    for (entry, _, surprise), fused_s in zip(head, fused)
                ]
                reranked.sort(key=lambda x: x[1], reverse=True)
                combined = reranked + tail
                if _trace is not None:
                    _trace["reranker"] = {
                        "fired": True,
                        "model": getattr(
                            self.config.reranker, "model_name", "?",
                        ),
                        "top_n": top_n,
                        "fusion_weight": getattr(
                            self.config.reranker, "fusion_weight", None,
                        ),
                        "candidates": [
                            {
                                "text_preview": entry.text[:80] + (
                                    "…" if len(entry.text) > 80 else ""
                                ),
                                "original_score": round(orig, 4),
                                "ce_score": round(ce, 4),
                                "fused_score": round(fused_s, 4),
                            }
                            for (entry, _, _), orig, ce, fused_s in zip(
                                head, head_orig_scores, ce_scores, fused,
                            )
                        ],
                    }
            elif not skip_for_margin:
                rerank_log["skip_reason"] = "rerank_failed_or_unavailable"
                if _trace is not None:
                    _trace["reranker"] = {
                        "fired": False,
                        "reason": "rerank_failed_or_unavailable",
                    }
        elif rerank_enabled:
            # Enabled but the gate above never opened: no model, no query
            # text, or nothing to rerank.
            rerank_log["skip_reason"] = "unavailable"

        if rerank_before_cut:
            # Deferred cut: the reranker — not the bi-encoder — chooses which
            # MEMORIES survive, but the reference pool's slots are reserved,
            # not raced for. A plain ``combined[:k + len(ref_pool)]`` would
            # drop reference documents outright whenever the widened neural
            # pool alone fills the budget, because ``combined`` is
            # ``neural + ref_pool`` CONCATENATED and the refs trail
            # positionally — inverting Pool 2's standing guarantee that
            # reference documents can never be displaced by memories. Same
            # output cardinality as the default path (``neural[:k] +
            # ref_pool``), and the post-rerank ORDER is preserved for both
            # kinds, including any interleaving the cross-encoder produced
            # (which the default path also allows). When the pass did not
            # fire (no model, margin gate) the widened pool is still sorted,
            # so this is exactly the default path's result.
            ref_texts = {e.text for e, _, _ in ref_pool}
            kept: list[tuple[MemoryEntry, float, float]] = []
            n_mem = 0
            for item in combined:
                if item[0].text in ref_texts:
                    kept.append(item)
                elif n_mem < k:
                    kept.append(item)
                    n_mem += 1
            combined = kept

        # Knobs in force for THIS query — config is mutable at runtime, so
        # a training reader cannot recover them from today's config.
        params: dict = {
            "top_k": int(k),
            "min_score": float(MIN_SCORE),
            "min_score_explicit": explicit_floor,
            # band_count, not "bands" — ``filters.bands`` below is the
            # band-NAME filter, and one key meaning two things in a blob
            # read offline months later is a trap.
            "band_count": n,
            # getattr guards mirror the bm25/reranker idiom above: eval
            # harnesses pass config objects predating these fields.
            "recency_boost": bool(
                n > 1 and not disable_recency_boost
                and getattr(self.config, "recency_boost_enabled", False)),
            "recency_base_half_life_s": float(
                getattr(self.config, "recency_base_half_life_s", 0.0)),
            "hide_superseded": hide_superseded,
            "bm25": {"enabled": bool(bm25_enabled)},
            # Candidate-pool shape. ``pool_size`` is the widest dense pool
            # any queried band actually returned (band-size capped), not the
            # requested ``k x multiplier`` — an offline reader needs to know
            # what the fusion ranked over, not what was asked for.
            "candidate_pool": {
                "multiplier": int(pool_mult),
                "pool_size": int(pool_size),
                "fusion": fusion_mode,
                "rerank_position": (
                    "before_cut" if rerank_before_cut else "after_cut"),
            },
            "reranker": rerank_log,
            "timeline": {"enabled": bool(timeline_enabled),
                         "fired": bool(timeline_fired)},
            # Filters shape the candidate set the fusion ranked over; a
            # replay of the bare query text would rank a different pool.
            "filters": {
                "bands": list(bands) if bands else None,
                "sources": list(sources) if sources else None,
                # sorted: the episode subtree arrives from a set, so an
                # unsorted copy makes two identical queries diff.
                "episodes": sorted(episodes) if episodes else None,
                "tags": normalize_tags(tags) if tags else None,
                "min_logical_turn": min_logical_turn,
            },
        }
        if bm25_enabled and hasattr(self.config, "bm25"):
            params["bm25"].update({
                "weight": float(self.config.bm25.weight),
                "min_score": float(self.config.bm25.min_score),
                "k1": float(self.config.bm25.k1),
                "b": float(self.config.bm25.b),
                "top_n": int(self.config.bm25.top_n),
            })

        if _trace is not None:
            _trace["candidate_pool"] = dict(params["candidate_pool"])
            _trace["final_topk"] = [
                {
                    "text_preview": e.text[:120] + ("…" if len(e.text) > 120 else ""),
                    "score": round(float(s), 4),
                    "source": e.source,
                    "bank": e.bank,
                }
                for e, s, _ in combined
            ]

        if not combined:
            return RetrievalResult(entries=[], scores=[], surprises=[],
                                   params=params)

        # Timeline presentation: when the channel fired, the MEMORY portion
        # of the final result is ordered by stream position — (timestamp,
        # seq), the tie-break the wall clock cannot provide — because
        # sequence is exactly what score-ordering destroys and what
        # temporally-cued questions need. Reference documents keep their
        # trailing position: they carry no meaningful stream position.
        if timeline_fired:
            ref_texts = {e.text for e, _, _ in ref_pool}
            mem_part = [t for t in combined if t[0].text not in ref_texts]
            ref_part = [t for t in combined if t[0].text in ref_texts]
            mem_part.sort(key=lambda t: (t[0].timestamp, t[0].seq))
            combined = mem_part + ref_part

        entries, scores, surprises = zip(*combined)
        # Access accrual happens HERE, on the final merged result set —
        # not in band.retrieve, whose top-k is only a candidate pool.
        for e in entries:
            e.access_count += 1
        return RetrievalResult(
            entries=list(entries),
            scores=list(scores),
            surprises=list(surprises),
            via=([via_map.get(e.text) for e in entries] if via_map else None),
            components=[comps.get(e.text) for e in entries],
            params=params,
        )

    def compute_surprise(self, embedding: torch.Tensor) -> float:
        """Aggregate surprise across all bands (min — anything any band
        already knows isn't surprising)."""
        return min(b.compute_surprise(embedding) for b in self.bands)

    # ------------------------------------------------------------------
    # Slot fact-sheet (v0.7+)
    # ------------------------------------------------------------------

    def _rebuild_slot_index(self) -> None:
        """Rebuild ``_slot_token_index`` (token -> (ordinal, band, entry))
        from every band's entries — the same lazy-dirty-flag idiom
        :class:`~pseudolife_memory.memory.miras.band.MIRASBand` already
        uses for its cosine pattern matrix. Runs only after a removal or
        wholesale entry replacement (:meth:`_slot_query_pool` calls this
        when ``_slot_index_dirty``); plain stores extend the index in
        place via :meth:`_slot_index_add` instead. The ordinal records
        the band-then-insertion walk position for deterministic
        tie-breaking at query time."""
        index, ordinal = self._compute_slot_index()
        self._slot_token_index = index
        self._slot_index_ordinal = ordinal
        self._slot_index_dirty = False

    def _compute_slot_index(
        self,
    ) -> tuple[dict[str, list[tuple[int, str, MemoryEntry]]], int]:
        """Walk every band and build a fresh slot-token index, without
        touching the live one — shared by :meth:`_rebuild_slot_index`
        (which adopts the result) and the shadow check (which compares
        against the live copy first)."""
        index: dict[str, list[tuple[int, str, MemoryEntry]]] = {}
        ordinal = 0
        for band in self.bands:
            for entry in band.entries:
                if not entry.slots:
                    continue
                tokens = _entry_slot_tokens(entry)
                if not tokens:
                    continue
                item = (ordinal, band.name, entry)
                ordinal += 1
                for tok in tokens:
                    index.setdefault(tok, []).append(item)
        return index, ordinal

    def _slot_index_shadow_check(self) -> bool:
        """Sampled runtime tripwire for the slot-index maintenance contract.

        Recomputes the index from the band entries and compares
        MEMBERSHIP — ``token -> {(band, entry identity)}`` — against the
        live copy. Ordinals are deliberately excluded: extend-in-place
        assigns them past the last rebuild's ceiling, so they legitimately
        differ from a fresh rebuild's band-then-insertion renumbering.
        A membership divergence, by contrast, always means some mutation
        path neither extended the index nor flagged it dirty (the bug
        class the 2026-07-12 audit found three of post-deploy). On
        divergence: log, count (``stats()``), and self-repair by adopting
        the fresh copy. Returns True when a divergence was found.

        The compare-and-adopt is only safe because every CMS read and
        write runs under ``MemoryService._lock`` — a reader outside that
        lock could otherwise lose a posting a concurrent store extended
        into the live index after the fresh walk."""
        fresh_index, fresh_ordinal = self._compute_slot_index()

        def _membership(
            ix: dict[str, list[tuple[int, str, MemoryEntry]]],
        ) -> dict[str, set[tuple[str, int]]]:
            return {
                tok: {(band, id(entry)) for _o, band, entry in items}
                for tok, items in ix.items()
            }

        live_m = _membership(self._slot_token_index)
        fresh_m = _membership(fresh_index)
        if live_m == fresh_m:
            return False

        # Name the suspect before the repair destroys the evidence: which
        # tokens hold ghost postings (live-only) or missed ones
        # (fresh-only), and which bands they sit in. Tokens only — no
        # entry text in daemon logs.
        stale = {t: live_m[t] - fresh_m.get(t, set())
                 for t in live_m if live_m[t] - fresh_m.get(t, set())}
        unindexed = {t: fresh_m[t] - live_m.get(t, set())
                     for t in fresh_m if fresh_m[t] - live_m.get(t, set())}
        bands = sorted({b for postings in (*stale.values(), *unindexed.values())
                        for b, _i in postings})
        self._slot_index_shadow_divergences += 1
        logger.warning(
            "Slot-index shadow check: live index diverged from a fresh "
            "rebuild — a mutation path bypassed the maintenance contract. "
            "%d stale token(s) (e.g. %s), %d unindexed token(s) (e.g. %s), "
            "band(s) %s. Repaired by adopting the fresh copy; "
            "divergence #%d.",
            len(stale), sorted(stale)[:5],
            len(unindexed), sorted(unindexed)[:5],
            bands, self._slot_index_shadow_divergences,
        )
        self._slot_token_index = fresh_index
        self._slot_index_ordinal = fresh_ordinal
        self._slot_index_dirty = False
        return True

    def _slot_index_add(self, band_name: str, entry: "MemoryEntry") -> None:
        """Extend a live slot index with one freshly-stored entry."""
        tokens = _entry_slot_tokens(entry)
        if not tokens:
            return
        item = (self._slot_index_ordinal, band_name, entry)
        self._slot_index_ordinal += 1
        for tok in tokens:
            self._slot_token_index.setdefault(tok, []).append(item)

    def _slot_query_pool(
        self,
        query_text: str,
        k: int,
        seen_texts: set[str],
        source_filter: set[str] | None = None,
        band_filter: set[str] | None = None,
        episode_filter: set[str] | None = None,
        tag_filter: set[str] | None = None,
        _trace: dict | None = None,
    ) -> list[tuple["MemoryEntry", float, float]]:
        """Pull entries via slot-token overlap with the query.

        Looks the query's content tokens up in the slot-token inverted
        index (:meth:`_rebuild_slot_index`) instead of scanning every
        entry in every band, then scores only the entries that share at
        least one token with the query. Returns the top-k hits,
        score-sorted, with the same ``(entry, score, surprise)`` tuple
        shape the neural pool emits.

        Designed to be a *belt and suspenders* second channel: the
        neural pool catches paraphrastic / fuzzy matches, the slot
        pool catches exact-fact lookups (category vs entity queries,
        attribute mentions). When both pools hit the same entry the
        ``seen_texts`` dedup keeps it from double-counting.
        """
        tokens = _content_tokens(query_text)
        if not tokens:
            return []

        if self._slot_index_dirty:
            self._rebuild_slot_index()
        else:
            # Sampled shadow verification of a live (non-dirty) index —
            # a dirty index is about to be rebuilt anyway, so there is
            # nothing to check. Plain attribute read: every construction
            # site passes a real MemoryConfig, and a getattr fallback
            # would turn a future field rename into a silent no-op.
            rate = self.config.slot_index_shadow_rate
            if rate > 0.0 and (rate >= 1.0 or self._shadow_rng.random() < rate):
                self._slot_index_shadow_check()

        slot_trace_block: list[dict] | None = None
        if _trace is not None:
            slot_trace_block = []
            _trace["slot_pool"] = slot_trace_block

        # Union of entries indexed under any query token — replaces the
        # previous full "every band, every entry" scan. Visited in
        # ordinal (band-then-insertion) order so the stable score sort
        # below breaks ties exactly like the old scan did, independent
        # of PYTHONHASHSEED.
        candidate_map: dict[int, tuple[int, str, "MemoryEntry"]] = {}
        for tok in tokens:
            for item in self._slot_token_index.get(tok, ()):
                candidate_map[id(item[2])] = item

        candidates: list[tuple["MemoryEntry", float, float]] = []
        for _ordinal, band_name, entry in sorted(
                candidate_map.values(), key=lambda t: t[0]):
            if entry.text in seen_texts:
                continue
            # Filter on the band that CONTAINS the entry (recorded at
            # index time), not entry.bank — the stamp can go stale when a
            # preset change makes hydration re-route rows into band[0].
            if band_filter is not None and band_name not in band_filter:
                continue
            if source_filter is not None and entry.source not in source_filter:
                continue
            if episode_filter is not None and entry.episode_id not in episode_filter:
                continue
            if tag_filter is not None and not (set(entry.tags) & tag_filter):
                continue

            slot_tokens = _entry_slot_tokens(entry)
            overlap = tokens & slot_tokens
            if not overlap:
                continue

            # Score: fraction of slot tokens that matched, blended
            # with absolute overlap count so a 2/2 match beats a
            # 1/1 lone-word match. Clamp to keep neural-pool entries
            # rankable alongside.
            confidence = len(overlap) / max(len(slot_tokens), 1)
            score = float(min(0.95, 0.55 + 0.35 * confidence))
            if entry.superseded_at is not None:
                score *= 0.55   # Mirror the supersession demotion.
            candidates.append((entry, score, 0.0))
            if slot_trace_block is not None:
                slot_trace_block.append({
                    "text_preview": entry.text[:80],
                    "source": entry.source,
                    "overlap_tokens": sorted(overlap),
                    "score": round(score, 4),
                    "superseded": entry.superseded_at is not None,
                })

        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[:k]

    # ------------------------------------------------------------------
    # In-memory sync helpers
    # ------------------------------------------------------------------

    def bump_entry_reinforcements(self, db_id: int, delta: int) -> bool:
        """Bump the resident entry's in-memory reinforcement counter to match a
        DB bump, so eviction scoring reflects it without a reload. Returns True
        if the entry was resident (a no-op + False otherwise — e.g. already
        evicted; the DB value stands and is reloaded on next hydrate)."""
        for band in self.bands:
            for e in band.entries:
                if e.db_id == db_id:
                    e.reinforcements += delta
                    return True
        return False

    def reflush_entries(self, db_ids: set[int]) -> int:
        """Re-insert resident entries whose storage rows are gone — a
        connection lost mid-store can roll back an insert whose RETURNING id
        was already handed out (see PostgresStorage._txn), leaving the entry
        holding a phantom db_id. Each hit gets a fresh row + id. Returns the
        number re-flushed."""
        if self.storage is None or not db_ids:
            return 0
        from pseudolife_memory.storage.sync import entry_to_row
        n = 0
        for band in self.bands:
            for e in band.entries:
                if e.db_id in db_ids:
                    e.db_id = self.storage.insert_entry(entry_to_row(e))
                    n += 1
        return n

    def bump_entry_access_count(self, db_id: int, delta: int) -> bool:
        """Bump the resident entry's in-memory access_count to match a DB bump, so
        the save-cadence sync (update_access_counts, in-memory -> DB) reconciles to
        the bumped value instead of clobbering it. Returns True if resident."""
        for band in self.bands:
            for e in band.entries:
                if e.db_id == db_id:
                    e.access_count += delta
                    return True
        return False

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    def rebalance_bands(self) -> int:
        """Seat restored entries within per-band capacity. Returns moves.

        The restore paths — :func:`hydrate_cms`, :meth:`load`, the legacy
        migration — append straight to ``band.entries``, bypassing
        ``store()`` and its capacity check. ``hydrate_cms`` also routes rows
        whose band no longer exists into ``bands[0]``, so a preset rename
        can pile the whole bank into the smallest band.

        That was harmless while capacity eviction deleted (the resident set
        stayed far below capacity). Since eviction started demoting
        instead, the resident set reaches the summed capacity, and draining
        an over-full head band costs one entry per subsequent ``store()``,
        each scoring the whole band and cascading a DB write per hop.

        Walks shallow → deep once, spilling each band's lowest-scoring
        surplus into the next — O(n log n), and each entry moves at most
        once per band. Deliberately moves the objects rather than going
        through :meth:`_relocate`, whose ``store()`` would re-run eviction
        scoring per entry and make this quadratic.

        The deepest band is allowed to finish over capacity when the bank
        holds more rows than the preset can seat. Startup is the wrong
        place to destroy memories the user did not ask to lose; the excess
        is logged and drains through the normal eviction path.
        """
        now = now_seconds()
        moved = 0
        for depth, band in enumerate(self.bands[:-1]):
            overflow = band.size - band.max_entries
            if overflow <= 0:
                continue
            destination = self.bands[depth + 1]
            # Lowest-scoring first, by this band's own retention policy —
            # the same ordering _evict_one would have used.
            spill = heapq.nsmallest(
                overflow, band.entries,
                key=lambda e: band.retention.source_weighted_score(e, now))
            spilled = {id(e) for e in spill}
            band.entries = [e for e in band.entries if id(e) not in spilled]
            band._dirty = True
            for entry in spill:
                entry.bank = destination.name
                destination.entries.append(entry)
                if self.storage is not None and entry.db_id is not None:
                    try:
                        self.storage.update_entry(
                            entry.db_id, band=destination.name)
                    except Exception as exc:  # noqa: BLE001
                        # Cosmetic: the in-memory seating is what matters,
                        # and a stale band column just re-spills identically
                        # on the next hydrate.
                        logger.warning("rebalance write-through failed: %s", exc)
            destination._dirty = True
            moved += overflow

        deepest = self.bands[-1]
        if deepest.size > deepest.max_entries:
            logger.warning(
                "band %r holds %d entries against a capacity of %d after "
                "restore — the bank has more rows than this preset seats. "
                "Left intact rather than truncated at startup; capacity "
                "eviction will drain it.",
                deepest.name, deepest.size, deepest.max_entries,
            )
        if moved:
            self._slot_index_dirty = True
        return moved

    def _relocate(self, entry: MemoryEntry, destination: MIRASBand) -> None:
        """Move ``entry`` into ``destination``, preserving its identity.

        Used by both directions of band movement: selective promotion
        (:meth:`_consolidate`) and forced overflow (:meth:`_on_band_evict`).
        ``destination.store`` mints a fresh :class:`MemoryEntry` with
        defaults, but a relocation must not restate provenance — timestamp
        and access_count especially, since an entry arriving in ``slow``
        must not look newly-created to that tier's eviction scoring. The
        storage row moves with it rather than being re-inserted.

        Note ``destination.store`` may itself overflow and cascade one band
        deeper; that completes before the append, so ``entries[-1]`` is
        still the entry we just placed.

        **All-or-nothing.** Callers prune the source on the strength of this
        returning, so a half-applied move — landed in the destination, still
        live in the source — is the duplicate-``db_id`` state both callers
        exist to avoid. On failure the destination append is rolled back and
        the exception propagates; the entry stays wholly in the source.
        """
        destination.store(
            entry.text,
            entry.embedding.clone(),
            source=entry.source,
            surprise=entry.surprise_score,
        )
        if not destination.entries:
            return
        moved = destination.entries[-1]
        try:
            self._carry_identity(entry, moved, destination)
        except Exception:
            # Undo the append so the move stays all-or-nothing. A cascade
            # triggered by the store above is deliberately NOT undone —
            # that entry moved deeper legitimately and is not duplicated.
            if destination.entries and destination.entries[-1] is moved:
                destination.entries.pop()
                destination._dirty = True
            raise

    def _carry_identity(self, entry: MemoryEntry, moved: MemoryEntry,
                        destination: MIRASBand) -> None:
        """Copy provenance onto the relocated entry. Split out of
        :meth:`_relocate` only so the rollback there has a clean boundary."""
        moved.last_logical_turn = entry.last_logical_turn
        moved.superseded_at = entry.superseded_at
        moved.timestamp = entry.timestamp
        moved.seq = entry.seq
        moved.access_count = entry.access_count
        # MTT retention term (protocols.py: retention_boost * log1p(...)).
        # Dropping it here would silently make ``memory_reinforce`` a no-op
        # for eviction resistance on the daemon, which runs
        # retention_boost=1.0 against the library default of 0.0.
        moved.reinforcements = entry.reinforcements
        # Same text, so the memoised possession cues carry too — a
        # relocation would otherwise re-scan every entry it moves, and at
        # saturation a single store relocates hundreds.
        moved.cue_flags = entry.cue_flags
        # v0.7+ carries structured slots across the move.
        moved.slots = list(entry.slots)
        # Schema v5 (v0.7.6) + MCP-fix: carry the superseding text so a
        # supersede→move sequence doesn't silently drop the correction.
        moved.superseded_by_text = entry.superseded_by_text
        # Schema v6 (Tier C): episode anchoring + tags follow the entry.
        moved.episode_id = entry.episode_id
        moved.episode_title = entry.episode_title
        moved.tags = list(entry.tags)
        # Schema v35: the label pair follows the entry across bands.
        moved.authority = entry.authority
        moved.distortion_tolerance = entry.distortion_tolerance
        moved.db_id = entry.db_id
        if self.storage is not None and entry.db_id is not None:
            # Must not escape. On the demotion path this runs inside
            # ``band.store`` *before* the incoming entry is appended, so a
            # raised transient DB error would abort the caller's store and
            # drop their memory outright — strictly worse than the
            # delete-on-evict path this replaced, which only logged. The
            # in-memory move stands; the row keeps its old band until the
            # next write-through, and ``hydrate_cms`` routes by that column,
            # so the cost of failing here is a misfiled entry, not a lost one.
            try:
                self.storage.update_entry(
                    entry.db_id,
                    band=destination.name,
                    access_count=entry.access_count,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "band relocation write-through failed (%s -> %s): %s",
                    entry.bank, destination.name, exc,
                )

    def _consolidate(self, from_idx: int, to_idx: int) -> None:
        """Promote high-value entries from ``bands[from_idx]`` to ``bands[to_idx]``.

        Source-band thresholds (``promotion_access_count`` / ``promotion_surprise``)
        decide what gets promoted; promoted entries are REMOVED from the
        source to prevent unbounded growth.
        """
        source = self.bands[from_idx]
        destination = self.bands[to_idx]
        ac_threshold = source.promotion_access_count
        surprise_threshold = source.promotion_surprise

        entries = source.entries
        promoted: list[MemoryEntry] = []
        remaining: list[MemoryEntry] = []
        try:
            for entry in entries:
                if (entry.access_count >= ac_threshold
                        or entry.surprise_score > surprise_threshold):
                    self._relocate(entry, destination)
                    promoted.append(entry)
                else:
                    remaining.append(entry)
        finally:
            # Prune in ``finally``: on a mid-loop raise the already-moved
            # entries are live in the destination, and leaving them in the
            # source too is a duplicate sharing one ``db_id`` — which
            # over-counts ``memory_stats`` and makes the next consolidation
            # relocate the stale copy onto the same row. ``_relocate`` is
            # all-or-nothing, so the entry that failed did not move and
            # belongs with the unexamined tail.
            if promoted:
                source.entries = remaining + entries[len(promoted) + len(remaining):]
                source._dirty = True
                # Only on an actual move. Consolidation ticks every store at
                # the shallow tiers, and dirtying unconditionally would
                # rebuild the slot index on nearly every search.
                self._slot_index_dirty = True
                self._consolidation_events.append({
                    "timestamp": time.time(),
                    "from_bank": source.name,
                    "to_bank": destination.name,
                    "entries_moved": len(promoted),
                })
                if len(self._consolidation_events) > self._max_events:
                    self._consolidation_events = self._consolidation_events[-self._max_events:]

    # ------------------------------------------------------------------
    # Persistence — schema v2 (N bands) with v1 migration
    # ------------------------------------------------------------------

    def save(self, directory: str | Path) -> None:
        """Save the CMS state to ``directory/cms_state.pt``.

        Always writes the current ``SCHEMA_VERSION``. Reference bank
        persists itself via ChromaDB so we don't touch it here.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        state = {
            "schema_version": SCHEMA_VERSION,
            "preset_name": self.config.miras.preset,
            "bands": {b.name: b.get_state_dict() for b in self.bands},
            "interaction_count": self._interaction_count,
            "logical_turn_count": self._logical_turn_count,
            "surprise_history": self._surprise_history,
            "consolidation_events": self._consolidation_events,
            "tier_hits": self._tier_hits,
            "tier_queries": self._tier_queries,
            # Episode log (schema v6) — JSON-compatible dict, round-trips
            # losslessly through torch.save. Pre-v6 loaders ignore unknown
            # keys; v6 loaders restore via EpisodeManager.from_dict.
            "episodes": self.episodes.to_dict(),
        }
        torch.save(state, directory / "cms_state.pt")

    # ------------------------------------------------------------------
    # Weights-only persistence (v0.2 — entries live in Postgres)
    # ------------------------------------------------------------------

    def save_weights(self, directory: str | Path) -> None:
        """Atomically persist band weights + counters to ``weights.pt``.

        No entries: in storage mode they are transactional in Postgres,
        so this file is a disposable, retrainable cache. tmp+rename plus
        a single ``.bak`` rotation — see :mod:`utils.atomic_io`.
        """
        from pseudolife_memory.utils.atomic_io import atomic_torch_save
        directory = Path(directory)
        state = {
            "schema_version": SCHEMA_VERSION,
            "kind": "weights",
            "preset_name": self.config.miras.preset,
            "interaction_count": self._interaction_count,
            "logical_turn_count": self._logical_turn_count,
            "surprise_history": self._surprise_history,
            "consolidation_events": self._consolidation_events,
            "tier_hits": self._tier_hits,
            "tier_queries": self._tier_queries,
        }
        atomic_torch_save(state, directory / "weights.pt")

    def load_weights(self, directory: str | Path) -> bool:
        """Restore band weights + counters from ``weights.pt`` (or .bak).

        Returns True on success, False when absent (fresh install) or
        corrupt — the corrupt case additionally sets ``weights_reset``
        so ``stats()`` surfaces that the MLPs restarted from scratch.
        Never touches band entries.
        """
        from pseudolife_memory.utils.atomic_io import (
            WeightsCorrupt, load_with_backup,
        )
        path = Path(directory) / "weights.pt"
        if not path.exists() and not path.with_suffix(".pt.bak").exists():
            return False
        try:
            state, used_backup = load_with_backup(path)
        except WeightsCorrupt as exc:
            logger.warning("weights.pt unrecoverable (%s) — fresh MLPs; "
                           "entries are unaffected.", exc)
            self.weights_reset = True
            return False
        if used_backup:
            logger.warning("weights.pt corrupt — restored from .bak.")
        self._interaction_count = state.get("interaction_count", 0)
        self._logical_turn_count = state.get("logical_turn_count", 0)
        self._surprise_history.update(state.get("surprise_history") or {})
        self._consolidation_events = state.get("consolidation_events") or []
        self._tier_hits.update(state.get("tier_hits") or {})
        self._tier_queries = state.get("tier_queries", 0)
        return True

    def load(self, directory: str | Path) -> None:
        """Load the CMS state.

        Detects schema version and migrates v1 → v2 in place:

        * **v1** (no ``schema_version`` key, has ``instant`` / ``short_term``
          / ``long_term`` top-level keys): the v0.4.x layout. Map the three
          named keys to the first three bands of the current config when
          their names match; otherwise restore by positional index.
        * **v2**: the v0.5+ layout. ``bands`` is a name-keyed dict. Each
          band is restored by name; bands present in the saved state but
          missing from the current config are silently skipped, and
          bands missing from saved state keep their fresh-init weights.
        """
        directory = Path(directory)
        state_path = directory / "cms_state.pt"

        if not state_path.exists():
            legacy_path = directory / "memory_state.pt"
            if legacy_path.exists():
                self._load_legacy_hopfield(legacy_path)
                # The legacy migration dumps ``fast_bank`` into bands[0] and
                # ``slow_bank`` into bands[-1] with no capacity check.
                self.rebalance_bands()
            return

        # weights_only=True: the CMS snapshot is tensors + plain containers, so
        # the safe loader handles it without unpickling arbitrary objects (CWE-502).
        state = torch.load(state_path, weights_only=True, map_location="cpu")
        schema_version = state.get("schema_version", 1)

        if schema_version == 1:
            self._load_schema_v1(state)
        elif schema_version in (2, 3, 4, 5, 6):
            # v3 / v4 / v5 / v6 are all fully backwards-compatible with v2 —
            # each added optional entry fields with sensible defaults:
            # v3: ``last_logical_turn`` + top-level ``chain_residual``,
            # v4: entry-level ``slots`` (default []),
            # v5: entry-level ``superseded_by_text`` (default None),
            # v6: entry-level ``episode_id`` / ``episode_title`` (default None)
            #     and ``tags`` (default []), plus top-level ``episodes``
            #     block (default empty).
            # The shared loader's ``.get(..., default)`` accesses keep
            # pre-v6 files loading cleanly.
            self._load_schema_v2(state)
        else:
            logger.warning(
                "Unknown CMS schema_version=%s in %s — refusing to load to "
                "avoid corrupting state. Bands stay at their fresh-init weights.",
                schema_version, state_path,
            )
            return
        # Both schema loaders replace band entries wholesale, bypassing the
        # capacity check in ``store()``. A save made under a roomier preset
        # therefore leaves bands over their current capacity.
        self.rebalance_bands()

    def _load_schema_v1(self, state: dict) -> None:
        """v0.4.x layout: top-level ``instant`` / ``short_term`` / ``long_term``.

        The v0.4.x state dicts have the same per-band shape we still use
        in v0.5 (``memory_state`` / ``optimizer_state`` / ``surprise_ema``
        / ``entries``) — the only thing that changed is how they're keyed
        in the parent dict. Map by band name when the names line up,
        positional otherwise.
        """
        logger.info("Migrating CMS state from schema v1 → v2.")
        legacy_keys = ["instant", "short_term", "long_term"]
        for idx, key in enumerate(legacy_keys):
            if key not in state:
                continue
            if idx >= len(self.bands):
                # Saved state has more banks than the current config; we
                # cannot route the extras anywhere sensible.
                logger.warning(
                    "v1 state has %r but current config has only %d bands. "
                    "Dropping %r.", key, len(self.bands), key,
                )
                continue
            target = self.bands[idx] if self.bands[idx].name == key else \
                next((b for b in self.bands if b.name == key), self.bands[idx])
            try:
                target.load_state_dict(state[key])
            except Exception as exc:
                logger.warning(
                    "Failed to restore band %r from v1 state: %s. "
                    "Memory weights kept at fresh init.", key, exc,
                )

        self._interaction_count = state.get("interaction_count", 0)
        self._surprise_history = {
            b.name: state.get("surprise_history", {}).get(b.name, [])
            for b in self.bands
        }
        self._consolidation_events = state.get("consolidation_events", [])
        # Band entries were wholesale replaced without going through
        # store() — a previously-built slot index must not survive.
        self._slot_index_dirty = True

    def _load_schema_v2(self, state: dict) -> None:
        """v0.5+ layout: ``bands`` keyed by band name."""
        saved_bands = state.get("bands", {})
        for band in self.bands:
            if band.name in saved_bands:
                try:
                    band.load_state_dict(saved_bands[band.name])
                except Exception as exc:
                    logger.warning(
                        "Failed to restore band %r: %s. "
                        "Memory weights kept at fresh init.", band.name, exc,
                    )
            # else: this band wasn't in the saved state (e.g. config bumped
            # to a longer-band preset). Leave fresh weights in place.

        # Saved bands whose name no longer exists in the configured preset
        # (e.g. a continuum-era save loaded under the flat default) route
        # into the first band instead of being silently dropped — the same
        # fallback hydrate_cms applies to storage rows. Before 2026-08-15
        # this path lost every entry of a renamed layout. load() rebalances
        # afterwards, so over-capacity seating is handled there.
        configured = {b.name for b in self.bands}
        first = self.bands[0]
        for name, saved in saved_bands.items():
            if name in configured:
                continue
            entries = saved.get("entries", [])
            logger.warning(
                "Saved band %r is not in the configured preset — routing "
                "its %d entries into %r.", name, len(entries), first.name,
            )
            for e in entries:
                try:
                    first.entries.append(MemoryEntry(
                        text=e["text"],
                        embedding=e["embedding"].to(first.device),
                        surprise_score=e["surprise_score"],
                        timestamp=e["timestamp"],
                        access_count=e["access_count"],
                        source=e.get("source", ""),
                        bank=first.name,
                        superseded_at=e.get("superseded_at"),
                        superseded_by_text=e.get("superseded_by_text"),
                        last_logical_turn=e.get("last_logical_turn"),
                        slots=e.get("slots", []),
                        episode_id=e.get("episode_id"),
                        episode_title=e.get("episode_title"),
                        tags=list(e.get("tags") or []),
                        authority=e.get("authority"),
                        distortion_tolerance=e.get("distortion_tolerance"),
                    ))
                except Exception as exc:  # noqa: BLE001 — one bad entry
                    logger.warning("Skipping unrestorable entry from saved "
                                   "band %r: %s", name, exc)
            first._dirty = True

        self._interaction_count = state.get("interaction_count", 0)
        # v3 fields — back-compat defaults preserve v2 behaviour.
        self._logical_turn_count = state.get("logical_turn_count", 0)
        self._surprise_history = {
            b.name: state.get("surprise_history", {}).get(b.name, [])
            for b in self.bands
        }
        self._consolidation_events = state.get("consolidation_events", [])
        # Per-tier instrumentation counters round-trip so usage stats survive
        # restarts — important for measuring whether deep tiers actually help.
        self._tier_hits = {
            b.name: state.get("tier_hits", {}).get(b.name, 0)
            for b in self.bands
        }
        self._tier_queries = state.get("tier_queries", 0)
        # v6 episode log — pre-v6 saves have no ``episodes`` key, in which
        # case from_dict returns an empty manager.
        self.episodes = EpisodeManager.from_dict(state.get("episodes") or {})
        # Band entries were wholesale replaced without going through
        # store() — a previously-built slot index must not survive.
        self._slot_index_dirty = True

    def _load_legacy_hopfield(self, path: Path) -> None:
        """Migrate from the v0.3.x Hopfield memory format.

        Reaches further back than v1 — pre-CMS, before the bank chain
        existed. Treats ``fast_bank`` as the first MIRAS band and
        ``slow_bank`` as the last; everything in between is left at
        fresh init.
        """
        try:
            state = torch.load(path, weights_only=True, map_location="cpu")
            first_band = self.bands[0]
            last_band = self.bands[-1]

            for e in state.get("fast_bank", {}).get("entries", []):
                first_band.entries.append(MemoryEntry(
                    text=e["text"],
                    embedding=e["embedding"],
                    timestamp=e.get("timestamp", time.time()),
                    access_count=e.get("access_count", 0),
                    surprise_score=e.get("surprise_score", 0.0),
                    source=e.get("source", ""),
                    bank=first_band.name,
                ))
            first_band._dirty = True

            if last_band is not first_band:
                for e in state.get("slow_bank", {}).get("entries", []):
                    last_band.entries.append(MemoryEntry(
                        text=e["text"],
                        embedding=e["embedding"],
                        timestamp=e.get("timestamp", time.time()),
                        access_count=e.get("access_count", 0),
                        surprise_score=e.get("surprise_score", 0.0),
                        source=e.get("source", ""),
                        bank=last_band.name,
                    ))
                last_band._dirty = True

            self._interaction_count = state.get("interaction_count", 0)
            # Entries appended without going through store() — invalidate
            # any previously-built slot index.
            self._slot_index_dirty = True
        except Exception:
            pass  # Silently fail legacy migration — old format may be malformed.

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Clear all neural bands. Does NOT clear reference bank."""
        for band in self.bands:
            band.entries.clear()
            band._dirty = True
            band.surprise_ema = 0.0
        self._slot_index_dirty = True
        self._interaction_count = 0
        self._surprise_history = {b.name: [] for b in self.bands}
        self._consolidation_events = []
        # Tier C — reset the episode log too so test fixtures get clean
        # bookkeeping on every ``clear``. Without this, ``pristine_service``
        # leaks episodes from earlier tests in the same module.
        self.episodes = EpisodeManager()

    def delete_entries(
        self,
        *,
        text: str | None = None,
        substring: str | None = None,
        source: str | None = None,
        episode: str | None = None,
        tag: str | None = None,
    ) -> list[str]:
        """Remove entries from every band matching any provided filter.

        At least one of ``text`` / ``substring`` / ``source`` /
        ``episode`` / ``tag`` must be provided — refuses to
        delete-everything implicitly. Filters combine with OR (an entry
        matching any filter is dropped). Returns the list of removed
        entry texts.

        Marks each affected band's pattern matrix dirty so the next
        retrieve rebuilds without the gone entries.
        """
        if all(v is None for v in (text, substring, source, episode, tag)):
            raise ValueError(
                "delete_entries requires at least one of: "
                "text, substring, source, episode, tag.",
            )

        # Normalise the tag filter to match how stored tags are keyed.
        tag_norm = tag.strip().lower() if isinstance(tag, str) else None

        def _matches(entry: MemoryEntry) -> bool:
            if text is not None and entry.text == text:
                return True
            if substring is not None and substring in entry.text:
                return True
            if source is not None and entry.source == source:
                return True
            if episode is not None and entry.episode_id == episode:
                return True
            if tag_norm is not None and tag_norm in entry.tags:
                return True
            return False

        removed: list[str] = []
        removed_ids: list[int] = []
        for band in self.bands:
            kept: list[MemoryEntry] = []
            band_changed = False
            for entry in band.entries:
                if _matches(entry):
                    removed.append(entry.text)
                    if entry.db_id is not None:
                        removed_ids.append(entry.db_id)
                    band_changed = True
                else:
                    kept.append(entry)
            if band_changed:
                band.entries = kept
                band._dirty = True
        if removed:
            self._slot_index_dirty = True
        if self.storage is not None and removed_ids:
            self.storage.delete_entry_ids(removed_ids)
        return removed

    def _on_band_evict(self, entry: MemoryEntry, band_idx: int | None = None) -> None:
        """Capacity eviction: demote to the next band, destroy only at the end.

        Before 2026-07-25 this deleted the entry and its storage row. But
        the only *other* way out of a band is promotion, which requires
        ``access_count >= N or surprise > theta`` — so an unsurprising,
        never-retrieved entry died in the head band while the deeper bands
        sat nearly empty. Measured on the LongMemEval ``s`` replay: 31.1% of
        stored turns discarded at **6.4%** total capacity utilisation, and
        answer-evidence turns fared worse than average (37.5% evicted)
        because eviction ranks on novelty and restated facts are
        unsurprising by construction.

        Handing the evictee down makes total capacity the real bound, which
        is what the layout always claimed. Only the deepest band's overflow
        is a true drop. ``band_idx=None`` (a hand-wired callback) keeps the
        old delete-on-evict behaviour.

        Under the ``flat`` preset (the default since 2026-08-15) there is
        no deeper band, so every capacity eviction is a true drop by
        design — a retention-scored delete that only fires at genuine
        total capacity (the exact arm the flat-band verdict measured as
        tying the continuum under forced eviction). True drops are
        counted (``stats()["true_drops"]``) and logged so a bank under
        real capacity pressure is visible, never silent.
        """
        self._slot_index_dirty = True
        if band_idx is not None and band_idx + 1 < len(self.bands):
            self._relocate(entry, self.bands[band_idx + 1])
            return
        self._true_drops += 1
        logger.info("capacity eviction (true drop #%d): %r",
                    self._true_drops, entry.text[:80])
        if self.storage is not None and entry.db_id is not None:
            try:
                self.storage.delete_entry_ids([entry.db_id])
            except Exception as exc:  # noqa: BLE001
                logger.warning("evict write-through failed: %s", exc)

    def stats(self) -> dict:
        """Memory statistics.

        Returns both the v0.4.x flat fields (``instant_bank_size``, etc.)
        for backwards compatibility with the existing frontend AND a new
        ``bands`` array describing every band in the continuum.
        """
        total_queries = max(1, self._tier_queries)
        bands_summary = [
            {
                "name": b.name,
                "size": b.size,
                "capacity": b.max_entries,
                "update_interval": b.update_interval,
                "retention_policy": b.retention.name,
                # v0.6 instrumentation: fraction of retrievals where this
                # tier contributed at least one entry to the merged result.
                # Lets us measure whether deep continua actually help.
                "hit_rate": round(
                    self._tier_hits.get(b.name, 0) / total_queries, 4
                ),
                "hit_count": self._tier_hits.get(b.name, 0),
            }
            for b in self.bands
        ]
        result = {
            "bands": bands_summary,
            "preset": self.config.miras.preset,
            "total_memories": self.total_memories,
            "interaction_count": self._interaction_count,
            "logical_turn_count": self._logical_turn_count,
            "retrieval_queries": self._tier_queries,
            # Entries destroyed by capacity eviction since startup (no
            # deeper band to demote into). 0 until the store genuinely
            # fills; a growing number is the signal to raise capacity or
            # curate.
            "true_drops": self._true_drops,
            # v0.2: True when weights.pt (and .bak) failed to load and the
            # band MLPs restarted fresh. Entries are unaffected.
            "weights_reset": self.weights_reset,
            # Slot-index shadow-check divergences since startup. Non-zero
            # means a mutation path bypassed the index maintenance
            # contract and was caught + repaired at query time.
            "slot_index_shadow_divergences": self._slot_index_shadow_divergences,
            # v0.4.x flat fields. Populate from the named banks when they
            # exist (titans preset), zero otherwise.
            "instant_bank_size": self.instant.size if self.instant else 0,
            "instant_bank_capacity": self.instant.max_entries if self.instant else 0,
            "short_term_bank_size": self.short_term.size if self.short_term else 0,
            "short_term_bank_capacity": self.short_term.max_entries if self.short_term else 0,
            "long_term_bank_size": self.long_term.size if self.long_term else 0,
            "long_term_bank_capacity": self.long_term.max_entries if self.long_term else 0,
        }
        if self.reference:
            ref_stats = self.reference.stats()
            result["reference_bank_size"] = ref_stats["reference_bank_size"]
            result["reference_document_count"] = ref_stats["reference_document_count"]
        else:
            result["reference_bank_size"] = 0
            result["reference_document_count"] = 0
        return result


def _recency_weight(timestamp: float, half_life: float = 3600.0) -> float:
    """Exponential recency weight."""
    age = max(time.time() - timestamp, 0.0)
    return 2.0 ** (-age / half_life)

