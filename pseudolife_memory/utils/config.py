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
    hidden_dim: int = 512
    max_entries: int = 5000
    learning_rate: float = 0.01
    update_interval: int = 1
    promotion_access_count: int = 2
    promotion_surprise: float = 0.5
    # MIRAS axes — string keys into the registries in src.memory.miras.
    memory_module: str = "mlp3"          # mlp3 / mlp2 / linear
    update_rule: str = "sgd_momentum"    # sgd_momentum / adam / lion / momentum_only
    objective: str = "l2"                # l2 / lp / neg_sim / kv
    objective_p: float = 2.0             # consumed only by ``objective: lp``
    retention_policy: str = "balanced"   # balanced / recency_heavy / surprise_heavy
    weight_decay: float = 0.001


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
    chain_residual:
        When True, retrieval flows the query forward through each band's
        MLP and adds the chained output as an extra synthesized retrieval
        signal — HOPE-style sequential read. ``stop_gradient`` between
        tiers keeps each tier's local optimisation independent (no BPTT,
        no training-time wiring). Off by default; on in the ``continuum``
        preset.
    """
    preset: str = "continuum"
    bands: list[MIRASBandSpec] = field(default_factory=list)
    chain_residual: bool = False

    def __post_init__(self) -> None:
        # Importing inside __post_init__ to dodge a circular import:
        # presets.py types its return as MIRASBandSpec, which lives here.
        if self.preset != "custom":
            from pseudolife_memory.memory.miras.presets import preset_bands, preset_chain_residual  # noqa: PLC0415
            self.bands = preset_bands(self.preset)
            # Presets can opt their default chain_residual setting; the
            # YAML may override with an explicit value.  Use the
            # default-only-if-untouched pattern: if chain_residual is at
            # its dataclass default (False), apply the preset's default.
            if not self.chain_residual:
                self.chain_residual = preset_chain_residual(self.preset)
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
    """Sibling slot-keyed canonical-fact store (schema v7).

    The cortex is the *cortical* layer to the continuum's *hippocampus*:
    identity-not-similarity, supersession-not-decay, currency-not-frequency —
    one current value per ``(entity, attribute)`` slot. It is populated
    deterministically from slot-shaped facts on every ``store`` (``auto_promote``,
    the no-LLM floor) and/or by explicit ``memory_fact_set`` tool calls.

    ``promote_confidence`` is deliberately a low floor so a deliberate
    ``fact_set`` (or a user-tier assertion) out-ranks an auto-promoted guess via
    ``supersede_confidence_margin``.
    """
    enabled: bool = True
    auto_promote: bool = True
    promote_confidence: float = 0.5
    search_first: bool = True
    # When True, a conflicting write weaker than a slot's current provenance tier
    # (user>action>agent), or below the confidence margin, is parked as a visible
    # contender instead of silently superseding. False -> pure newer-wins.
    protect_provenance: bool = True
    supersede_confidence_margin: float = 0.15
    reinforce_rate: float = 0.34


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
    # Contrastive retrieval objective (Slice F, v0.7.6).
    contrastive: ContrastiveConfig = field(default_factory=ContrastiveConfig)
    # Cortex — sibling slot-keyed canonical-fact store (schema v7).
    cortex: CortexConfig = field(default_factory=CortexConfig)
    memory_engine: str = "titans"  # "titans" or "hopfield"
    surprise_threshold: float = 0.3
    top_k: int = 8       # neural memory slots (instant + short + long)
    ref_top_k: int = 3   # max reference bank results injected alongside memories
    save_dir: str = "./memory_state"
    # When False (default), entries marked superseded by the contradiction
    # pipeline are hidden from retrieval so the LLM sees only current facts.
    # Flip to True for debugging or historical inspection.
    show_superseded: bool = False


@dataclass
class ContextConfig:
    max_memory_tokens: int = 2000
    history_length: int = 10


@dataclass
class ChunkingConfig:
    chunk_size: int = 512
    chunk_overlap: int = 64


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
    if "context" in raw:
        config.context = _dict_to_dataclass(ContextConfig, raw["context"])
    if "chunking" in raw:
        config.chunking = _dict_to_dataclass(ChunkingConfig, raw["chunking"])

    return config
