"""Configuration loading and management."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EmbeddingConfig:
    model_name: str = "Qwen/Qwen3-Embedding-0.6B"
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
    # 0 disables. ~4 KB per entry at dim 1024 (was ~1.5 KB at 384).
    cache_size: int = 1024
    # Instruction prefix prepended to QUERY-side text only (never to stored
    # documents) — see EmbeddingPipeline.encode_query. Default is the exact
    # Qwen3-Embedding card string (embedding-backbone-v25); instruction-tuned
    # embedders swing on wording, so this must match the card verbatim,
    # including no space after "Query:". Empty string ("") restores
    # symmetric behavior for models (like all-MiniLM-L6-v2, the default
    # before embedding-backbone-v25) that don't distinguish query/document
    # sides.
    query_prefix: str = (
        "Instruct: Given a web search query, retrieve relevant passages "
        "that answer the query\nQuery:"
    )
    # Caps the tokenizer's max sequence length on the loaded model. Ahead of
    # the Qwen3-Embedding-0.6B swap (32k native context) — an unbounded
    # input is a latency and RAM hazard; 512 matches the measured shootout
    # configuration. Applied as a cap (min with whatever the model shipped
    # with), never a raise: a model whose native default is already shorter
    # is left alone.
    max_seq_length: int = 512


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
        ``flat`` (default since 2026-08-15 — one band at the continuum's
        total capacity; the measured tie, see docs/superpowers/specs/
        2026-08-14-flat-band-verdict-preregistration.md) / ``continuum``
        (the retained 8-tier layout — one-line rollback) / ``titans`` /
        ``moneta`` / ``yaad`` / ``memora`` (deprecated aliases of
        ``continuum``) / ``custom``. ``continuum`` is the v0.6 8-tier preset designed for
        agentic deployments.
    bands:
        Per-tier specs. Populated from the preset for non-``custom``.
    """
    preset: str = "flat"
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

    **On by default since 2026-07-25.** It shipped disabled, which meant
    every eval measured dense-only retrieval through a 22M-parameter 2021
    bi-encoder; an independent 2026 harness puts plain BM25 above every
    dedicated agent-memory system on LongMemEval. Cost is ~20-50ms per
    query at bank scale and no new dependency. Disable globally via
    ``memory.bm25.enabled = false``, or pass ``bm25=False`` per call to
    ``memory_search``.

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
    enabled: bool = True
    k1: float = 1.5
    b: float = 0.75
    weight: float = 0.3
    top_n: int = 20
    # Floor on the *normalised* BM25 score — entries below this aren't
    # injected into the result pool. Keeps high-frequency-but-irrelevant
    # docs from polluting recall.
    min_score: float = 0.1
    # Cortex fact retrieval has its own lexical switch, OFF by default:
    # the pre-registered 2026-07-30 _s A/B (evals/results/
    # bm25-ab-confirmation.json) found the fusion changed 56/78 served
    # fact contexts with zero accuracy or commit-rate movement, and cost
    # ~1 question on the oracle gate slice — the capability is kept for
    # identifier-heavy corpora (opt in here or per-call `bm25=True` on
    # `cortex_search`) but does not ship on.
    cortex_enabled: bool = False


