"""Configuration loading and management."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ClaudeConfig:
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096


@dataclass
class LMStudioConfig:
    base_url: str = "http://localhost:1234/v1"
    model: str = "local-model"
    max_tokens: int = -1  # -1 = let LM Studio use model's max; respects loaded context
    api_key: str = "lm-studio"


@dataclass
class GeminiConfig:
    model: str = "gemini-2.0-flash"
    api_key: str = ""
    max_tokens: int = 4096


@dataclass
class EmbeddingConfig:
    model_name: str = "all-MiniLM-L6-v2"
    device: str = "cuda"
    batch_size: int = 64


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
    """Configuration for the optional NLI contradiction-detection path."""
    enabled: bool = True
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
    per call to ``memory_search`` / ``memory_trace``.

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
    ``memory_search`` / ``memory_trace``.

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
    # Backlog + quiescence trigger (consumed by dream_status / future sweep).
    min_batch: int = 8
    idle_seconds: float = 1800.0
    max_batch: int = 40
    sweep_interval_seconds: float = 600.0   # used by the Phase 3 daemon sweep
    # Tier 2 (Phase 3) — BYO OpenAI-compatible extractor. Unused in Phases 1–2.
    extractor_base_url: str | None = None
    extractor_api_key: str | None = None
    extractor_model: str | None = None
    # Output budget for the extractor call. Sized generously so a dense dream
    # batch can emit all its claim JSON without truncation (a truncated response
    # parses to fewer/zero claims). 2048 ≈ 40-80 claims. Override per-deploy with
    # ``PSEUDOLIFE_DREAM_MAX_TOKENS``.
    extractor_max_tokens: int = 2048
    # The default CPU sidecar (Gemma E2B Q4) generates at ~30 tok/s, so a full
    # ``extractor_max_tokens`` (2048) generation is ~70s — plus prompt processing
    # of the texts + vocab hint. The old 20s default timed the dream out (claims:0
    # → no cortex write). 240s gives headroom for slower end-user CPUs/laptops too;
    # the dream is a background sweep (600s interval) so latency is irrelevant.
    # Override per-deploy with ``PSEUDOLIFE_DREAM_TIMEOUT_SECONDS``.
    extractor_timeout_seconds: float = 240.0
    # GAM #2 graph-from-text: the dream also extracts (src,relation,dst) triples
    # into the graph (separate extract_relations call — the bench winner). Edges
    # are dream-inferred, so a modest confidence below explicit graph_relate (0.8)
    # and lessons (0.7).
    extract_relations: bool = True
    relation_confidence: float = 0.6


@dataclass
class HydeConfig:
    """HyDE-lite query expansion (Slice E, v0.7.6).

    Short queries carry weak embedding signal. When fired, the active LLM
    generates a one-sentence hypothetical answer, we embed it, and blend
    it with the query embedding before retrieval. Cost-guarded by a length
    heuristic and a hard timeout — falls back silently to query-only on
    any failure.
    """
    enabled: bool = True
    min_query_words: int = 5
    min_query_chars: int = 30
    max_tokens: int = 60
    query_weight: float = 0.5
    timeout_seconds: float = 5.0


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
    # to be surfaced (and to suppress low_confidence). Default 0.3 = today.
    guard_min_score: float = 0.3
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


@dataclass
class MetaFilterConfig:
    """Self-reference meta-statement filter on the store path.

    Designed for PseudoLife's chat flow where model responses are
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
    hyde: HydeConfig = field(default_factory=HydeConfig)
    # Periodic reflection / dreaming (Slice D, v0.7.6).
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    # Dream pass — MIRAS→cortex consolidation (pluggable extractor).
    dream: DreamConfig = field(default_factory=DreamConfig)
    # Contrastive retrieval objective (Slice F, v0.7.6).
    contrastive: ContrastiveConfig = field(default_factory=ContrastiveConfig)
    # Cortex — sibling slot-keyed canonical-fact store (schema v7).
    cortex: CortexConfig = field(default_factory=CortexConfig)
    # Procedural / outcome memory — lessons store (schema v10).
    lessons: LessonsConfig = field(default_factory=LessonsConfig)
    # memory_recall — live MemCoT iterative retrieval (read-only).
    recall: RecallConfig = field(default_factory=RecallConfig)
    # Topology analytics computed during dream (Track B).
    graph_insight: GraphInsightConfig = field(default_factory=GraphInsightConfig)
    # Engram cross-index (provenance-as-link, schema v13).
    traces: TracesConfig = field(default_factory=TracesConfig)
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
    backend: str = "lmstudio"
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    lmstudio: LMStudioConfig = field(default_factory=LMStudioConfig)
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

    # Build config from raw dict
    config = AppConfig(backend=raw.get("backend", "lmstudio"))

    if "claude" in raw:
        config.claude = _dict_to_dataclass(ClaudeConfig, raw["claude"])
    if "gemini" in raw:
        config.gemini = _dict_to_dataclass(GeminiConfig, raw["gemini"])
    if "lmstudio" in raw:
        config.lmstudio = _dict_to_dataclass(LMStudioConfig, raw["lmstudio"])
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
        if "hyde" in mem_raw:
            config.memory.hyde = _dict_to_dataclass(HydeConfig, mem_raw["hyde"])
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
    if "context" in raw:
        config.context = _dict_to_dataclass(ContextConfig, raw["context"])
    if "chunking" in raw:
        config.chunking = _dict_to_dataclass(ChunkingConfig, raw["chunking"])
    if "storage" in raw:
        config.storage = _dict_to_dataclass(StorageConfig, raw["storage"])
    if "time" in raw:
        config.time = _dict_to_dataclass(TimeConfig, raw["time"])

    return config
