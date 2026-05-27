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

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import torch

from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.memory.miras.band import MIRASBand, build_band
from pseudolife_memory.memory.meta_filter import is_meta_statement
from pseudolife_memory.memory.contradiction import detect_contradictions, decay_contradicted_entries
from pseudolife_memory.memory.slots import extract_slots
from pseudolife_memory.utils.config import MemoryConfig

if TYPE_CHECKING:
    from pseudolife_memory.memory.nli import NLIContradictionScorer

logger = logging.getLogger(__name__)


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
SCHEMA_VERSION = 5


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
    ) -> None:
        self.config = config
        self._nli_scorer = nli_scorer
        self._nli_candidate_cap: int = (
            getattr(config.nli, "max_candidates", 8) if hasattr(config, "nli") else 8
        )
        device = "cuda" if torch.cuda.is_available() else "cpu"

        # ── Construct the N-band chain from the MIRAS config ──────────────────
        # The default ``titans`` preset produces 3 bands with the same shapes
        # as the v0.4.x ``TitansConfig`` defaults, so behaviour is unchanged
        # for users who don't opt into a different preset.
        self.bands: list[MIRASBand] = [
            build_band(spec, embedding_dim=config.embedding_dim, device=device)
            for spec in config.miras.bands
        ]
        if not self.bands:
            raise ValueError(
                "ContinuumMemorySystem requires at least one MIRAS band. "
                "Check memory.miras.bands in config.yaml."
            )

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
        # ticks per :meth:`store`) so agentic deployments that emit many
        # bookkeeping stores per logical turn (tool_call + tool_result +
        # llm_thinking + agent_action per agent step) don't blow through six
        # tiers of consolidation in a single user-facing turn.  When the
        # caller wraps each logical turn in
        # :meth:`begin_logical_turn` / :meth:`end_logical_turn`,
        # consolidation runs only on logical boundaries.  When the caller
        # doesn't (e.g. v0.5.x chat flow), the original per-store
        # consolidation in :meth:`store` keeps working unchanged.
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

        # Rolling coreference anchor for slot extraction (v0.7+). Tracks
        # the last named entity / type referent so that "I gave him away"
        # can attach a gender slot to the right entity even when the text
        # itself doesn't name it.
        self._last_entity_seen: str | None = None

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

        Returns:
            Tuple of ``(was_stored, surprise_score)``.
        """
        if is_meta_statement(text, role=source):
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
        contradiction_found = False
        for band in self.bands:
            contradicted = detect_contradictions(
                text, embedding, band.entries,
                similarity_threshold=0.7, device=device,
                nli_scorer=self._nli_scorer,
                nli_candidate_cap=self._nli_candidate_cap,
            )
            if contradicted:
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

        # ── Land the write in the first band ──────────────────────────────────
        self.bands[0].store(text, embedding, source=source, surprise=overall_surprise)
        if self.bands[0].entries:
            entry = self.bands[0].entries[-1]
            # Stamp logical turn (schema v3 — None when no turn open).
            if self._in_logical_turn:
                entry.last_logical_turn = self._logical_turn_count + 1
            # Extract structured slots (schema v4). ``last_entity_context``
            # threads recent-entity coreference across messages — letting
            # "I gave him away" inherit the previous turn's "Jacque" anchor.
            slots = extract_slots(
                text,
                last_entity_context=self._last_entity_seen,
            )
            entry.slots = [
                (s.entity, s.attribute, s.value, s.polarity)
                for s in slots
            ]
            # Update the rolling coreference anchor — last text's first
            # named entity (if any) becomes the default referent for the
            # next message's pronouns.
            for ent, attr, _val, _pol in entry.slots:
                if attr in ("name", "type"):
                    self._last_entity_seen = ent
                    break
        self._interaction_count += 1

        # ── Walk the promotion chain (band[i] → band[i+1]) ────────────────────
        # When the caller has wrapped the agent step in
        # :meth:`begin_logical_turn` / :meth:`end_logical_turn`, defer the
        # consolidation to ``end_logical_turn`` so an agentic step that emits
        # many bookkeeping stores doesn't blow through six tiers in one go.
        # Otherwise fall back to the v0.5.x per-store consolidation cadence so
        # the chat flow that doesn't know about logical turns keeps working.
        if not self._in_logical_turn:
            self._consolidate_eligible(self._interaction_count)

        return True, overall_surprise

    # ------------------------------------------------------------------
    # Logical-turn API — agentic-friendly consolidation cadence
    # ------------------------------------------------------------------

    def begin_logical_turn(self) -> None:
        """Mark the start of a logical turn (one user message / agent step).

        While a logical turn is open, :meth:`store` defers consolidation —
        an agent doing 30 tool calls within one user turn shouldn't trigger
        consolidation 30 times. Pair every call to ``begin_logical_turn``
        with one to :meth:`end_logical_turn`.
        """
        self._in_logical_turn = True

    def end_logical_turn(self) -> None:
        """Close the logical turn and run any eligible consolidations.

        Promotion eligibility is keyed off the *logical-turn* counter so
        each destination band fires every ``update_interval`` logical turns,
        not every ``update_interval`` raw stores.
        """
        self._logical_turn_count += 1
        self._consolidate_eligible(self._logical_turn_count)
        self._in_logical_turn = False

    def _consolidate_eligible(self, counter: int) -> None:
        """Walk the promotion chain, firing each tier whose interval is hit."""
        for i in range(len(self.bands) - 1):
            destination = self.bands[i + 1]
            if counter % destination.update_interval == 0:
                self._consolidate(i, i + 1)

    # ------------------------------------------------------------------
    # Retrieval path
    # ------------------------------------------------------------------

    def retrieve_with_trace(
        self,
        query_embedding: torch.Tensor,
        top_k: int | None = None,
        *,
        bands: list[str] | None = None,
        sources: list[str] | None = None,
        min_logical_turn: int | None = None,
    ) -> tuple[RetrievalResult, dict]:
        """Like :meth:`retrieve` but also returns a structured trace dict
        describing exactly what happened — per-tier scores + per-entry
        breakdown of recency / source-weight / chain-residual contributions.

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
                "min_logical_turn": min_logical_turn,
            },
            "tiers": [],
            "chain_residual": {"enabled": False, "synthetic_hits": []},
            "reference_pool": [],
            "final_topk": [],
        }
        result = self.retrieve(
            query_embedding,
            top_k=top_k,
            bands=bands,
            sources=sources,
            min_logical_turn=min_logical_turn,
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
        min_logical_turn: int | None = None,
        query_text: str | None = None,
        min_score: float | None = None,
        disable_recency_boost: bool = False,
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
        # Set ``config.hide_superseded = True`` to restore the v0.7.2
        # filter behaviour. The opposite-named config field
        # ``show_superseded`` (legacy v0.6 name, default False) is
        # honoured as a no-op for backwards-compatible config files.
        SUPERSEDED_SCORE_MULT = 0.55

        def _source_mult(entry: MemoryEntry) -> float:
            return ASSISTANT_SCORE_MULT if entry.source == "assistant" else 1.0

        k = top_k or self.config.top_k
        ref_k = getattr(self.config, "ref_top_k", 3)

        # v0.7.3: superseded entries are included in retrieval by
        # default. Set ``config.hide_superseded = True`` to restore
        # the v0.7.2-and-earlier filter behaviour. The legacy
        # ``show_superseded`` config field (default False) is
        # deliberately ignored — its semantics were the cause of the
        # cat-Jacque category-query failure, where the only entry
        # mentioning the category word was hard-filtered after a
        # later supersession event.
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

        # ── Pool 1: neural memories — N bands, recency-weighted by depth ──────
        # The earlier the band, the stronger the recency boost. We schedule
        # the boost coefficient as a linear ramp: bands[0] gets boost=0.4 with
        # half-life 1 hour, bands[-1] gets boost=0 (no recency mod). Half-life
        # scales geometrically with depth.
        neural: list[tuple[MemoryEntry, float, float]] = []
        seen_texts: set[str] = set()
        n = len(self.bands)
        hit_band_names: set[str] = set()

        for depth, band in enumerate(self.bands):
            if band_filter is not None and band.name not in band_filter:
                if _trace is not None:
                    _trace["tiers"].append({
                        "name": band.name, "depth": depth, "filtered_out": True,
                        "candidates": [],
                    })
                continue

            # Ramp from (0.4, 3600s) at depth=0 down to (0.0, ∞) at depth=n-1.
            if n == 1 or disable_recency_boost:
                boost, half_life = 0.0, float("inf")
            else:
                frac = depth / (n - 1)
                boost = 0.4 * (1.0 - frac)
                # Geometric half-life: 1h → 2h → 4h … (skip recency at
                # depth=n-1 anyway because boost=0).
                half_life = 3600.0 * (2.0 ** depth)

            band_result = band.retrieve(query_embedding, top_k=k)
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
                    seen_texts.add(entry.text)
                    hit_band_names.add(band.name)
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
                _trace=_trace,
            )
            for entry, score, surprise in slot_hits:
                neural.append((entry, score, surprise))
                seen_texts.add(entry.text)
                if entry.bank:
                    hit_band_names.add(entry.bank)

        neural.sort(key=lambda x: x[1], reverse=True)
        neural = neural[:k]

        # ── Optional: HOPE-style chained-residual read ─────────────────────────
        # When ``config.miras.chain_residual`` is enabled (currently only the
        # ``continuum`` preset), flow the query forward through every band's
        # MLP — each tier sees its predecessor's output added to the query,
        # producing a progressively-abstracted representation. The final
        # chained output is then matched against every entry's embedding to
        # surface "neural-pattern" hits the per-band retrieval missed. We
        # ``stop_gradient`` between tiers so each tier's local optimisation
        # stays independent — no BPTT, no training-time complexity.
        if getattr(self.config.miras, "chain_residual", False) and self.bands:
            pre_chain = len(neural)
            self._inject_chained_hits(
                query_embedding, neural, seen_texts,
                top_k=k, source_filter=source_filter,
                min_logical_turn=min_logical_turn, keep=_keep,
            )
            if _trace is not None:
                # Anything appended to ``neural`` after pre_chain came from
                # the chained read.
                _trace["chain_residual"]["enabled"] = True
                _trace["chain_residual"]["synthetic_hits"] = [
                    {
                        "text_preview": e.text[:80] + ("…" if len(e.text) > 80 else ""),
                        "score": round(float(s), 4),
                    }
                    for e, s, _ in neural[pre_chain:]
                ]

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
        if _trace is not None:
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
            return RetrievalResult(entries=[], scores=[], surprises=[])

        entries, scores, surprises = zip(*combined)
        return RetrievalResult(
            entries=list(entries),
            scores=list(scores),
            surprises=list(surprises),
        )

    def _inject_chained_hits(
        self,
        query_embedding: "torch.Tensor",
        neural: list[tuple["MemoryEntry", float, float]],
        seen_texts: set[str],
        *,
        top_k: int,
        source_filter: set[str] | None,
        min_logical_turn: int | None,
        keep,
    ) -> None:
        """HOPE-style: flow the query through every band's MLP and use the
        chained output to surface additional entries.

        ``stop_gradient`` on the per-tier outputs keeps tiers independently
        optimised — the chained read is purely a retrieval-time signal.
        Mutates ``neural`` in place by appending new ``(entry, score, surprise)``
        tuples whose post-multiplier score clears the same MIN_SCORE threshold
        the parallel-pool scoring used.
        """
        import torch.nn.functional as F  # noqa: PLC0415

        device = self.bands[0].device
        q = query_embedding.to(device)
        q = F.normalize(q.unsqueeze(0), p=2, dim=1).squeeze(0)

        # Forward chain: each tier sees q + previous-tier output, with
        # stop_gradient on the previous-tier contribution.
        chained = q
        for band in self.bands:
            band.memory.eval()
            with torch.no_grad():
                tier_out = band.memory(chained)
                tier_out = F.normalize(tier_out.unsqueeze(0), p=2, dim=1).squeeze(0)
            # Residual: average with running chained vector to keep magnitude
            # stable across N hops.
            chained = F.normalize(
                (chained + tier_out).unsqueeze(0), p=2, dim=1,
            ).squeeze(0).detach()

        # Match the final chained representation against every band's stored
        # patterns. The score is plain cosine against the chained vector —
        # no further depth-weighting because the chain itself already
        # encodes a slow-band-biased view.
        MIN_SCORE = 0.25
        for band in self.bands:
            if not band.entries:
                continue
            if band._dirty:
                band._rebuild_pattern_matrix()
            if band._pattern_matrix is None:
                continue
            scores = band._pattern_matrix @ chained
            for entry, score_t in zip(band.entries, scores.tolist()):
                if entry.text in seen_texts or not keep(entry):
                    continue
                if source_filter is not None and entry.source not in source_filter:
                    continue
                if min_logical_turn is not None:
                    et = getattr(entry, "last_logical_turn", None)
                    if et is None or et < min_logical_turn:
                        continue
                if score_t < MIN_SCORE:
                    continue
                neural.append((entry, float(score_t), entry.surprise_score))
                seen_texts.add(entry.text)

        # Re-sort and cap to top_k after the chained additions.
        neural.sort(key=lambda x: x[1], reverse=True)
        del neural[top_k:]

    def compute_surprise(self, embedding: torch.Tensor) -> float:
        """Aggregate surprise across all bands (min — anything any band
        already knows isn't surprising)."""
        return min(b.compute_surprise(embedding) for b in self.bands)

    # ------------------------------------------------------------------
    # Slot fact-sheet (v0.7+)
    # ------------------------------------------------------------------

    def _slot_query_pool(
        self,
        query_text: str,
        k: int,
        seen_texts: set[str],
        source_filter: set[str] | None = None,
        band_filter: set[str] | None = None,
        _trace: dict | None = None,
    ) -> list[tuple["MemoryEntry", float, float]]:
        """Pull entries via slot-token overlap with the query.

        Walks every band's entries, looks at each entry's
        ``(entity, attribute, value, polarity)`` slot triples, and
        scores the entry by how much of the query text's content
        tokens overlap with slot entities or values. Returns the
        top-k hits, score-sorted, with the same ``(entry, score,
        surprise)`` tuple shape the neural pool emits.

        Designed to be a *belt and suspenders* second channel: the
        neural pool catches paraphrastic / fuzzy matches, the slot
        pool catches exact-fact lookups (category vs entity queries,
        attribute mentions). When both pools hit the same entry the
        ``seen_texts`` dedup keeps it from double-counting.
        """
        import re as _re  # noqa: PLC0415

        # Tokenise the query: lowercase content words ≥ 3 chars,
        # excluding obvious stop-words that would over-match.
        STOP = {
            "the", "and", "you", "your", "for", "have", "had", "has",
            "with", "from", "this", "that", "what", "where", "when",
            "who", "why", "how", "are", "was", "were", "been", "being",
            "into", "onto", "out", "did", "does", "doing", "say", "said",
            "can", "will", "would", "should", "could", "may", "might",
            "any", "some", "all", "not", "yes", "tell", "tells", "told",
        }
        tokens = {
            t for t in _re.findall(r"[a-z']{3,}", query_text.lower())
            if t not in STOP
        }
        if not tokens:
            return []

        slot_trace_block: list[dict] | None = None
        if _trace is not None:
            slot_trace_block = []
            _trace["slot_pool"] = slot_trace_block

        candidates: list[tuple["MemoryEntry", float, float]] = []
        for band in self.bands:
            if band_filter is not None and band.name not in band_filter:
                continue
            for entry in band.entries:
                if entry.text in seen_texts:
                    continue
                if source_filter is not None and entry.source not in source_filter:
                    continue
                if not entry.slots:
                    continue

                # Collect tokens from slot entity + value (attribute is
                # usually a structural label like "type"/"breed" — less
                # informative for query matching).
                slot_tokens: set[str] = set()
                for s_entity, _s_attr, s_value, _polarity in entry.slots:
                    for t in _re.findall(
                        r"[a-z']{3,}", (s_entity + " " + s_value).lower(),
                    ):
                        if t not in STOP:
                            slot_tokens.add(t)

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

    def slot_view_for_entries(
        self, entries: "list[MemoryEntry]",
    ) -> "dict[str, dict[str, str]]":
        """Merge slot triples from a list of entries into a per-entity view.

        Used by the context builder at chat time to inject a structured
        fact sheet alongside the prose-retrieved memories. Newer entries
        override older ones on the same ``(entity, attribute)``; loss /
        negation polarity wins.
        """
        from pseudolife_memory.memory.slots import Slot, merge_slots_view  # noqa: PLC0415

        # Sort by timestamp ascending so later entries override earlier
        # ones during the merge.
        ordered = sorted(entries, key=lambda e: e.timestamp)
        slot_lists: list[list[Slot]] = []
        for e in ordered:
            slot_lists.append([
                Slot(entity=t[0], attribute=t[1], value=t[2], polarity=t[3])
                for t in (e.slots or [])
            ])
        return merge_slots_view(slot_lists)

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

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

        promoted: list[MemoryEntry] = []
        remaining: list[MemoryEntry] = []
        for entry in source.entries:
            if entry.access_count >= ac_threshold or entry.surprise_score > surprise_threshold:
                destination.store(
                    entry.text,
                    entry.embedding.clone(),
                    source=entry.source,
                    surprise=entry.surprise_score,
                )
                # Propagate the logical-turn stamp + supersession flag to
                # the freshly-promoted entry — destination.store creates a
                # new MemoryEntry with defaults, but the promotion is a
                # pure relocation: the entry's provenance shouldn't change.
                # Preserve timestamp + access_count for the same reason —
                # an entry promoted to ``slow`` shouldn't suddenly look
                # newly-created at the slow tier's eviction scoring.
                if destination.entries:
                    promoted_copy = destination.entries[-1]
                    promoted_copy.last_logical_turn = entry.last_logical_turn
                    promoted_copy.superseded_at = entry.superseded_at
                    promoted_copy.timestamp = entry.timestamp
                    promoted_copy.access_count = entry.access_count
                    # v0.7+ also carries structured slots across promotion.
                    promoted_copy.slots = list(entry.slots)
                    # Schema v5 (v0.7.6) + MCP-fix: also propagate the
                    # superseding text so the supersede→promote sequence
                    # doesn't silently drop the correction. The original
                    # cat-Jacque ship added the field on MemoryEntry but
                    # missed this propagation path — see upstream issue.
                    promoted_copy.superseded_by_text = entry.superseded_by_text
                promoted.append(entry)
            else:
                remaining.append(entry)

        if promoted:
            source.entries = remaining
            source._dirty = True
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
            "chain_residual": getattr(self.config.miras, "chain_residual", False),
            "bands": {b.name: b.get_state_dict() for b in self.bands},
            "interaction_count": self._interaction_count,
            "logical_turn_count": self._logical_turn_count,
            "surprise_history": self._surprise_history,
            "consolidation_events": self._consolidation_events,
            "tier_hits": self._tier_hits,
            "tier_queries": self._tier_queries,
        }
        torch.save(state, directory / "cms_state.pt")

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
            return

        state = torch.load(state_path, weights_only=False, map_location="cpu")
        schema_version = state.get("schema_version", 1)

        if schema_version == 1:
            self._load_schema_v1(state)
        elif schema_version in (2, 3, 4, 5):
            # v3 / v4 / v5 are all fully backwards-compatible with v2 —
            # each added optional entry fields with sensible defaults:
            # v3: ``last_logical_turn`` + top-level ``chain_residual``,
            # v4: entry-level ``slots`` (default []),
            # v5: entry-level ``superseded_by_text`` (default None).
            # The shared loader's ``.get(..., default)`` accesses keep
            # pre-v5 files loading cleanly.
            self._load_schema_v2(state)
        else:
            logger.warning(
                "Unknown CMS schema_version=%s in %s — refusing to load to "
                "avoid corrupting state. Bands stay at their fresh-init weights.",
                schema_version, state_path,
            )

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

    def _load_legacy_hopfield(self, path: Path) -> None:
        """Migrate from the v0.3.x Hopfield memory format.

        Reaches further back than v1 — pre-CMS, before the bank chain
        existed. Treats ``fast_bank`` as the first MIRAS band and
        ``slow_bank`` as the last; everything in between is left at
        fresh init.
        """
        try:
            state = torch.load(path, weights_only=False, map_location="cpu")
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
            band.memory.init_weights()
            band.surprise_ema = 0.0
        self._interaction_count = 0
        self._surprise_history = {b.name: [] for b in self.bands}
        self._consolidation_events = []

    def delete_entries(
        self,
        *,
        text: str | None = None,
        substring: str | None = None,
        source: str | None = None,
    ) -> list[str]:
        """Remove entries from every band matching any provided filter.

        At least one of ``text`` / ``substring`` / ``source`` must be
        provided — refuses to delete-everything implicitly. Filters
        combine with OR (an entry matching any filter is dropped).
        Returns the list of removed entry texts.

        Marks each affected band's pattern matrix dirty so the next
        retrieve rebuilds without the gone entries.
        """
        if text is None and substring is None and source is None:
            raise ValueError(
                "delete_entries requires at least one of: "
                "text, substring, source.",
            )

        def _matches(entry: MemoryEntry) -> bool:
            if text is not None and entry.text == text:
                return True
            if substring is not None and substring in entry.text:
                return True
            if source is not None and entry.source == source:
                return True
            return False

        removed: list[str] = []
        for band in self.bands:
            kept: list[MemoryEntry] = []
            band_changed = False
            for entry in band.entries:
                if _matches(entry):
                    removed.append(entry.text)
                    band_changed = True
                else:
                    kept.append(entry)
            if band_changed:
                band.entries = kept
                band._dirty = True
        return removed

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
                "base_lr": b.base_lr,
                "objective": b.objective.name,
                "update_rule": b.update_rule.name,
                "retention_policy": b.retention.name,
                "memory_module": type(b.memory).__name__,
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
            "chain_residual": getattr(self.config.miras, "chain_residual", False),
            "total_memories": self.total_memories,
            "interaction_count": self._interaction_count,
            "logical_turn_count": self._logical_turn_count,
            "retrieval_queries": self._tier_queries,
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

    def introspection(self) -> dict:
        """Detailed introspection data for visualisation."""
        bank_health = {}
        for band in self.bands:
            history = self._surprise_history.get(band.name, [])
            avg_surprise = sum(history) / len(history) if history else 0.0
            bank_health[band.name] = {
                "avg_surprise": round(avg_surprise, 3),
                "entry_count": band.size,
                "surprise_ema": round(band.surprise_ema, 3),
            }

        timeline = []
        for band in self.bands:
            recent = sorted(band.entries, key=lambda e: e.timestamp, reverse=True)[:20]
            for entry in recent:
                timeline.append({
                    "text_preview": entry.text[:80] + ("..." if len(entry.text) > 80 else ""),
                    "bank": band.name,
                    "surprise": round(entry.surprise_score, 3),
                    "timestamp": entry.timestamp,
                    "source": entry.source,
                })
        timeline.sort(key=lambda x: x["timestamp"], reverse=True)
        timeline = timeline[:50]

        return {
            "surprise_history": self._surprise_history,
            "bank_health": bank_health,
            "consolidation_events": self._consolidation_events[-20:],
            "memory_timeline": timeline,
            "interaction_count": self._interaction_count,
            "preset": self.config.miras.preset,
        }


def _recency_weight(timestamp: float, half_life: float = 3600.0) -> float:
    """Exponential recency weight."""
    age = max(time.time() - timestamp, 0.0)
    return 2.0 ** (-age / half_life)
