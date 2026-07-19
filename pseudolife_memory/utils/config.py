"""Configuration loading and management."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EmbeddingConfig:
    model_name: str = "all-MiniLM-L6-v2"
    device: str = "cuda"
    batch_size: int = 64
    # "torch" (default) or "onnx" — onnxruntime via sentence-transformers'
    # native backend (needs optimum[onnxruntime]). ~3x faster single-text
    # encode on CPU with bit-identical embeddings; falls back to torch with
    # a warning when the backend can't load.
    backend: str = "torch"
    # Which ONNX file inside the model repo to load. Explicit because the
    # MiniLM repo ships nine variants and sentence-transformers otherwise
    # warns and picks one itself. fp32 keeps parity exact; the qint8
    # variants trade ~0.008 cosine drift for ~25% more speed.
    onnx_file_name: str = "onnx/model.onnx"
    # LRU cache over (text, normalize) -> embedding. The daemon embeds the
    # same strings repeatedly (query text for search + slot ops, dedup
    # keys, warmup probes); repeats skip the model forward entirely.
    # 0 disables. ~1.5 KB per entry at dim 384.
    cache_size: int = 1024


@dataclass
class MemoryBankConfig:
    max_patterns: int = 5000
    beta: float = 4.0
    consolidation_interval: int = 1  # 1 = every interaction (fast bank default)


@dataclass
class TitansConfig:
    """Legacy flat TITANS configuration (v0.4.x).

    Kept around so existing ``config.yaml`` files with a ``memory.titans``
    block keep loading. New code should construct a :class:`MIRASConfig`
    instead — when ``preset = "titans"``, the resulting band specs
    reproduce these defaults exactly.
    """
    # Instant bank (updated every message)
    instant_hidden_dim: int = 512
    instant_max_entries: int = 2000
    instant_lr: float = 0.01

    # Short-term bank (updated every N messages)
    short_term_hidden_dim: int = 512
    short_term_max_entries: int = 5000
    short_term_lr: float = 0.001
    short_term_interval: int = 5

    # Long-term bank (updated every M messages)
    long_term_hidden_dim: int = 768
    long_term_max_entries: int = 10000
    long_term_lr: float = 0.0001
    long_term_interval: int = 20

    weight_decay: float = 0.001


@dataclass
class MIRASBandSpec:
    """Per-band configuration along the four MIRAS axes plus capacity / cadence.

    A :class:`MIRASConfig` holds a list of these — one per band in the
    continuum. Field names mirror the axes documented in
    :mod:`src.memory.miras` so a YAML reader can map 1:1.
    """
    name: str = "band"
    max_entries: int = 5000
    update_interval: int = 1
    promotion_access_count: int = 2
    promotion_surprise: float = 0.5
    retention_policy: str = "balanced"   # balanced / recency_heavy / surprise_heavy


@dataclass
class MIRASConfig:
    """Continuum-of-bands specification.

    ``preset`` selects a named point in the MIRAS design space. When
    ``preset != "custom"``, ``bands`` is populated from the preset registry
    at construction time — any ``bands`` block in the YAML is ignored
    for non-custom presets, which keeps the config diffable.

    When ``preset = "custom"``, ``bands`` must be provided explicitly.

    Attributes
    ----------
    preset:
        ``titans`` / ``moneta`` / ``yaad`` / ``memora`` / ``continuum`` /
        ``custom``. ``continuum`` is the v0.6 8-tier preset designed for
        agentic deployments.
    bands:
        Per-tier specs. Populated from the preset for non-``custom``.
    """
    preset: str = "continuum"
    bands: list[MIRASBandSpec] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Importing inside __post_init__ to dodge a circular import:
        # presets.py types its return as MIRASBandSpec, which lives here.
        if self.preset != "custom":
            from pseudolife_memory.memory.miras.presets import preset_bands  # noqa: PLC0415
            self.bands = preset_bands(self.preset)
        elif not self.bands:
            raise ValueError(
                "MIRASConfig: preset='custom' requires a non-empty bands list "
                "in config.yaml. Either set preset to a named value "
                "(titans / moneta / yaad / memora / continuum) or provide explicit bands."
            )


@dataclass
class ReferenceConfig:
    """ChromaDB-backed reference bank for RAG document storage."""
    persist_dir: str = "./memory_state/chromadb"
    collection_name: str = "reference_bank"
    chunk_size: int = 512
    chunk_overlap: int = 64
    max_results: int = 5


@dataclass
class NLIConfig:
    """Configuration for the optional NLI contradiction-detection path.

    EXPERIMENTAL / not wired: no production path constructs the scorer, so
    this block only takes effect for library callers that inject one
    (2026-07-02 zombie sweep set the default to False to stop the knob
    lying about a live capability)."""
    enabled: bool = False
    model_name: str = "cross-encoder/nli-deberta-v3-xsmall"
    threshold: float = 0.70
    max_candidates: int = 8


@dataclass
class BM25Config:
    """BM25 sparse-lexical retrieval pool (Tier B2).

    Runs the standard Okapi BM25 scorer across every band entry in
    parallel with the bi-encoder dense retrieval. The two pools are
    weighted-sum-fused before the cross-encoder reranker fires, so a
    query like ``process_chunk_v2`` — where the dense embedder has
    little to latch onto — still surfaces the entry whose text
    contains the exact token.

    Off by default. Enable globally via
    ``memory.bm25.enabled = true`` in config, or pass ``bm25=True``
    per call to ``memory_search``.

    Score fusion
    ------------
    BM25 raw scores are min-max normalised into ``[0, 1]`` per query
    (so unbounded BM25 magnitudes don't drown the bi-encoder's
    cosine-bounded scores). The contribution to the combined score is::

        final = dense_score + weight * normalized_bm25

    ``weight = 0.3`` (default) treats BM25 as a *boost* — the dense
    pool still drives ordering on most queries, but lexically-aligned
    entries get nudged up. New entries that only BM25 finds enter the
    pool at ``weight * normalized_bm25`` (no dense contribution), which
    is intentionally below the typical dense hit so BM25-only matches
    don't displace strong semantic matches.
    """
    enabled: bool = False
    k1: float = 1.5
    b: float = 0.75
    weight: float = 0.3
    top_n: int = 20
    # Floor on the *normalised* BM25 score — entries below this aren't
    # injected into the result pool. Keeps high-frequency-but-irrelevant
    # docs from polluting recall.
    min_score: float = 0.1


@dataclass
class RerankerConfig:
    """Cross-encoder reranker over the merged retrieval pool (Tier B).

    Bi-encoder retrieval (dense MiniLM-L6) is cheap but loses signal on
    near-duplicates and ambiguous queries — a query and a relevant doc
    can have low cosine similarity while a less-relevant one wins on
    surface tokens. A cross-encoder attends over (query, candidate)
    jointly and re-scores them at the cost of one transformer pass per
    pair. We run it on the top-N candidates only (default 20) so the
    cost stays bounded.

    Off by default — install with ``pip install .[rerank]`` (which just
    pulls a slightly newer sentence-transformers anyway), set
    ``enabled = True`` in config, or pass ``rerank=True`` per-call to
    ``memory_search``.

    Score fusion
    ------------
    The fused score is::

        final = fusion_weight * sigmoid(ce_score) + (1 - fusion_weight) * original

    where ``ce_score`` is the cross-encoder logit and ``original`` is the
    bi-encoder's adjusted score (cosine × recency × source × supersession).
    ``fusion_weight = 0.7`` (default) leans on the cross-encoder but
    preserves enough of the bi-encoder signal that recency/source/
    supersession multipliers still nudge the order on near-ties.
    """
    enabled: bool = False
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    top_n: int = 20
    fusion_weight: float = 0.7
    # Skip the cross-encoder pass when the gap between the two best
    # bi-encoder-adjusted scores is >= this margin — a decisively
    # separated head can only be reshuffled, not fixed, by reranking.
    # 0.0 (default) disables the gate: the reranker fires whenever
    # enabled, exactly the pre-gate behavior.
    # CAUTION: a skip returns raw bi-encoder scores, which sit lower than
    # fused (0.7*sigmoid(ce)) scores for strong matches — don't combine a
    # nonzero margin with a search_confidence_floor tuned to the fused
    # scale, or decisive winners just under the floor will spuriously
    # abstain.
    skip_margin: float = 0.0


@dataclass
class ContrastiveConfig:
    """Contrastive retrieval objective (Slice F, v0.7.6).

    When the user signals dissatisfaction with a recall ("no, that's
    wrong", "are you sure", "I never said that"), suppress the top-1
    retrieval against the *previous* user query and apply a small
    negated gradient step to the owning band so similar patterns rank
    lower in future retrieval.

    Cost: one extra retrieval + one negated gradient step per fire.
    Disabled by default off → on makes the system actively learn from
    negative feedback; verify behaviour with the audit log
    (``source="correction"`` markers in ``/api/memory/search``) before
    relying on it in production.
    """
    enabled: bool = True
    min_target_score: float = 0.35
    scale: float = 0.1
    max_targets_per_signal: int = 1  # only the top-1 in v0.7.6


@dataclass
class ReflectionConfig:
    """Periodic reflection / dreaming (Slice D, v0.7.6).

    Every ``reflect_every_n_interactions`` stores, sample the most recent
    ``reflect_window`` user-sourced memories and ask the LLM to distil
    them into 1-3 short factual sentences. Stored via the CMS pipeline
    with ``source="reflection"``.

    Cost: one extra LLM call per N stores, max_tokens-bounded, runs on
    a daemon thread so chat latency is unaffected.
    """
    enabled: bool = True
    reflect_every_n_interactions: int = 50
    reflect_window: int = 30
    max_tokens: int = 200
    timeout_seconds: float = 10.0


@dataclass
class DreamConfig:
    """Dream pass — MIRAS→cortex consolidation (pluggable extractor).

    Tier 0 (regex floor) needs no config. ``eligible_sources`` / ``exclude_sources``
    decide which stored memories a dream consolidates; ``min_batch`` / ``idle_seconds``
    are the backlog+quiescence trigger used by ``dream_status`` (and, later, the
    daemon sweep). Tier-2 extractor fields are defined now for config stability but
    unused until the OpenAI-compatible extractor lands.
    """
    enabled: bool = True
    # Which stored sources are eligible. None => every source EXCEPT exclude_sources.
    eligible_sources: list[str] | None = None
    # Sources the dream never consolidates — they stay in the searchable bands but
    # are not mined for facts/graph edges. "status"/"log" are the convention for
    # dense status dumps (recallable via memory_search, but no graph pollution).
    exclude_sources: list[str] = field(
        default_factory=lambda: ["consolidation", "reflection", "status", "log"]
    )
    # Backlog + quiescence trigger (consumed by dream_status + the daemon sweep).
    # idle_seconds is deliberately short-ish: consolidate ~10 min after the user
    # goes quiet, but NEVER mid-session (any store resets idle) — see
    # docs/specs/2026-06-26-dream-cadence-design.md.
    min_batch: int = 8
    idle_seconds: float = 600.0
    max_batch: int = 40
    sweep_interval_seconds: float = 600.0   # used by the Phase 3 daemon sweep
    # Tier 2 (Phase 3) — BYO OpenAI-compatible extractor. Unused in Phases 1–2.
    extractor_base_url: str | None = None
    extractor_api_key: str | None = None
    extractor_model: str | None = None
    # Who owns the extractor endpoint settings above: "env" (default) keeps
    # the ops contract — PSEUDOLIFE_DREAM_* env vars override the dataclass,
    # as the compose file and docs/guide/dreaming.md document. "config" hands control to
    # this config (the Console's Extractor panel writes here), ignoring the
    # env vars — the honest way to let a UI change win over a compose-baked
    # env default without silently breaking existing env-driven deploys.
    # api_key stays env-only either way (never persisted to config.yaml).
    extractor_source: str = "env"
    # Output budget for the extractor call. Sized generously so a dense dream
    # batch can emit all its claim JSON without truncation (a truncated response
    # parses to fewer/zero claims). 2048 ≈ 40-80 claims. Override per-deploy with
    # ``PSEUDOLIFE_DREAM_MAX_TOKENS``.
    extractor_max_tokens: int = 2048
    # A small CPU extractor generates at ~12-30 tok/s depending on the bake, so
    # a full ``extractor_max_tokens`` (2048) generation is ~70-170s — plus prompt
    # processing of the texts + vocab hint. The old 20s default timed the dream
    # out (claims:0 → no cortex write). 240s covers the lighter bakes; the Docker
    # stack ships 480s for the default E4B sidecar (see ops/docker-compose.yml).
    # The dream is a background sweep (600s interval) so latency is irrelevant.
    # Override per-deploy with ``PSEUDOLIFE_DREAM_TIMEOUT_SECONDS``.
    extractor_timeout_seconds: float = 240.0
    # Primary/fallback extractor selection (2026-07-11 sonnet-sidecar-cutover
    # spec). fallback_base_url unset => single-extractor behavior identical
    # to before (no probe, no selection). extractor_mode: "auto" probes the
    # primary and falls back; "primary" never falls back (outages hold);
    # "fallback" skips the primary entirely (sovereign-only override).
    # Env: PSEUDOLIFE_DREAM_FALLBACK_BASE_URL / _FALLBACK_MODEL /
    # _EXTRACTOR_MODE (honoured when extractor_source == "env").
    # Timeout/max_tokens are shared with the primary — no fallback copies.
    fallback_base_url: str | None = None
    fallback_model: str | None = None
    extractor_mode: str = "auto"
    # GAM #2 graph-from-text: the dream also extracts (src,relation,dst) triples
    # into the graph (separate extract_relations call — the bench winner). Edges
    # are dream-inferred, so a modest confidence below explicit graph_relate (0.8)
    # and lessons (0.7).
    extract_relations: bool = True
    relation_confidence: float = 0.6  # legacy default; superseded by edge_confidence()
    # Edges scoring below this at link time are dropped. Hard type-violations
    # score 0.1125-0.175 (relation_quality.edge_confidence), so 0.2 auto-drops
    # them at the source instead of leaving them for deep-dream cleanup.
    # Set 0.0 to restore the old write-everything behavior.
    min_relation_confidence: float = 0.2
    # Edges at/above the floor but below this route to edge_proposals for
    # review instead of the live graph. At 0.5 this quarantines exactly the
    # untyped related-to co-mention edges (conf 0.45) — the dominant
    # review-queue pollutant (~19/day, dubious count 34 -> 120 in four days,
    # 2026-07-19). Typed clean edges (0.70) are unaffected. 0.0 disables.
    relation_quarantine_below: float = 0.5
    # Write-time dedup: when the dream mints a NEW entity whose name-token
    # Jaccard against an existing canonical/display/alias reaches this
    # threshold, a merge proposal is filed for review (never auto-folded).
    # 0 disables the detector.
    write_dedup_min_jaccard: float = 0.6
    # Alias-candidate post-pass: when a dream claim mints a NEW cortex entity
    # whose name-embedding cosine against an existing entity name reaches
    # this threshold, a merge proposal is filed for review (same queue and
    # review flow as the Jaccard detector above; never auto-folded). Semantic
    # complement to token Jaccard: "production extractor sidecar" ~
    # "Pseudolife-MCP default extractor sidecar" is Jaccard 0.33 but cosine
    # 0.65 (all-MiniLM-L6-v2 calibration 2026-07-07: paraphrase pairs scored
    # 0.53-0.77, unrelated pairs <= 0.17). 0 disables.
    alias_candidate_min_cosine: float = 0.5
    # TiMem-inspired known-facts window
    # (docs/specs/2026-07-10-known-facts-window-design.md): when > 0, the dream
    # prompt also shows the CURRENT VALUES of the top-N relevance-ranked slots
    # so updates supersede in place instead of minting paraphrase-variant keys.
    # 0 (default) = off — the extractor request is byte-identical to before.
    # Working value when enabled: 20.
    known_facts_window: int = 0


@dataclass
class DeepDreamConfig:
    """Manual full-corpus graph consolidation (Phase-2 'C'). See
    docs/superpowers/specs/2026-06-28-deep-dream-graph-consolidation-design.md."""
    min_similarity: float = 0.55       # cosine floor for a link candidate
    top_k_candidates: int = 50         # max candidate pairs emitted per pass
    max_context_snippets: int = 3      # context snippets per entity in a candidate
    auto_apply_safe: bool = True       # auto-supersede violations + merge exact dups (apply only)
    min_entity_mentions: int = 2       # an entity needs >= this many distinct mentioning entries to be candidate-eligible
    merge_min_similarity: float = 0.90   # cosine floor for a near-dup MERGE candidate (vs a link)
    junk_max_degree: int = 1             # junk entities must be this weakly connected to be flagged
    max_support_overlap: float = 0.8     # Jaccard on supporting-entry sets at/above which a pair is co-occurrence
    snippet_max_chars: int = 240         # per-snippet truncation in the deep response
    snapshot_keep: int = 10              # graph-snapshot undo files kept under data_dir/graph_snapshots


@dataclass
class CortexConfig:
    """Sibling slot-keyed canonical-fact store (schema v8).

    The cortex is the *cortical* layer to the continuum's *hippocampus*:
    identity-not-similarity, supersession-not-decay, currency-not-frequency —
    one current value per ``(entity, attribute)`` slot. Single-writer cortex: it
    is populated by the LLM **dream** pass (the sole automatic writer) and by
    explicit ``memory_fact_set`` tool calls. ``auto_promote`` is an opt-in
    (default **off**) deterministic regex floor that runs on every ``store``;
    it is off by default because the regex mis-splits compound entity names
    (``"payments database host"`` -> ``payments`` / ``database host``) and so
    fragments slots — see ``docs/specs/2026-06-19-single-writer-cortex-design.md``.

    ``promote_confidence`` is deliberately a low floor so a deliberate
    ``fact_set`` (or a user-tier assertion) out-ranks an auto-promoted guess via
    ``supersede_confidence_margin``.
    """
    enabled: bool = True
    auto_promote: bool = False
    promote_confidence: float = 0.5
    search_first: bool = True
    # When True, a conflicting write weaker than a slot's current provenance tier
    # (user>action>agent), or below the confidence margin, is parked as a visible
    # contender instead of silently superseding. False -> pure newer-wins.
    protect_provenance: bool = True
    supersede_confidence_margin: float = 0.15
    reinforce_rate: float = 0.34
    # Cortex guard for memory_search abstention: a current fact must score >= this
    # to be surfaced (and to suppress low_confidence). Default 0.2: fact embeddings
    # are terse "entity attribute value" strings whose cosine vs a natural-language
    # question rarely clears 0.3 even when the fact IS the answer — the 2026-07-06
    # LongMemEval replay sweep showed 0.3 serves ZERO facts for 60% of questions
    # vs 28% at 0.2, with identical end-to-end accuracy. 0.1 was tried and served
    # more gold facts but measurably hurt: the extra weak facts dilute the context
    # and the consumer abstains ("distractor-induced under-confidence").
    # Abstention-on deployments still override upward (see
    # docs/guide/retrieval.md: the 0.65 pairing).
    guard_min_score: float = 0.2
    # Dream-path slot resolver: a paraphrased dreamed claim adopts an existing
    # current slot when its value-free slot embedding cosine >= this. <=0 disables
    # (exact-key only = today's behaviour). Positive = the cosine floor.
    dream_slot_match_threshold: float = 0.0


@dataclass
class LessonsConfig:
    """Procedural / outcome memory ("lessons", schema v10) — a third slot-keyed
    store beside the personal and world cortex. Keyed by ``(task-type, aspect)``,
    each lesson carries an ``outcome`` (success|failure|correction) and ``polarity``
    (do/avoid). Written solely by the dream from cheap in-session outcome signals
    (single-writer). See ``docs/specs/2026-06-20-procedural-outcome-memory-design.md``.
    """
    enabled: bool = True
    top_k: int = 5
    min_confidence: float = 0.0
    # Unconsumed (and consumed) signals older than this are pruned on the dream
    # sweep so the append-only log can't grow unbounded when no extractor drains it.
    signal_retention_days: int = 30
    # When False, the dream skips signal drain / lesson synthesis (signals still
    # pruned by retention).
    synthesize_in_dream: bool = True
    # Auto-outcome inference (spec 2026-07-18): infer signals for episodes
    # that close with entries but zero explicit outcomes. origin="inferred";
    # lessons from all-inferred batches start at confidence 0.4.
    infer_outcomes: bool = True
    infer_outcomes_max_signals: int = 3


@dataclass
class CompactionConfig:
    """Superseded-row compaction over facts/world_facts/lessons (spec
    2026-07-14). Per slot: keep the newest ``keep_per_slot`` non-live
    records; purge the rest once older than ``min_age_days``. Runs on the
    dream sweep tick."""
    enabled: bool = True
    keep_per_slot: int = 3
    min_age_days: float = 30.0


@dataclass
class MetaFilterConfig:
    """Self-reference meta-statement filter on the store path.

    Designed for Pseudolife's chat flow where model responses are
    auto-captured. In the MCP build every store is deliberate, so
    ``MemoryService._apply_mcp_defaults`` disables it.
    """
    enabled: bool = True


@dataclass
class GraphInsightConfig:
    """Topology analytics computed during dream (Track B). Communities persisted;
    god-nodes/surprises/questions stored as the meta['graph_digest'] snapshot."""
    enabled: bool = True
    algorithm: str = "louvain"          # "louvain" | "leiden" (leiden needs graspologic; falls back)
    resolution: float = 1.0
    max_community_fraction: float = 0.25
    god_nodes_top_n: int = 10
    surprises_top_n: int = 10
    questions_top_n: int = 7
    betweenness_sample: int = 200       # k-sample betweenness above this node count (0 = exact)


@dataclass
class TracesConfig:
    """Engram cross-index (provenance-as-link). When enabled, the dream links
    each consolidated fact-slot to the dense episodes it came from and bumps their
    reinforcement counter. retention_boost (Phase 2) reads that counter."""
    enabled: bool = True
    # MTT retention (Phase 2). Weight on log1p(reinforcements) in band eviction
    # scoring; 0.0 = today's eviction exactly. A positive value makes reinforced
    # episodes resist forgetting in proportion to their strength.
    retention_boost: float = 0.0


@dataclass
class ScopesConfig:
    """Project-scope derivation (entity_sources backfill). ``exclude`` lists
    source tags that must never become projects — meta/chatter tags leak into
    the Atlas project list otherwise. ``rollup`` maps a fine-grained source to
    an umbrella project; the backfill writes BOTH scopes, so the family view
    and the precise filter coexist. Scope keys are always case-folded."""
    exclude: list[str] = field(default_factory=lambda: [
        "status", "claude", "agent", "correction"])
    rollup: dict[str, str] = field(default_factory=dict)

    def scope_keys(self, sources) -> set[str]:
        """Fold raw source tags into the scope keys this policy admits:
        case-folded, excluded tags dropped, rollup umbrellas added alongside
        their fine-grained key. Shared by the backfill and write-time
        provenance stamping so the two can never disagree."""
        excl = {str(s).strip().lower() for s in self.exclude}
        roll = {str(k).strip().lower(): str(v).strip().lower()
                for k, v in self.rollup.items()}
        out: set[str] = set()
        for s in sources or ():
            key = str(s).strip().lower()
            if not key or key in excl:
                continue
            out.add(key)
            umb = roll.get(key)
            if umb and umb != key and umb not in excl:
                out.add(umb)
        return out


@dataclass
class RecallConfig:
    """memory_recall — live MemCoT iterative retrieval (read-only).

    ``driver`` selects seed resolution: "mechanical" (word-match vocab; default,
    no model) or "llm" (the dream extractor names seeds). Env override:
    ``PSEUDOLIFE_RECALL_DRIVER``.
    """
    driver: str = "mechanical"
    default_hops: int = 3
    default_top_k: int = 5
    max_entities: int = 50
    # Hub-gating (graphify-derived): include high-degree hubs as results but
    # don't expand THROUGH them. hub_floor / expand_budget are bench-tuned.
    hub_gate: bool = True
    hub_percentile: float = 95.0
    hub_floor: int = 8
    expand_budget: int = 0   # per-hop expansion cap; 0 = unlimited


@dataclass
class MemoryConfig:
    embedding_dim: int = 384
    # Legacy Hopfield config (kept for fallback)
    fast_bank: MemoryBankConfig = field(default_factory=lambda: MemoryBankConfig(
        max_patterns=5000, beta=4.0, consolidation_interval=1,
    ))
    slow_bank: MemoryBankConfig = field(default_factory=lambda: MemoryBankConfig(
        max_patterns=10000, beta=2.0, consolidation_interval=10,
    ))
    # Legacy flat TITANS config — kept for backwards compat (v0.4.x YAML still loads).
    titans: TitansConfig = field(default_factory=TitansConfig)
    # MIRAS (v0.5+) — preset-driven per-band specification. Default ``titans``
    # preset reproduces ``TitansConfig`` defaults bit-for-bit so behaviour is
    # unchanged for anyone who doesn't opt into a different preset.
    miras: MIRASConfig = field(default_factory=MIRASConfig)
    # Reference bank (RAG via ChromaDB)
    reference: ReferenceConfig = field(default_factory=ReferenceConfig)
    # NLI contradiction-detection (fourth path)
    nli: NLIConfig = field(default_factory=NLIConfig)
    # BM25 sparse lexical pool, fused with dense retrieval (Tier B2).
    bm25: BM25Config = field(default_factory=BM25Config)
    # Cross-encoder reranker over the merged retrieval pool (Tier B).
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    # HyDE-lite query expansion (Slice E, v0.7.6).
    # Periodic reflection / dreaming (Slice D, v0.7.6).
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    # Dream pass — MIRAS→cortex consolidation (pluggable extractor).
    dream: DreamConfig = field(default_factory=DreamConfig)
    # Manual full-corpus graph consolidation (deep dream, Phase-2 'C').
    deep_dream: DeepDreamConfig = field(default_factory=DeepDreamConfig)
    # Contrastive retrieval objective (Slice F, v0.7.6).
    contrastive: ContrastiveConfig = field(default_factory=ContrastiveConfig)
    # Cortex — sibling slot-keyed canonical-fact store (schema v7).
    cortex: CortexConfig = field(default_factory=CortexConfig)
    # Procedural / outcome memory — lessons store (schema v10).
    lessons: LessonsConfig = field(default_factory=LessonsConfig)
    # Superseded-row compaction (keep-newest-N + min-age; spec 2026-07-14).
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    # memory_recall — live MemCoT iterative retrieval (read-only).
    recall: RecallConfig = field(default_factory=RecallConfig)
    # Topology analytics computed during dream (Track B).
    graph_insight: GraphInsightConfig = field(default_factory=GraphInsightConfig)
    # Engram cross-index (provenance-as-link, schema v13).
    traces: TracesConfig = field(default_factory=TracesConfig)
    # Project-scope derivation — meta-source exclusions + umbrella rollups.
    scopes: ScopesConfig = field(default_factory=ScopesConfig)
    # Meta-statement filter on the store path (off in the MCP build).
    meta_filter: MetaFilterConfig = field(default_factory=MetaFilterConfig)
    # Base recency half-life at band depth 0; doubles per depth.
    # 3600 (1h) suits chat; the MCP build sets 86400 (1 day).
    recency_base_half_life_s: float = 3600.0
    memory_engine: str = "titans"  # "titans" or "hopfield"
    # v0.5 store gate is novelty-based (1 - max cos to existing entries). 0.0 =
    # permissive (store everything; novelty still scores eviction/promotion);
    # raise to dedup near-duplicate stores.
    surprise_threshold: float = 0.0
    top_k: int = 8       # episodic retrieval slots across bands
    ref_top_k: int = 3   # max reference bank results injected alongside memories
    save_dir: str = "./memory_state"
    # When False (default), entries marked superseded by the contradiction
    # pipeline are hidden from retrieval so the LLM sees only current facts.
    # Flip to True for debugging or historical inspection.
    show_superseded: bool = False
    # Abstention: when the top search score is below this floor, memory_search
    # returns low_confidence=True so the agent declines instead of using weak
    # distractor hits. 0.0 = off (only an empty result is low-confidence).
    # Tuned on a dev split by the benchmark ladder; default off to preserve recall.
    search_confidence_floor: float = 0.0


@dataclass
class ContextConfig:
    max_memory_tokens: int = 2000
    history_length: int = 10


@dataclass
class ChunkingConfig:
    chunk_size: int = 512
    chunk_overlap: int = 64


@dataclass
class TimeConfig:
    """Presentation of the temporal stamp (v0.4). ``relative_age`` adds a human
    ``age`` field (e.g. "3 days ago") to serialised canonical facts so the agent
    reads a sense of time without parsing epoch seconds."""
    relative_age: bool = True


@dataclass
class StorageConfig:
    """Postgres persistence policy.

    ``write_mode`` selects the canonical write path:

    * ``snapshot`` (default, the only live path) — the cortex is small, so each
      save is a transactional full rewrite (``replace_facts``). Single-writer by
      construction via the daemon's lock.
    * ``occ`` — optimistic concurrency control (per-row compare-and-swap on
      ``version``) for a future multi-process writer topology. **Phase 2**: the
      seam exists (``version`` column, ``replace_facts_occ`` stub) but the real
      path is unbuilt; selecting it raises ``NotImplementedError``.
    """
    write_mode: str = "snapshot"


@dataclass
class AppConfig:
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    time: TimeConfig = field(default_factory=TimeConfig)


def _dict_to_dataclass(cls: type, data: dict[str, Any]) -> Any:
    """Recursively convert a dict to a dataclass, ignoring extra keys."""
    if not isinstance(data, dict):
        return data
    field_names = {f.name for f in cls.__dataclass_fields__.values()}
    filtered = {}
    for k, v in data.items():
        if k in field_names:
            field_type = cls.__dataclass_fields__[k].type
            # Handle nested dataclasses
            if isinstance(v, dict) and hasattr(field_type, "__dataclass_fields__"):
                filtered[k] = _dict_to_dataclass(field_type, v)
            else:
                filtered[k] = v
    return cls(**filtered)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load configuration from a YAML file, falling back to defaults."""
    path = Path(path)
    if not path.exists():
        return AppConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # Build config from raw dict. (The chat-product backend blocks —
    # backend/claude/gemini/lmstudio — were removed in the 2026-07-02
    # zombie sweep; unknown YAML sections are simply ignored.)
    config = AppConfig()

    if "embedding" in raw:
        config.embedding = _dict_to_dataclass(EmbeddingConfig, raw["embedding"])
    if "memory" in raw:
        mem_raw = raw["memory"]
        config.memory = MemoryConfig(
            embedding_dim=mem_raw.get("embedding_dim", 384),
            memory_engine=mem_raw.get("memory_engine", "titans"),
            surprise_threshold=mem_raw.get("surprise_threshold", 0.3),
            top_k=mem_raw.get("top_k", 8),
            ref_top_k=mem_raw.get("ref_top_k", 3),
            save_dir=mem_raw.get("save_dir", "./memory_state"),
            show_superseded=mem_raw.get("show_superseded", False),
            search_confidence_floor=mem_raw.get("search_confidence_floor", 0.0),
            recency_base_half_life_s=mem_raw.get("recency_base_half_life_s", 3600.0),
        )
        if "fast_bank" in mem_raw:
            config.memory.fast_bank = _dict_to_dataclass(MemoryBankConfig, mem_raw["fast_bank"])
        if "slow_bank" in mem_raw:
            config.memory.slow_bank = _dict_to_dataclass(MemoryBankConfig, mem_raw["slow_bank"])
        if "titans" in mem_raw:
            config.memory.titans = _dict_to_dataclass(TitansConfig, mem_raw["titans"])
        if "miras" in mem_raw:
            miras_raw = mem_raw["miras"]
            # ``bands`` is a list of dicts → list of :class:`MIRASBandSpec`.
            bands_raw = miras_raw.get("bands", []) or []
            bands = [_dict_to_dataclass(MIRASBandSpec, b) for b in bands_raw]
            # Construction triggers __post_init__ which overrides ``bands`` from
            # the preset registry for non-custom presets — see :class:`MIRASConfig`.
            config.memory.miras = MIRASConfig(
                preset=miras_raw.get("preset", "titans"),
                bands=bands,
            )
        if "reference" in mem_raw:
            config.memory.reference = _dict_to_dataclass(ReferenceConfig, mem_raw["reference"])
        if "nli" in mem_raw:
            config.memory.nli = _dict_to_dataclass(NLIConfig, mem_raw["nli"])
        if "bm25" in mem_raw:
            config.memory.bm25 = _dict_to_dataclass(
                BM25Config, mem_raw["bm25"],
            )
        if "reranker" in mem_raw:
            config.memory.reranker = _dict_to_dataclass(
                RerankerConfig, mem_raw["reranker"],
            )
        if "reflection" in mem_raw:
            config.memory.reflection = _dict_to_dataclass(
                ReflectionConfig, mem_raw["reflection"],
            )
        if "contrastive" in mem_raw:
            config.memory.contrastive = _dict_to_dataclass(
                ContrastiveConfig, mem_raw["contrastive"],
            )
        if "cortex" in mem_raw:
            config.memory.cortex = _dict_to_dataclass(CortexConfig, mem_raw["cortex"])
        if "lessons" in mem_raw:
            config.memory.lessons = _dict_to_dataclass(
                LessonsConfig, mem_raw["lessons"],
            )
        if "dream" in mem_raw:
            config.memory.dream = _dict_to_dataclass(DreamConfig, mem_raw["dream"])
        if "recall" in mem_raw:
            config.memory.recall = _dict_to_dataclass(RecallConfig, mem_raw["recall"])
        if "graph_insight" in mem_raw:
            config.memory.graph_insight = _dict_to_dataclass(
                GraphInsightConfig, mem_raw["graph_insight"],
            )
        if "meta_filter" in mem_raw:
            config.memory.meta_filter = _dict_to_dataclass(
                MetaFilterConfig, mem_raw["meta_filter"],
            )
        if "traces" in mem_raw:
            config.memory.traces = _dict_to_dataclass(
                TracesConfig, mem_raw["traces"],
            )
        if "compaction" in mem_raw:
            config.memory.compaction = _dict_to_dataclass(
                CompactionConfig, mem_raw["compaction"],
            )
        if "deep_dream" in mem_raw:
            config.memory.deep_dream = _dict_to_dataclass(
                DeepDreamConfig, mem_raw["deep_dream"],
            )
        if "scopes" in mem_raw:
            config.memory.scopes = _dict_to_dataclass(
                ScopesConfig, mem_raw["scopes"],
            )
    if "context" in raw:
        config.context = _dict_to_dataclass(ContextConfig, raw["context"])
    if "chunking" in raw:
        config.chunking = _dict_to_dataclass(ChunkingConfig, raw["chunking"])
    if "storage" in raw:
        config.storage = _dict_to_dataclass(StorageConfig, raw["storage"])
    if "time" in raw:
        config.time = _dict_to_dataclass(TimeConfig, raw["time"])

    return config