@dataclass
class RerankerConfig:
    """Cross-encoder reranker over the merged retrieval pool (Tier B).

    Bi-encoder retrieval (the dense default backbone) is cheap but loses signal on
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
        default_factory=lambda: ["consolidation", "reflection", "status",
                                 "log", "digest"]
    )
    # Session digests (spec 2026-08-24-session-digest-design.md): one
    # narrative digest per closed session root, generated in the idle dream
    # cycle and stored as a source="digest" band entry. Ships OFF until the
    # budget-matched BEAM verdict and the sidecar quality spot-check
    # (evals/digest_sidecar_probe.py) both pass — the known-facts-window
    # precedent for dream-path features.
    digest_enabled: bool = False
    # Max session-context characters per summarize_session call; longer
    # sessions are split on line boundaries and map-reduce merged. 24000
    # chars ≈ 6K tokens — sized for the bundled CPU sidecar's context.
    digest_context_chars: int = 24000
    # Prose length target passed to the digest prompt. Originally 800
    # (the 2026-08-22 BEAM competitor analysis shape); re-targeted to
    # 1200 after the 2026-08-27 sidecar probe (9 digests, 3 runs) showed
    # the model steadily writing 1019–1908 chars (mean ~1435) against
    # 800, with the tail carrying retrievable specifics (versions,
    # deadline changes, error names) — the target now states the
    # observed natural length instead of fighting it.
    digest_target_chars: int = 1200
    # Closed episodes digested per dream cycle — bounds the backfill sweep
    # (the zero-start cursor digests all history when first enabled).
    digest_max_per_cycle: int = 4
    # Backlog + quiescence trigger (consumed by dream_status + the daemon sweep).
    # idle_seconds is deliberately short-ish: consolidate ~10 min after the user
    # goes quiet, but NEVER mid-session (any store resets idle) — see
    # docs/specs/2026-06-26-dream-cadence-design.md.
    min_batch: int = 8
    idle_seconds: float = 600.0
    max_batch: int = 40
    sweep_interval_seconds: float = 600.0   # used by the Phase 3 daemon sweep
    # Consolidation quarantine — the two-man rule for low-trust claims
    # (spec 2026-08-09-consolidation-quarantine-design.md; MAFIA-informed:
    # the defense keys on WHO wrote, never on what the text says). When on,
    # a scalar dream claim whose backing entry is agent-tier (source maps
    # to origin "agent") and outside ``trusted_sources`` parks as a
    # contender instead of taking ``current``; promotion needs an explicit
    # ``memory_fact_resolve(accept=True)`` or an independent second
    # witness. Ships OFF; flipping the default is a separate soak decision.
    quarantine_low_trust: bool = False
    # Sources operators explicitly trust past the quarantine (exact match
    # on the entry's ``source`` tag). Empty = the paranoid configuration.
    trusted_sources: list[str] = field(default_factory=list)
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
    # Model-only override for the PRIMARY extractor, applied by
    # resolve_endpoints AFTER env-vs-config resolution — so the Console's
    # dreamer picker can switch the served model (e.g. between claude-*
    # tiers on the CLI shim, which honours per-request model names) without
    # flipping extractor_source to "config" and re-owning the env-managed
    # endpoint/fallback wiring. None (default) = inert; the fallback model
    # is never overridden (the sidecar serves one fixed model).
    extractor_model_override: str | None = None
    # Reasoning effort for the PRIMARY extractor, sent as ``reasoning_effort``
    # in the request body. None/"" (default) = the field is never sent, so the
    # endpoint's own default serves — for the CLI shims that means the host
    # CLI config (the 2026-09-01 ladder artifacts ran that way, at the Codex
    # host's "high"). A set value is passed through verbatim: the CLI shims
    # map it to their effort flag per request, OpenAI-compatible servers read
    # it natively, and most local runtimes ignore the unknown field (a hosted
    # API may instead reject an unsupported value with a clear 400). The
    # fallback sidecar never receives it (same rule as the model override).
    extractor_reasoning_effort: str | None = None
    # Output budget for the extractor call. Sized generously so a dense dream
    # batch can emit all its claim JSON without truncation (a truncated response
    # parses to fewer/zero claims). 2048 ≈ 40-80 claims. Override per-deploy with
    # ``PSEUDOLIFE_DREAM_MAX_TOKENS``.
    extractor_max_tokens: int = 2048
    # llama-server's default prompt cache changes extractor OUTPUT once
    # populated (measured: evals/results/warm-cache-probe-0809.json — a warm
    # container answers byte-identical temperature-0 requests differently).
    # The daemon therefore pins ``cache_prompt: false`` on every extractor
    # request by default; the measured cost on the live sidecar is
    # +7.25s/call of shared-prefix prefill
    # (evals/results/sidecar-cache-latency-sidecar-cache-0809.json) — noise
    # for a 600s background sweep. ``None`` restores the server default
    # (cache on) for deployments that prefer the latency; ``True`` forces
    # the cache on explicitly. Non-llama.cpp endpoints ignore the field.
    extractor_cache_prompt: bool | None = False
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
    # Per-dream cap on the retype pass: quarantined untyped pairs re-asked for
    # a TYPED relation using only the notes where both entities co-occur. ~44%
    # of them name a real relationship that merely got the wrong label, and
    # without this the quarantine only accumulates. Self-limiting — the pass
    # no-ops on an empty quarantine, so a drained bank pays nothing.
    # 0 disables.
    retype_quarantined_max: int = 3
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
    # Literal-faithfulness gate (2026-08-02 design doc): digit-bearing
    # tokens in a dream claim's value (outside date-like spans) must appear
    # in the source notes, or the claim is flagged ("log") or dropped
    # ("enforce"). "off" disables. The shipped default is set by measured
    # evidence, not preference: "enforce" since 2026-08-02 — after the
    # matcher learned the legitimate re-formatting classes, the at-scale
    # probes fire almost exclusively on genuinely unbacked literals
    # (derived aggregates, imported world knowledge) at 1.3-1.7% of
    # gateable claims (evals/results/gate-firing-normfix-verdict.json;
    # original decision trail in literal-fidelity-verdict.json).
    literal_gate: str = "enforce"        # "off" | "log" | "enforce"
    # What counts as the source corpus: "batch" (union of the pull's note
    # texts — the default; derived sums and cross-note values are measured
    # false-drop classes under per-note gating) or "source" (only the note
    # the claim cites).
    literal_gate_scope: str = "batch"    # "batch" | "source"
    # Provenance-span gate (2026-08-12 stance+span-gate design, Feature B):
    # the extractor emits a verbatim "quote" from the cited note; the claim
    # loop verifies containment against THAT note (source scope is correct
    # by construction for a quote). "log" counts and logs unbacked claims
    # but still writes them; "contend" parks unbacked SCALAR claims as
    # contenders (span:unbacked provenance marker) — never a silent drop,
    # because span failures include benign paraphrase. Ships "off": the
    # live v5 prompt emits no quotes, so any other default would flag 100%
    # of claims. Flipping to "contend" requires the gate-4 firing audit
    # (evals/results/span-gate-verdict.json), not preference.
    span_gate: str = "off"               # "off" | "log" | "contend"
    # Dream-run audit retention (schema v27): the newest N run rows (and,
    # via CASCADE, their pre-image journals) survive; older ones are pruned
    # during the sweep beside superseded-row compaction. The journal is the
    # rollback source, so this bounds how far back a pass stays revertible.
    runs_keep: int = 50
    # Chronicle events (schema v28): when on, the dream pass runs a second,
    # dedicated events-extraction call per batch (extract_events, pinned
    # artifact evals/prompts/events_pass_v1.txt) and stores dated
    # occurrences into chronicle_events. ON by default since 2026-08-12:
    # the pipeline passed its preregistered gates (separate-pass events +
    # the multi-task sidecar) and the 08-05..08-12 production soak
    # reviewed clean (188 events, 0 incorrect dates, negligible volume).
    # Requires PG; an events-pass failure is non-fatal to claims.
    chronicle: bool = True


@dataclass
class DeepDreamConfig:
    """Manual full-corpus graph consolidation (Phase-2 'C'). See
    docs/superpowers/specs/2026-06-28-deep-dream-graph-consolidation-design.md."""
    min_similarity: float = 0.55       # cosine floor for a link candidate
    top_k_candidates: int = 50         # max candidate pairs emitted per pass
    max_context_snippets: int = 3      # context snippets per entity in a candidate
    auto_apply_safe: bool = True       # auto-supersede violations + merge exact dups (apply only)
    min_entity_mentions: int = 2       # an entity needs >= this many distinct mentioning entries to be candidate-eligible
    # Upper bound on the token-mention FALLBACK scan (trace-less entities
    # only): above it the match set is a corpus centroid, not a context.
    # Default = p90 of the fallback-set size distribution measured on the
    # live bank 2026-08-16 (695 embedded entries, 3792 entities, 1342
    # vector-eligible fallback entities: p50=5, p90=30, p95=59; the incident
    # class sat far above — 'pseudolife-pg' matched 301, 'VS Code' 124 —
    # and filed 9 cross-hub merge pairs in one pass). 0/None disables.
    max_fallback_mentions: int = 30
    merge_min_similarity: float = 0.90   # cosine floor for a near-dup MERGE candidate (vs a link)
    junk_max_degree: int = 1             # junk entities must be this weakly connected to be flagged
    max_support_overlap: float = 0.8     # containment (|shared| / min(|a|,|b|)) on supporting-entry sets at/above which a pair is co-occurrence
    snippet_max_chars: int = 240         # per-snippet truncation in the deep response
    snapshot_keep: int = 10              # graph-snapshot undo files kept under data_dir/graph_snapshots
    curation_min_similarity: float = 0.80  # cosine floor for a lesson/world cross-key duplicate listing; slot embeddings include the key text, so even a verbatim-duplicate value at a different key lands near ~0.82
    curation_top_k: int = 20             # max store-curation pairs listed per store per pass
    # Need-based sweep-tick automation of the MECHANICAL half only (Steps
    # A/B apply: rescore, guard-passing junk auto-delete, scope stamping,
    # proposal filing — all snapshot-first). Step C (judgment) is never
    # automated here; the same need signal is surfaced as
    # dream_status["deep_dream"] so ANY MCP client can nudge its user.
    auto_tick: bool = True               # False disables the tick entirely
    auto_min_new_entities: int = 150     # fire when the bank grew this much since the last apply; 0 disables
    auto_interval_days: float = 7.0      # time backstop since the last apply; 0 disables
    # Autonomous Step-C judge (2026-08-16 design, extended 2026-09-02): the
    # sweep sends pending merge proposals to the configured extractor.
    # "off" = never; "shadow" = record the verdict on the proposal, apply
    # nothing; "auto-reject" = additionally apply reject verdicts at/above
    # judge_reject_min_confidence (decided_by='dream-judge', pair
    # dismissed — and, with judge_second_opinion on, two agreeing rejects
    # at mean >= judge_reject_min_confidence_2); "auto" = additionally
    # fold a pair on two agreeing accepts from DIFFERENT models on
    # non-low-differential evidence (the only path that applies an
    # accept). Note a wrong auto-reject also writes dismissed_pairs, which
    # has no expiry. Mode gates per the judge ladders
    # (evals/judge_ladder.py, evals/queue_judge_ladder.py).
    judge_mode: str = "shadow"           # off | shadow | auto-reject | auto
    judge_batch: int = 8                 # proposals judged per sweep (one model call)
    # Per-snippet cap on the evidence the MERGE JUDGE reads, separate from
    # the review surfaces' snippet_max_chars. 0 = unbounded. Default = the
    # frozen 240 the published judge numbers were measured at, ON PURPOSE:
    # the 2026-09-03 ladder rerun with the same 63 rows at 3000 chars
    # (their source entries run p50 1299 / p95 2765 / max 4282 chars;
    # evals/results/queue-judge-ladder-20260903-fulllen.json) made Opus
    # accept more and be wrong more often — accept precision 0.70 vs 0.85,
    # the two-vote auto-fold gate 6/7 vs 4/4, replicate disagreement 6/63
    # vs 2/63 — while rejects stayed clean. Longer evidence is not better
    # evidence for this judge; raise it only behind a new ladder run. Note
    # the low_differential stamp is computed from the truncated texts, so
    # the cap also moves the auto-accept gate's precondition.
    judge_snippet_max_chars: int = 240
    judge_reject_min_confidence: float = 0.8
    judge_url: str = ""                  # optional OpenAI-compatible override endpoint; empty = the dream extractor
    judge_model: str = ""                # model name for judge_url (ignored when judge_url is empty)
    # One switch for every judge stage below (merge, link, junk, curation,
    # candidates): False makes each return {"skipped": "judges_disabled"}
    # without reading a queue — the documented "turn it all off" for an
    # operator who wants the mechanical tick but no model verdicts. The
    # two apply-time mechanics have their own switches
    # (analyzer_file_duplicates, orphan_sweep).
    judges_enabled: bool = True
    # Review-queue autonomy (2026-09-02 design, docs/superpowers/specs/
    # 2026-09-02-review-queue-autonomy-design.md). Every gate below is
    # measured on the 2026-09-02 blind-panel verdicts (committed as
    # evals/results/queue-judge-panel-20260902.json; the raw evidence pack is
    # private bank content and stays outside the tree), replayed by
    # evals/queue_judge_ladder.py.
    #
    # Merge second opinion + guarded auto-accept. On the 63 residual merge
    # rows (everything the single-vote 0.8 reject gate had left pending),
    # two independent Opus rejects at mean >= 0.7 were 8/8 correct, and two
    # independent accepts on NON-low-differential evidence were 6/6 at mean
    # >= 0.6 — while single-vote accept precision on the same rows was 0.74
    # and 9 of 10 two-vote accepts on low-differential rows were right but
    # the tenth folded the wrong way. A wrong fold deletes an entity, so
    # accepts additionally require judge_mode "auto".
    judge_second_opinion: bool = True
    # A same-model second vote (temperature 0) is independent only through
    # batch composition — 2/129 flips on the 2026-08-16 ladder — which is
    # enough to double-check a reject but not to authorize a fold: "auto"
    # accepts require a DIFFERENT model here (with claude-fable-5 as the
    # second model the same 63 rows gave 6/6 accepts, 8/8 rejects).
    # str | None: the Console setter clears a string knob to None (config_io
    # _coerce); every reader tests truthiness, so "" and None mean the same.
    judge_second_model: str | None = ""  # empty = same endpoint, fresh batch
    judge_reject_min_confidence_2: float = 0.7   # two-vote mean gate
    judge_accept_min_confidence: float = 0.6     # two-vote mean gate ("auto" only)
    # Link judge over pending edge_proposals. Edges are reversible
    # (memory_graph_unrelate / supersede), which is why this is the one
    # queue whose accepts may ship auto: the 2026-09-02 panel accepted 23,
    # retyped 1 and rejected 13 of 37.
    link_judge_mode: str = "shadow"      # off | shadow | auto
    link_accept_min_confidence: float = 0.8
    link_reject_min_confidence: float = 0.8
    # Junk judge over pending junk proposals (the evidence-bearing rows the
    # zero-structure auto-delete skipped). Deletes stay behind an evidence
    # bar even in auto: degree <= junk_max_auto_degree and at most the one
    # fact slot the name was minted from (a lesson-minted object passes by
    # construction — deleting it only nulls the lesson's pointer).
    junk_judge_mode: str = "shadow"      # off | shadow | auto
    junk_keep_min_confidence: float = 0.8
    junk_delete_min_confidence: float = 0.85
    # The 2026-09-02 panel's 12 ratified deletes all sat at degree 1-2
    # (lesson-minted objects: one prefers edge per lesson naming them);
    # nothing above 2 was a delete. The bar is set at the measured edge.
    junk_max_auto_degree: int = 2
    # Store-curation judge over the lesson/world duplicate listings.
    # "distinct" is a reversible dismissal (delete the dismissed_pairs row);
    # "duplicate" forgets a slot and needs the full "auto".
    curation_judge_mode: str = "shadow"  # off | shadow | auto-distinct | auto
    curation_distinct_min_confidence: float = 0.8
    curation_forget_min_confidence: float = 0.9
    curation_rejudge_days: float = 30.0  # a judged pair is not re-sent sooner
    # Step-C candidate judge: turns the deep dream's link CANDIDATES into
    # filed proposals (then settled by the link judge) or dismissed pairs.
    # One judge_batch slice per sweep tick, after each deep apply -- not
    # once per apply.
    candidate_judge_mode: str = "off"    # off | shadow | auto
    candidate_min_confidence: float = 0.6
    candidate_rejudge_days: float = 30.0  # a judged pair (any verdict) is not re-sent sooner
    # Mechanical apply additions: file the Console's live analyzer duplicate
    # findings into the merge / link queues (they were never filed anywhere,
    # so no judge ever saw them), and delete entities that carry no
    # evidence at all — no edge (superseded included), fact, lesson, alias,
    # scope, proposal or mentioning entry — once older than
    # orphan_min_age_days (50 such on the 2026-09-02 live bank).
    analyzer_file_duplicates: bool = True
    # Off by default for one release: it is the only destructive default
    # that would fire on the first apply after an upgrade, on a bank whose
    # census nobody has read. Flip it on after reading `orphans_deleted`
    # in a dry census (the storage helper) — and it stays capped per pass.
    orphan_sweep: bool = False
    orphan_min_age_days: float = 7.0
    orphan_max_per_apply: int = 50       # 0 = uncapped


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
    # Slot read telemetry (schema v33): count each slot served by fact_get /
    # cortex search into slot_reads. Kill switch, not a tuning constant —
    # one small upsert per fact-serving call, PG-only.
    read_tracking: bool = True
    # TypeRetrieve (schema v35, arXiv 2608.22752): pin in-scope
    # CONSTRAINT-labelled facts ahead of cosine in memory_search's cortex
    # block (query names the entity) and memory_recall (seed entities).
    # Kill switch, not a tuning constant: an unlabelled bank is served
    # identically either way. Retrieval-affecting when labels exist.
    pin_constraints: bool = True


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
    # Synthesis-time cross-key near-duplicate gate: a SYNTHESIZED lesson
    # whose embedding hits an existing current lesson at a DIFFERENT key
    # with the SAME polarity at/above this cosine is skipped (counted as
    # ``deduped``). The store re-minted the same deploy/triage lessons at
    # fresh keys every session (five folded on 2026-08-12 alone). 0.88 is
    # deliberately above the 0.80 curation-listing floor: a false skip
    # silently loses a new lesson, a missed dup merely waits for curation.
    # Opposite-polarity near-matches are NEVER gated (an "avoid" inversion
    # of a "do" lesson is new information). 0 disables.
    synthesis_dedup_min_similarity: float = 0.88


@dataclass
class RetrievalLogConfig:
    """Append-only per-query retrieval log (schema v31/v32) — the training
    data for a future learned fusion/reranker stage. Every ``memory_search``
    writes one ``retrieval_events`` row: query text, the ranked served list
    with each entry's ranking *components* (bi-encoder / cross-encoder /
    BM25 scores, surprise, recency, multipliers), and the ``params``
    snapshot of the knobs in force. A later ``memory_get``/
    ``memory_reinforce`` on a served entry in the same session writes an
    implicit relevance label to ``retrieval_uses``. Purely observational:
    no retrieval behaviour changes, and nothing is computed for the log
    that ranking did not already compute. Requires Postgres storage (file
    mode skips silently); ``memory_stats`` reports the row counts and
    write-failure count."""
    enabled: bool = True
    # Events older than this are pruned on the dream-sweep tick (labels
    # CASCADE), bounding growth. Generous by default: the log IS the
    # training corpus, and rows carry no texts — only ids, scores and the
    # per-entry component floats. The v32 components/params widened a row
    # from roughly a few hundred bytes to a couple of KB at a full top-k
    # serve, so retention is now the only thing bounding a busy bank's
    # log; lower it if the table outgrows its usefulness.
    retention_days: int = 365
    # A get/reinforce this many seconds after a search still counts as a
    # use of it. Bounds the implicit-label lookback so a stale id fetched
    # much later doesn't credit an ancient query.
    use_window_seconds: int = 3600


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
    # Search fan-out caps. Measured 2026-09-04, `evals/recall_fanout_bench.py`
    # on a restored copy of the live bank (1,296 entries, 5,504 entities,
    # flat preset; 20 relational questions, CPU, top_k=6, hops=3 —
    # `evals/results/recall-fanout-cap-20260904.json`): the uncapped walk
    # issued a mean of 89.15 `service.search` calls per recall (max 205)
    # and took 25.25 s (max 57.67 s), against 0.26 s for a plain search on
    # the same questions. Two live recall calls timed out at the MCP layer
    # that morning. The cause is structural: one re-query per newly
    # discovered entity per hop on a star-shaped graph (degree p50 1 /
    # p95 5 / max 132), so one hub's ring prices the whole call.
    #
    # Per hop, re-query only the top-N newly discovered entities (seed-hit
    # mentions first, then lowest degree); the rest are still returned as
    # entities with their facts. 6 is above that degree p95 of 5, so a
    # typical spoke's whole neighborhood is still re-queried and only hub
    # rings are cut. At 6 the same 20 questions cost a mean of 12.40
    # searches / 4.166 s with no expected target lost. 0 = unlimited
    # (pre-2026-09-04 behavior).
    max_searches_per_hop: int = 6
    # Hard ceiling per recall call, seed search included; on reaching it
    # the walk stops and the response carries `truncated: true` +
    # `searches_issued`. Sized to be a genuine backstop rather than a
    # binding constraint: `memory_recall` advertises `hops` clamped to
    # 1..5, and a full 5-hop walk under the per-hop cap above costs at
    # most 1 + 6 x 5 = 31, so at 31 no request the tool accepts can be cut
    # by this ceiling — only a raised per-hop cap can reach it. (It was 20
    # when the 2026-09-04 run above was measured; that run was hops=3, cost
    # at most 19 searches, and never tripped the ceiling on any of the 20
    # questions, so the artifact's numbers are unchanged by this default.)
    # 0 = no ceiling.
    max_total_searches: int = 31
    # Wall-clock fail-soft: past this the walk returns what it has with
    # `truncated: true` instead of running on. Above the 7.51 s worst
    # capped call measured above and well inside the MCP client timeout
    # the two live calls hit. 0 = no budget.
    time_budget_seconds: float = 20.0
    # `part-of` is this bank's filler relation — 19% of live edges, and
    # 1,046 of the 1,763 entities the 2026-09-04 run's recalls added
    # arrived through `part-of` alone. When True, such an entity is still
    # returned with its facts but never spends a search. Default False:
    # the knob exists so the eval can measure what dropping those
    # re-queries costs before any default changes.
    skip_part_of_expansion: bool = False


# The retrieval channel-fusion modes. Named here so the config's
# load-time validation and ``cms.retrieve``'s per-query belt cannot drift
# apart, and so a new mode is added in one place.
FUSION_MODES = ("weighted_sum", "rrf")


@dataclass
class SearchConfig:
    """Aggregation-aware retrieval knobs (Phase 1, spec
    2026-08-03-aggregation-aware-recall-design.md) plus the candidate-pool
    shape knobs added 2026-09-04. All default OFF (or to the shipped
    behaviour) until the preregistered gates pass; the eval harness pins
    its control arm to vanilla retrieval via per-call overrides regardless
    of these values."""

    # Temporal-contiguity expansion (EM-LLM, arXiv:2407.09450): each search
    # hit also surfaces up to N temporal neighbors per side — same episode,
    # falling back to same source — marked ``via: "contiguity"`` in the
    # response. 0 = off.
    contiguity_neighbors: int = 0
    # Timeline channel: queries with temporal cues ("first", "how many
    # times", month names…) get lexically-relevant entries injected and the
    # final result ordered ascending by timestamp — the presentation the
    # raw-turns control arm gets for free and consolidation loses.
    timeline_channel: bool = False
    # Serving-side staleness policy (spec 2026-08-09-serving-side-staleness-
    # design.md). ret-0809 measured that the annotation flags halve
    # unqualified stale serving but compliance keys on value shape — a
    # daemon-side policy removes that answerer discretion. Applied at the
    # shared record render sites, so every read surface behaves identically;
    # version history (audit chain) is deliberately exempt.
    #   "annotate"   — flags only (today's behavior, default)
    #   "demote"     — stale records sort after non-stale on list surfaces
    #                  and carry a top-level ``warning``
    #   "quarantine" — a stale record's ``value`` is replaced by a wrapper
    #                  and the original moves to ``last_known_value``
    stale_policy: str = "annotate"
    # Retrieve-then-rerank width. Each band's DENSE candidate pool becomes
    # ``top_k * candidate_pool_multiplier`` (band-size capped); the final
    # truncation to ``top_k`` is unchanged. 1 (default) is the shipped
    # path, byte-identical to the pre-knob code and pinned by
    # tests/test_retrieval_pool.py::
    # test_multiplier_one_matches_captured_prechange_output.
    #
    # Why this is a knob at all: under ``miras.preset = "flat"`` (the
    # default since 2026-08-15) there is ONE band, so the dense pool for
    # the whole bank was exactly the served width — BM25 fusion, the slot
    # pool and the cross-encoder all re-ranked a set that dense retrieval
    # had already cut to size, and the reranker's ``top_n = 20`` budget
    # never saw more than ~11 candidates. Not a tuned constant, and not
    # unmeasured either: multiplier 4 was run through a judged eval on
    # 2026-09-04 and LOST under both fusions (see the verdict on
    # ``fusion`` below), so it ships at 1.
    candidate_pool_multiplier: int = 1
    # How the dense / slot / BM25 / timeline channels are merged.
    #   "weighted_sum" — today's behaviour: BM25 contributes
    #                    ``weight x normalised`` additively to the dense
    #                    score and every channel's score is then raw-sorted
    #                    together, despite the scales being incommensurate
    #                    (cosine, 0.55-0.95 slot confidence, 0.3 x
    #                    normalised BM25).
    #   "rrf"          — reciprocal rank fusion over the four channels'
    #                    RANK lists, which needs no shared scale. Source
    #                    and supersession multipliers stay ranking-only
    #                    modifiers, applied to the fused score; recency
    #                    rides inside the dense channel's own rank order.
    # Ships "weighted_sum" for the same measured reason as the multiplier
    # above.
    #
    # CAUTION — "rrf" changes the SCALE of every served score, not just the
    # order: a fused score is a sum of 1/(60 + rank) terms, so it tops out
    # around 0.05 (typically 0.016-0.05) where a cosine reaches 1.0. Any
    # threshold, weight or margin tuned on the cosine scale silently
    # changes meaning. Four known sites, all pinned in
    # tests/test_retrieval_pool.py:
    #
    #   ``memory.search_confidence_floor`` — set it to 0 (its default)
    #     before enabling rrf, or memory_search abstains on everything;
    #     the same hazard the reranker's ``skip_margin`` comment
    #     documents, in the other direction.
    #   ``memory.reranker.fusion_weight`` — reranker.fuse computes
    #     ``w * ce + (1 - w) * orig``. With ``orig`` on the rrf scale the
    #     bi-encoder term is worth at most 0.3 x 0.05 = 0.015, so a
    #     cross-encoder difference of ~0.02 outranks the ENTIRE fused
    #     ranking: rrf plus the reranker is cross-encoder-only ordering.
    #   ``memory.reranker.skip_margin`` — a nonzero margin tuned on
    #     cosines can never be reached by fused scores, so the gate never
    #     skips and the ~200ms pass it was added to avoid always runs.
    #   the reference bank (Pool 2) — reference documents keep their RAW
    #     cosines (~0.9), which are not rescaled onto the fused scale.
    #     They trail positionally with the reranker off, but the moment
    #     the reranker fires they sort above every memory on the
    #     ``(1 - w) * orig`` term alone.
    #
    # Plainly: do NOT combine "rrf" with the cross-encoder reranker or a
    # populated reference bank until that combination has been measured.
    # The judged verdict below covers rrf with the reranker OFF and an
    # empty reference bank; nothing else is measured.
    #
    # Judged verdict (2026-09-04, LongMemEval knowledge-update oracle,
    # n=78, qwen-27b extraction): "rrf" at multiplier 4 LOSES —
    # rag 0.744 vs 0.859 control (-0.115), hybrid 0.833 vs 0.897
    # (-0.064). Artifacts:
    # evals/results/longmemeval-ku-oracle-qwen-27b-pool-{ctl,m4rrf}.*
    # and evals/results/compare-pool-m4rrf-pairs.json; the table is in
    # evals/README.md. Ships "weighted_sum" for that reason, not for want
    # of measurement.
    fusion: str = "weighted_sum"

    def __post_init__(self) -> None:
        # Fail at LOAD, not once per query. ``cms.retrieve`` also rejects
        # an unknown mode, but that raise fires inside every retrieval, so
        # a typo in config.yaml would surface as a burst of failing
        # searches on a daemon that started clean. Validating here turns
        # it into a refusal to start. The retrieve() check stays as the
        # belt: config objects reach it by paths that never run this
        # (per-attribute setattr, eval harnesses, Console saves).
        if self.fusion not in FUSION_MODES:
            raise ValueError(
                f"memory.search.fusion: unknown mode {self.fusion!r} "
                f"(expected one of {', '.join(map(repr, FUSION_MODES))})")


@dataclass
class McpConfig:
    """What an AGENT pays per MCP tool call (spec: the 2026-09-04 agent-side
    token ledger, ``evals/agent_token_ledger.py``).

    Every published "fewer tokens" figure to date measured *served benchmark
    context* — the answerer's prompt — never the payload a real MCP client
    reads back from a tool. The ledger measured that side; these knobs gate
    the cuts it justified, as ONE switch:

    * ``memory_search`` entry ``text`` capped at ``entry_text_chars`` with a
      ``truncated: true`` marker (``memory_get`` returns the full text);
    * the cortex block sized to the caller's ``top_k`` rather than a fixed 5;
    * ``memory_fact_get``'s bookkeeping keys behind ``verbose=True``.

    ``compact_payloads: False`` restores the pre-cut payloads verbatim. All
    three are PROJECTIONS above ``service.*`` — ranking, ``min_score`` and
    the service layer are untouched, so no eval number can move (the eval
    harness calls the service, pinned by
    ``tests/test_agent_payload_budget.py``).
    """

    compact_payloads: bool = True
    # 600 chars ≈ 150 tokens. Measured 2026-09-04 over 15 dev-session
    # queries at top_k=8 against the live bank (1,316 entries, 120 served
    # entries) — evals/results/agent-token-ledger-20260904-r3.json: served
    # entry text runs mean 1,180 chars / median 1,149 / p90 1,794, and
    # entry text alone was 64% of the whole search payload. A 600-char cap
    # therefore clips 88% of hits ON THIS BANK, deliberately: these are
    # consolidated notes, not one-liners, and 600 chars is enough to judge
    # a hit and usually to act on it, with ``memory_get`` for the rest. It
    # halves the served entry text (9,464 -> 4,550 mean chars) and takes
    # 33% off the call. It does NOT apply to ``superseded_by_text``, which
    # is exempt: that field has no recovery path, since a compact entry
    # carries no id for the superseding entry (see ``_compact_entry``).
    # Raise it for long-form corpora where the tail of a
    # note carries the answer. ``memory_recall`` has capped its supporting
    # texts at 200 since 2026-07-10 (``_RECALL_TEXT_CHARS``); search
    # entries are the primary answer rather than walk evidence, so they
    # get the wider cap.
    entry_text_chars: int = 600


@dataclass
class MemoryConfig:
    embedding_dim: int = 384
    # MIRAS (v0.5+) — preset-driven per-band specification. The default
    # ``titans`` preset reproduces the v0.4.x flat TITANS band defaults
    # bit-for-bit, so behaviour is unchanged for anyone who doesn't opt into
    # a different preset.
    miras: MIRASConfig = field(default_factory=MIRASConfig)
    # Reference bank (RAG via ChromaDB)
    reference: ReferenceConfig = field(default_factory=ReferenceConfig)
    # NLI contradiction-detection (fourth path)
    nli: NLIConfig = field(default_factory=NLIConfig)
    # BM25 sparse lexical pool, fused with dense retrieval (Tier B2).
    bm25: BM25Config = field(default_factory=BM25Config)
    # Cross-encoder reranker over the merged retrieval pool (Tier B).
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    # Aggregation-aware retrieval knobs (contiguity / timeline; Phase 1).
    search: SearchConfig = field(default_factory=SearchConfig)
    # Dream pass — MIRAS→cortex consolidation (pluggable extractor).
    dream: DreamConfig = field(default_factory=DreamConfig)
    # Manual full-corpus graph consolidation (deep dream, Phase-2 'C').
    deep_dream: DeepDreamConfig = field(default_factory=DeepDreamConfig)
    # Cortex — sibling slot-keyed canonical-fact store (schema v7).
    cortex: CortexConfig = field(default_factory=CortexConfig)
    # Procedural / outcome memory — lessons store (schema v10).
    lessons: LessonsConfig = field(default_factory=LessonsConfig)
    # Superseded-row compaction (keep-newest-N + min-age; spec 2026-07-14).
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    # memory_recall — live MemCoT iterative retrieval (read-only).
    recall: RecallConfig = field(default_factory=RecallConfig)
    # MCP payload shaping — the agent-side token cost of a tool call.
    mcp: McpConfig = field(default_factory=McpConfig)
    # Topology analytics computed during dream (Track B).
    graph_insight: GraphInsightConfig = field(default_factory=GraphInsightConfig)
    # Engram cross-index (provenance-as-link, schema v13).
    traces: TracesConfig = field(default_factory=TracesConfig)
    # Retrieval event log — learned-reranker training data (schema v31).
    retrieval_log: RetrievalLogConfig = field(
        default_factory=RetrievalLogConfig)
    # Project-scope derivation — meta-source exclusions + umbrella rollups.
    scopes: ScopesConfig = field(default_factory=ScopesConfig)
    # Meta-statement filter on the store path (off in the MCP build).
    meta_filter: MetaFilterConfig = field(default_factory=MetaFilterConfig)
    # Base recency half-life at band depth 0; doubles per depth.
    # 3600 (1h) suits chat; the MCP build sets 86400 (1 day).
    recency_base_half_life_s: float = 3600.0
    # Depth-ramped recency boost on retrieval. OFF since 2026-07-25: the
    # ramp treats band depth as a proxy for age, but depth is set by
    # promotion history — which, without retrieval to accrue access counts,
    # is driven by surprise rather than age. Measured cost of leaving it on:
    # up to 18 points on the LongMemEval naive-RAG arm. Flip to True to
    # restore the pre-2026-07-25 ranking.
    recency_boost_enabled: bool = False
    # v0.5 store gate is novelty-based (1 - max cos to existing entries). 0.0 =
    # permissive (store everything; novelty still scores eviction/promotion);
    # raise to dedup near-duplicate stores.
    surprise_threshold: float = 0.0
    top_k: int = 8       # episodic retrieval slots across bands
    ref_top_k: int = 3   # max reference bank results injected alongside memories
    save_dir: str = "./memory_state"
    # When False (default, since v0.7.3), entries marked superseded by the
    # contradiction pipeline stay retrievable — merely downranked — so the
    # agent can narrate the historical sequence ("you used to have X, then
    # you said Y"). Flip to True to restore the v0.7.2 hard filter, which
    # hides them. Hiding is lossy: it caused the cat-category retrieval
    # failure (see cms.retrieve) and superseded rows carry knowledge-update
    # recall, so prefer the default outside of debugging.
    # Replaced the no-op ``show_superseded`` field on 2026-07-30.
    hide_superseded: bool = False
    # Abstention: when the top search score is below this floor, memory_search
    # returns low_confidence=True so the agent declines instead of using weak
    # distractor hits. 0.0 = off (only an empty result is low-confidence).
    # Tuned on a dev split by the benchmark ladder; default off to preserve recall.
    search_confidence_floor: float = 0.0
    # Shadow-verification of the slot-token index: on this fraction of
    # non-dirty slot-pool queries, recompute the index from the band
    # entries and compare membership against the live copy. A divergence
    # means some mutation path neither extended the index nor flagged it
    # dirty; it is logged, counted in stats(), and self-repaired by
    # adopting the fresh copy. 0.0 disables; 1.0 checks every query
    # (tests). The sampled rebuild is the whole cost, so keep the rate
    # small in production.
    slot_index_shadow_rate: float = 0.01


@dataclass
class ContextConfig:
    max_memory_tokens: int = 2000


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
            # Fallbacks here must mirror the dataclass defaults (pinned by
            # test_yaml_memory_block_omitted_keys_keep_dataclass_defaults):
            # a divergent literal means a config.yaml that has a memory:
            # block but omits the key runs a different value than no file.
            embedding_dim=mem_raw.get("embedding_dim", 384),
            surprise_threshold=mem_raw.get("surprise_threshold", 0.0),
            top_k=mem_raw.get("top_k", 8),
            ref_top_k=mem_raw.get("ref_top_k", 3),
            save_dir=mem_raw.get("save_dir", "./memory_state"),
            hide_superseded=mem_raw.get("hide_superseded", False),
            search_confidence_floor=mem_raw.get("search_confidence_floor", 0.0),
            recency_base_half_life_s=mem_raw.get("recency_base_half_life_s", 3600.0),
            recency_boost_enabled=mem_raw.get("recency_boost_enabled", False),
            slot_index_shadow_rate=mem_raw.get("slot_index_shadow_rate", 0.01),
        )
        if "miras" in mem_raw:
            miras_raw = mem_raw["miras"]
            # ``bands`` is a list of dicts → list of :class:`MIRASBandSpec`.
            bands_raw = miras_raw.get("bands", []) or []
            bands = [_dict_to_dataclass(MIRASBandSpec, b) for b in bands_raw]
            # Construction triggers __post_init__ which overrides ``bands`` from
            # the preset registry for non-custom presets — see :class:`MIRASConfig`.
            # Fallback must match the dataclass default (flat since
            # 2026-08-15) — a miras block that omits ``preset`` must not
            # resolve differently than no miras block at all.
            config.memory.miras = MIRASConfig(
                preset=miras_raw.get("preset", "flat"),
                bands=bands,
            )
        if "reference" in mem_raw:
            config.memory.reference = _dict_to_dataclass(ReferenceConfig, mem_raw["reference"])
        if "nli" in mem_raw:
            config.memory.nli = _dict_to_dataclass(NLIConfig, mem_raw["nli"])
        if "search" in mem_raw:
            config.memory.search = _dict_to_dataclass(
                SearchConfig, mem_raw["search"])
        if "bm25" in mem_raw:
            config.memory.bm25 = _dict_to_dataclass(
                BM25Config, mem_raw["bm25"],
            )
        if "reranker" in mem_raw:
            config.memory.reranker = _dict_to_dataclass(
                RerankerConfig, mem_raw["reranker"],
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
        if "mcp" in mem_raw:
            config.memory.mcp = _dict_to_dataclass(McpConfig, mem_raw["mcp"])
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
        if "retrieval_log" in mem_raw:
            config.memory.retrieval_log = _dict_to_dataclass(
                RetrievalLogConfig, mem_raw["retrieval_log"],
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
    if "storage" in raw:
        config.storage = _dict_to_dataclass(StorageConfig, raw["storage"])
    if "time" in raw:
        config.time = _dict_to_dataclass(TimeConfig, raw["time"])

    return config
