"""MemoryService — high-level wrapper over the PseudoLife memory stack.

One ``MemoryService`` per data directory. The MCP server (see
:mod:`pseudolife_memory.mcp_server`) holds a single instance for the
process lifetime and routes every MCP tool call through one of the
methods below. All methods return plain-JSON-serialisable dicts /
lists so the MCP layer can ``json.dumps`` them without further work.

Design notes
------------
* **No LLM dependency.** Reflection / HyDE were dropped from this build —
  Claude is the LLM, so the natural way to reflect is for Claude to call
  ``memory_store`` with a summary it composes itself. Contrastive stays
  because it doesn't need an LLM.

* **No silent fallbacks.** PseudoLife's chat path swallows memory errors so
  the user's conversation never breaks. For an MCP tool Claude is calling
  deliberately, errors should surface — so this layer lets exceptions
  propagate and the MCP server converts them into structured error
  responses.

* **Source = tag.** The MCP exposes a ``source`` parameter on every store
  for free-form tagging ("pseudolife", "general", "v0.7.6"). Retrieval can
  filter by source list. Multi-tag support could land later as a
  schema-versioned addition.

* **Lazy init.** Embedder + CMS are constructed on the first method call,
  not in ``__init__``. Keeps the MCP startup fast (Claude's tool list
  loads even if torch / sentence-transformers are slow to warm up).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.consolidation import (
    Cluster,
    cluster_candidates,
)
from pseudolife_memory.memory.context_builder import ContextBuilder
from pseudolife_memory.memory.contrastive import ContrastiveUpdater
from pseudolife_memory.memory.embedding import EmbeddingPipeline
from pseudolife_memory.memory.reference_bank import ReferenceBank
from pseudolife_memory.memory.reranker import CrossEncoderReranker
from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.utils.config import (
    AppConfig,
    ContextConfig,
    EmbeddingConfig,
    MemoryConfig,
    ReferenceConfig,
    load_config,
)

logger = logging.getLogger(__name__)


def _entry_to_dict(
    entry: MemoryEntry,
    score: float | None = None,
    *,
    include_embedding: bool = False,
) -> dict[str, Any]:
    """Serialise a :class:`MemoryEntry` for MCP transport.

    The embedding tensor is dropped by default — it's a 384-float vector
    that bloats the response and is meaningless to the LLM consumer.
    Pass ``include_embedding=True`` only for debug tooling.
    """
    out: dict[str, Any] = {
        "text": entry.text,
        "source": entry.source,
        "bank": entry.bank,
        "timestamp": entry.timestamp,
        "access_count": entry.access_count,
        "surprise_score": round(entry.surprise_score, 4),
        "superseded": entry.superseded_at is not None,
        "superseded_by_text": entry.superseded_by_text,
        # Tier C (schema v6) — None / [] for entries stored before
        # episodes / tags existed, so MCP responses never crash on legacy
        # state.
        "episode_id": entry.episode_id,
        "episode_title": entry.episode_title,
        "tags": list(entry.tags),
    }
    if entry.slots:
        out["slots"] = [
            {"entity": e, "attribute": a, "value": v, "polarity": p}
            for (e, a, v, p) in entry.slots
        ]
    if score is not None:
        out["score"] = round(float(score), 4)
    if include_embedding:
        out["embedding"] = entry.embedding.detach().cpu().tolist()
    return out


def _cortex_record_to_dict(rec) -> dict[str, Any]:
    """Serialise a :class:`CortexRecord` for transport (JSON-safe)."""
    return {
        "entity": rec.entity,
        "attribute": rec.attribute,
        "value": rec.value,
        "polarity": rec.polarity,
        "status": rec.status,
        "confidence": round(float(rec.confidence), 4),
        "origin": rec.origin,
        "support": sorted(rec.support),
        "provenance": sorted(rec.provenance),
        "asserted_at": rec.asserted_at,
        "last_confirmed": rec.last_confirmed,
        "supersedes_value": rec.supersedes_value,
        "superseded_by_value": rec.superseded_by_value,
        "superseded_at": rec.superseded_at,
    }


def _world_record_to_dict(rec, now=None) -> dict[str, Any]:
    """Serialise a WorldRecord for transport, with read-time effective confidence
    (age-decayed) and a stale flag, plus the per-fact citation."""
    return {
        "entity": rec.entity,
        "attribute": rec.attribute,
        "value": rec.value,
        "polarity": rec.polarity,
        "status": rec.status,
        "confidence": round(float(rec.confidence), 4),
        "effective_confidence": round(float(rec.effective_confidence(now)), 4),
        "stale": bool(rec.is_stale(now)),
        "origin": rec.origin,
        "freshness_class": rec.freshness_class,
        "source_url": rec.source_url,
        "source_quote": rec.source_quote,
        "retrieved_at": rec.retrieved_at,
        "asserted_at": rec.asserted_at,
        "last_confirmed": rec.last_confirmed,
        "supersedes_value": rec.supersedes_value,
        "superseded_by_value": rec.superseded_by_value,
        "superseded_at": rec.superseded_at,
    }


# Map a store ``source`` tag to a cortex ``origin`` tier (provenance-of-kind).
# MCP can't see the conversation, so origin is defaulted from source (or set
# explicitly by the caller). Unknown sources -> None (origin left blank).
_SOURCE_ORIGIN = {
    "conversation": "user", "user": "user",
    "claude": "agent", "assistant": "agent", "agent": "agent",
    "tool": "action", "action": "action",
}


def _origin_from_source(source: str | None) -> str | None:
    return _SOURCE_ORIGIN.get((source or "").strip().lower())


class MemoryService:
    """Thin orchestration over CMS + embedder + reference bank + contrastive.

    Construct once per process. All public methods are thread-safe via
    a single coarse ``_lock`` — the MCP server is sequential per
    connection but we don't want concurrent ``store`` and ``save`` to
    race on torch state.
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        database_url: str | None = None,
    ) -> None:
        self._lock = Lock()
        # Schema v8: when a database URL is configured (param or
        # PSEUDOLIFE_MCP_DATABASE_URL), Postgres is the source of truth
        # and the in-memory bands are a write-through cache. Without it,
        # the v0.1 file mode is preserved bit-for-bit.
        self._db_url = database_url or os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
        self._storage = None
        # Resolve data directory first — that's where memory_state lives
        # AND where the default config sits (if config_path not given).
        self.data_dir = Path(data_dir) if data_dir else Path.cwd() / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        if config_path is None:
            # Sentinel for "use defaults" — load_config returns an
            # AppConfig() when the file doesn't exist.
            cfg_candidate = self.data_dir / "config.yaml"
            self.config = load_config(cfg_candidate)
        else:
            self.config = load_config(config_path)

        # Override save_dir so memory tensors land inside data_dir even
        # when the config wasn't tailored for this install.
        self.config.memory.save_dir = str(self.data_dir / "memory_state")
        self.config.memory.reference.persist_dir = str(self.data_dir / "chromadb")

        # Defaults that make sense for the *Claude* use-case differ from
        # the human-chat defaults shipped with PseudoLife — see README.
        self._apply_mcp_defaults(self.config)

        # Lazy components — built on first use.
        self._embedder: EmbeddingPipeline | None = None
        self._cms: ContinuumMemorySystem | None = None
        self._reference: ReferenceBank | None = None
        self._contrastive: ContrastiveUpdater | None = None
        self._context_builder: ContextBuilder | None = None
        self._reranker: CrossEncoderReranker | None = None
        self._cortex: CortexStore | None = None
        self._world = None  # WorldCortexStore | None (world-knowledge cortex, v9)
        self._age = None  # AgeGraph mirror when the extension is present
        self._last_user_query: str | None = None
        self._last_saved_fingerprint = None

    # ------------------------------------------------------------------
    # Lazy construction
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_mcp_defaults(config: AppConfig) -> None:
        """Tweak PseudoLife defaults for the MCP / Claude use case.

        Differences from the user-facing chat defaults:

        * Lower ``surprise_threshold`` (0.3 → 0.2): Claude stores
          deliberately, so the gate doesn't need to be aggressive.
        * Smaller embedder batch size: MCP calls one-at-a-time, no point
          paying the warmup overhead of a large batch.
        * Meta-filter OFF: it exists to drop auto-captured chat noise;
          every MCP store is a deliberate tool call.
        * Recency base half-life 24h (vs 1h): Claude Code sessions are
          hours-to-days apart, so a 1h half-life made the recency boost
          effectively always zero.

        Leaves the MIRAS preset alone — the ``continuum`` 8-tier default
        is fine for Claude's use too.
        """
        config.memory.surprise_threshold = 0.2
        config.embedding.batch_size = 16
        config.memory.meta_filter.enabled = False
        config.memory.recency_base_half_life_s = 86400.0

    def _ensure_init(self) -> None:
        if self._cms is not None:
            return
        logger.info("MemoryService: initialising embedder + CMS (first call).")
        self._embedder = EmbeddingPipeline(self.config.embedding)
        # Make sure the embedder dim matches the configured memory dim —
        # MiniLM-L6 is 384-d. Other embedders would need config tuning.
        self.config.memory.embedding_dim = self._embedder.embedding_dim
        try:
            self._reference = ReferenceBank(
                self.config.memory.reference,
                embedding_dim=self._embedder.embedding_dim,
            )
        except Exception as exc:  # noqa: BLE001
            # ChromaDB is optional — if it fails to start (corrupt DB,
            # missing dep), continue without the reference bank. Memory
            # tier still works.
            logger.warning("ReferenceBank disabled: %s", exc)
            self._reference = None
        # Reranker is always *constructible* (the model is lazy-loaded on
        # the first rerank()), so attach one unconditionally and let the
        # rerank-enabled flag in cms.retrieve gate actual firing. Reading
        # from config means a user can disable the reranker entirely by
        # setting config.memory.reranker.enabled = False without paying
        # any cost.
        self._reranker = CrossEncoderReranker(
            model_name=self.config.memory.reranker.model_name,
            fusion_weight=self.config.memory.reranker.fusion_weight,
            top_n=self.config.memory.reranker.top_n,
        )
        if self._db_url:
            from pseudolife_memory.storage.postgres import PostgresStorage
            self._storage = PostgresStorage(self._db_url)
            logger.info("storage: postgres (%s)",
                        self._db_url.rsplit("@", 1)[-1])
            if self._storage.capabilities.get("age_available"):
                try:
                    from pseudolife_memory.storage.age import AgeGraph
                    self._age = AgeGraph(self._storage.conn)
                    logger.info("AGE graph mirror active")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("AGE init failed (Cypher layer off): %s", exc)
        self._cms = ContinuumMemorySystem(
            self.config.memory,
            reference_bank=self._reference,
            reranker=self._reranker,
            storage=self._storage,
        )
        if self._storage is not None:
            from pseudolife_memory.storage import migrate as _migrate
            from pseudolife_memory.storage import sync as _sync
            try:
                summary = _migrate.migrate_legacy(self.data_dir, self._storage)
                if summary.get("migrated"):
                    logger.warning("legacy .pt bank migrated: %s", summary)
            except Exception as exc:  # noqa: BLE001
                logger.warning("legacy migration failed (continuing): %s", exc)
            n = _sync.hydrate_cms(self._cms, self._storage)
            logger.info("hydrated %d entries from storage", n)
            try:
                self._cms.load_weights(self.config.memory.save_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("weights load skipped: %s", exc)
        else:
            # File mode (v0.1) — restore persisted state if any.
            try:
                self._cms.load(self.config.memory.save_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("CMS load skipped: %s", exc)

        self._contrastive = ContrastiveUpdater(
            self.config.memory.contrastive, self._embedder,
        )
        self._context_builder = ContextBuilder(self.config.context)

        # Cortex — sibling slot-keyed canonical-fact store (schema v7).
        # Co-persisted next to memory_state; deliberately outside the
        # band / promotion / decay machinery.
        cc = self.config.memory.cortex
        self._cortex = CortexStore(
            supersede_confidence_margin=cc.supersede_confidence_margin,
            reinforce_rate=cc.reinforce_rate,
            protect_provenance=cc.protect_provenance,
        )
        if self._storage is not None:
            from pseudolife_memory.storage import sync as _sync
            try:
                _sync.hydrate_cortex(self._cortex, self._storage)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cortex hydration skipped: %s", exc)
        else:
            try:
                self._cortex.load(self._cortex_path())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cortex load skipped: %s", exc)

        # World-knowledge cortex (schema v9) — sibling slot store for sourced
        # EXTERNAL facts, persisted in its own world_facts table. Hydrated like
        # the cortex; Postgres-only (no .pt fallback — it is a v0.2+ feature).
        from pseudolife_memory.memory.world_cortex import WorldCortexStore
        self._world = WorldCortexStore()
        if self._storage is not None:
            from pseudolife_memory.storage import sync as _sync
            try:
                _sync.hydrate_world_cortex(self._world, self._storage)
            except Exception as exc:  # noqa: BLE001
                logger.warning("World cortex hydration skipped: %s", exc)

    # ------------------------------------------------------------------
    # Tool: store
    # ------------------------------------------------------------------

    def store(
        self,
        text: str,
        source: str = "claude",
        tags: list[str] | None = None,
        origin: str | None = None,
    ) -> dict[str, Any]:
        """Embed and store a memory through the CMS pipeline.

        ``tags`` (schema v6) is an optional multi-valued label list.
        Normalised by the underlying CMS (lowercased / stripped /
        deduped). Tags exist alongside ``source``, not as a replacement.

        ``origin`` (``"user"`` / ``"action"`` / ``"agent"``) records who asserted
        any canonical facts auto-promoted from this text into the cortex. When
        omitted it is defaulted from ``source`` (conversation->user, claude->
        agent, tool->action). See :meth:`_promote_slots`.

        Returns ``{"stored": bool, "surprise": float, "reason": str|None,
        "cortex_promoted": int}``. Stores can be rejected by either the
        meta-filter (looks like self-reference) or the surprise gate (already
        known) — the ``reason`` field surfaces which.
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cms is not None
            text = (text or "").strip()
            if not text:
                return {"stored": False, "surprise": 0.0, "reason": "empty",
                        "cortex_promoted": 0}
            embedding = self._embedder.encode_single(text)
            stored, surprise = self._cms.store(
                text, embedding, source=source, tags=tags,
            )
            reason: str | None = None
            if not stored:
                # Mirror the gates in CMS.store so callers know why.
                if surprise == 0.0:
                    reason = "filtered_meta"
                elif surprise < self.config.memory.surprise_threshold:
                    reason = "below_surprise_threshold"
                else:
                    reason = "rejected"
            # Deterministic cortex promotion: lift slot-shaped facts into the
            # canonical layer with NO model cooperation (the no-LLM floor). Runs
            # on a real store AND on a restatement (below_surprise_threshold) so
            # re-asserting a known fact still confirms its slot — but never on
            # meta-filtered junk.
            promoted = 0
            cc = self.config.memory.cortex
            if cc.enabled and cc.auto_promote and reason != "filtered_meta":
                promoted = self._promote_slots(text, source=source, origin=origin)
                if promoted and self._storage is not None:
                    self._save_cortex()
            return {
                "stored": stored,
                "surprise": round(float(surprise), 4),
                "reason": reason,
                "cortex_promoted": promoted,
            }

    def _promote_slots(self, text: str, *, source: str, origin: str | None) -> int:
        """Lift any slot-shaped facts in ``text`` into the cortex deterministically
        (regex ``extract_slots``, no LLM). Caller MUST already hold ``self._lock``
        — writes go straight to ``self._cortex`` (not via ``cortex_write``, which
        would re-acquire the non-reentrant lock). Returns the number written."""
        from pseudolife_memory.memory.slots import extract_slots
        assert self._cortex is not None and self._embedder is not None
        sup = origin if origin is not None else _origin_from_source(source)
        conf = self.config.memory.cortex.promote_confidence
        prov = [source] if source else []
        written = 0
        for s in extract_slots(text):
            value = s.value if getattr(s, "polarity", "+") != "-" else ("NOT " + s.value)
            claim = f"{s.entity} {s.attribute} {value}".strip()
            try:
                self._cortex.write_fact(
                    Slot(s.entity, s.attribute, value),
                    self._embedder.encode_single(claim),
                    confidence=conf,
                    provenance=prov,
                    support=sup,
                )
                self._ensure_subject_entity(s.entity)
                written += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("cortex auto-promote skipped (%s): %s", claim, exc)
        return written

    def _ensure_subject_entity(self, entity: str) -> None:
        """Fact writes create the subject's graph node (spec §5.1) so the
        cortex and graph stay joined. No-op in file mode. Caller holds the
        lock."""
        if self._storage is None:
            return
        from pseudolife_memory.graph import norm_name
        n = norm_name(entity)
        if n and self._storage.find_entity(n) is None:
            self._storage.ensure_entity(n, display=entity.strip())
            self._age_mirror(lambda: self._age.upsert_entity(n, entity.strip()))

    def _age_mirror(self, op) -> None:
        """Run an AGE mirror op best-effort — the tables are the truth and
        ``age-sync`` can rebuild the mirror, so a failure only logs."""
        if self._age is None:
            return
        try:
            op()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AGE mirror op failed (run age-sync to heal): %s", exc)

    # ------------------------------------------------------------------
    # Tool: search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int | None = None,
        sources: list[str] | None = None,
        bands: list[str] | None = None,
        episodes: list[str] | None = None,
        tags: list[str] | None = None,
        min_score: float | None = None,
        disable_recency_boost: bool = False,
        rerank: bool | None = None,
        bm25: bool | None = None,
    ) -> dict[str, Any]:
        """Retrieve relevant memories ranked by associative similarity.

        ``sources`` and ``bands`` filter the result set: only entries whose
        ``source`` / ``bank`` match the supplied list survive. ``None`` means
        no filter on that axis.

        ``min_score`` overrides the relevance keep-threshold (default 0.25).
        Lower it to widen recall when the bank is sparse; raise it to drop
        weak hits. ``disable_recency_boost=True`` short-circuits the
        per-band recency uplift so ranking depends on raw similarity ×
        source-multiplier × supersession only — useful for state-probe
        queries where popularity bias is unwelcome.

        ``rerank`` overrides ``config.memory.reranker.enabled``:

        * ``None`` (default) — follow the config flag.
        * ``True`` — apply the cross-encoder reranker on the top-N
          candidates even if config disables it. First call lazy-loads
          ``cross-encoder/ms-marco-MiniLM-L-6-v2`` (~80MB).
        * ``False`` — skip reranking even if config enables it.

        ``bm25`` overrides ``config.memory.bm25.enabled``:

        * ``None`` (default) — follow the config flag.
        * ``True`` — run BM25 sparse-lexical retrieval in parallel with
          the dense pool and fuse the two. Catches exact-keyword queries
          (function names, version strings, error codes) that dense
          retrieval can underweight. Pure-stdlib, no extra deps.
        * ``False`` — skip BM25 even if config enables it.
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cms is not None
            query = (query or "").strip()
            if not query:
                return {"entries": [], "query": "", "count": 0, "low_confidence": True}
            embedding = self._embedder.encode_single(query)
            result = self._cms.retrieve(
                embedding,
                top_k=top_k,
                bands=bands,
                sources=sources,
                episodes=episodes,
                tags=tags,
                query_text=query,
                min_score=min_score,
                disable_recency_boost=disable_recency_boost,
                rerank=rerank,
                bm25=bm25,
            )
            # Stash the query so memory_supersede / contrastive flows have
            # something to anchor against on the next call.
            self._last_user_query = query
            from pseudolife_memory.memory.abstain import low_confidence
            return {
                "query": query,
                "count": len(result.entries),
                "low_confidence": low_confidence(
                    list(result.scores),
                    self.config.memory.search_confidence_floor,
                ),
                "entries": [
                    _entry_to_dict(e, s)
                    for e, s in zip(result.entries, result.scores)
                ],
            }

    # ------------------------------------------------------------------
    # Tool: trace — search + structured ranking trace
    # ------------------------------------------------------------------

    def trace(
        self,
        query: str,
        top_k: int | None = None,
        sources: list[str] | None = None,
        bands: list[str] | None = None,
        episodes: list[str] | None = None,
        tags: list[str] | None = None,
        rerank: bool | None = None,
        bm25: bool | None = None,
    ) -> dict[str, Any]:
        """Like :meth:`search` but also returns the structured ranking trace.

        Use to diagnose retrieval misses: each tier's candidates show
        raw_score, recency, source/supersession multipliers, and the
        drop_reason (or ``kept=True``) — so callers can see *why* a
        fact didn't surface.

        ``rerank`` plumbs the cross-encoder override through to
        ``cms.retrieve_with_trace`` so the trace ``reranker`` field
        records both whether it fired and per-candidate ce/fused scores.

        ``bm25`` plumbs the BM25 override through; when enabled, the
        trace's ``bm25`` field records raw + normalised scores per hit
        and any BM25-only injections.
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cms is not None
            query = (query or "").strip()
            if not query:
                return {
                    "query": "", "count": 0, "entries": [], "trace": None,
                }
            embedding = self._embedder.encode_single(query)
            result, trace = self._cms.retrieve_with_trace(
                embedding,
                top_k=top_k,
                bands=bands,
                sources=sources,
                episodes=episodes,
                tags=tags,
                query_text=query,
                rerank=rerank,
                bm25=bm25,
            )
            return {
                "query": query,
                "count": len(result.entries),
                "entries": [
                    _entry_to_dict(e, s)
                    for e, s in zip(result.entries, result.scores)
                ],
                "trace": trace,
            }

    # ------------------------------------------------------------------
    # Tool: recent
    # ------------------------------------------------------------------

    def recent(
        self,
        n: int = 10,
        sources: list[str] | None = None,
        episodes: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """List the N most recently stored memories across all bands.

        Useful for debugging ("what did I just store?"). Unlike
        ``search``, this returns by ``timestamp`` not relevance.

        ``episodes`` and ``tags`` (schema v6) AND-combine with
        ``sources``. Each filter is OR within itself.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            source_filter = set(sources) if sources else None
            episode_filter = set(episodes) if episodes else None
            # Tag filter mirrors retrieval semantics: normalised, set
            # intersection non-empty test.
            from pseudolife_memory.memory.episodes import normalize_tags as _norm
            tag_filter = set(_norm(tags)) if tags else None
            all_entries: list[MemoryEntry] = []
            for band in self._cms.bands:
                for entry in band.entries:
                    if source_filter and entry.source not in source_filter:
                        continue
                    if episode_filter and entry.episode_id not in episode_filter:
                        continue
                    if tag_filter and not (set(entry.tags) & tag_filter):
                        continue
                    all_entries.append(entry)
            all_entries.sort(key=lambda e: e.timestamp, reverse=True)
            limited = all_entries[: max(0, int(n))]
            return {
                "count": len(limited),
                "entries": [_entry_to_dict(e) for e in limited],
            }

    # ------------------------------------------------------------------
    # Tool: list_sources — source-tag taxonomy
    # ------------------------------------------------------------------

    def list_sources(self) -> dict[str, Any]:
        """Enumerate the source tags currently in the bank, with counts.

        Use before ``search`` / ``recent`` / ``delete`` to discover what
        tags exist instead of guessing. Returns
        ``{"sources": [{"source": str, "count": int}, ...], "total": N}``,
        sorted by count descending.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            counts: dict[str, int] = {}
            total = 0
            for band in self._cms.bands:
                for entry in band.entries:
                    counts[entry.source] = counts.get(entry.source, 0) + 1
                    total += 1
            rows = sorted(
                ({"source": s, "count": c} for s, c in counts.items()),
                key=lambda r: (-r["count"], r["source"]),
            )
            return {"sources": rows, "total": total}

    # ------------------------------------------------------------------
    # Tool: supersede
    # ------------------------------------------------------------------

    def supersede(self, old_text: str, new_text: str) -> dict[str, Any]:
        """Explicit correction: mark entries matching ``old_text`` as
        superseded by ``new_text``, then store ``new_text`` itself.

        Matching is by exact-text first, falling back to top-1 embedding
        retrieval — so a near-paraphrase of the wrong fact still gets
        caught even if the user phrasing drifted.
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cms is not None
            old_text = (old_text or "").strip()
            new_text = (new_text or "").strip()
            if not old_text or not new_text:
                return {"superseded_count": 0, "reason": "empty_input"}

            now = time.time()
            superseded: list[str] = []
            superseded_entries: list[MemoryEntry] = []

            # Exact-text pass.
            for band in self._cms.bands:
                for entry in band.entries:
                    if entry.text == old_text and entry.superseded_at is None:
                        entry.superseded_at = now
                        entry.superseded_by_text = new_text
                        superseded.append(entry.text)
                        superseded_entries.append(entry)

            # If no exact match, fall back to top-1 retrieval on old_text.
            if not superseded:
                emb = self._embedder.encode_single(old_text)
                result = self._cms.retrieve(emb, top_k=1, query_text=old_text)
                if result.entries:
                    target = result.entries[0]
                    if target.superseded_at is None:
                        target.superseded_at = now
                        target.superseded_by_text = new_text
                        superseded.append(target.text)
                        superseded_entries.append(target)

            # Write-through the supersession marks.
            if self._storage is not None:
                for e in superseded_entries:
                    if e.db_id is not None:
                        self._storage.update_entry(
                            e.db_id,
                            superseded_at=e.superseded_at,
                            superseded_by_text=e.superseded_by_text,
                        )

            # Always store the correction text as a regular memory so future
            # retrieval surfaces the new state.
            store_emb = self._embedder.encode_single(new_text)
            stored, surprise = self._cms.store(
                new_text, store_emb, source="correction",
            )
            return {
                "superseded_count": len(superseded),
                "superseded_texts": superseded,
                "new_memory_stored": stored,
                "new_memory_surprise": round(float(surprise), 4),
            }

    # ------------------------------------------------------------------
    # Tool: delete — hygiene
    # ------------------------------------------------------------------

    def delete(
        self,
        text: str | None = None,
        substring: str | None = None,
        source: str | None = None,
        episode: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        """Remove memories matching any of the provided filters.

        At least one filter is required — bare ``delete()`` raises
        ``ValueError`` so accidental "delete everything" is impossible.
        For a wholesale wipe use ``CMS.clear()`` via the maintenance path,
        not this tool.

        Returns ``{"deleted_count": N, "deleted_texts": [...]}``. The
        sample of deleted texts is capped at 20 so MCP responses stay
        small even on large purges.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            removed = self._cms.delete_entries(
                text=text, substring=substring, source=source,
                episode=episode, tag=tag,
            )
            return {
                "deleted_count": len(removed),
                "deleted_texts": removed[:20],
            }

    # ------------------------------------------------------------------
    # Tool: stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Sizes, capacities, hit rates per band + reference bank summary."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            return self._cms.stats()

    # ------------------------------------------------------------------
    # Tool: ingest_document
    # ------------------------------------------------------------------

    def ingest_document(
        self, path: str, source: str | None = None,
    ) -> dict[str, Any]:
        """Read a file (txt/md/pdf) and chunk-store it in the reference bank.

        Returns ``{"source": ..., "chunks_stored": N}``.
        """
        with self._lock:
            self._ensure_init()
            if self._reference is None:
                raise RuntimeError(
                    "Reference bank disabled (ChromaDB init failed). "
                    "Documents cannot be ingested.",
                )
            assert self._embedder is not None
            file_path = Path(path)
            if not file_path.exists():
                raise FileNotFoundError(f"Not found: {file_path}")
            result = self._reference.ingest_file(
                file_path, source=source, embedder=self._embedder,
            )
            return {
                "source": source or file_path.name,
                "chunks_stored": result.get("chunks_stored", 0),
                "chunks_total": result.get("chunks_total", 0),
            }

    # ------------------------------------------------------------------
    # Tool: search_documents
    # ------------------------------------------------------------------

    def search_documents(
        self, query: str, top_k: int = 5,
    ) -> dict[str, Any]:
        """RAG search over the reference bank only — no neural memories."""
        with self._lock:
            self._ensure_init()
            if self._reference is None:
                return {"count": 0, "entries": []}
            assert self._embedder is not None
            embedding = self._embedder.encode_single(query)
            result = self._reference.retrieve(embedding, top_k=top_k)
            return {
                "count": len(result.entries),
                "entries": [
                    _entry_to_dict(e, s)
                    for e, s in zip(result.entries, result.scores)
                ],
            }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> dict[str, Any]:
        """Persist CMS state. ChromaDB persists itself."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            out = self._persist_all(kind="explicit")
            self._last_saved_fingerprint = self._entry_fingerprint()
            return out

    def _entry_fingerprint(self):
        """Cheap signature of mutating state: (live entry count, superseded
        count). Changes on store/delete/supersede/consolidate; unaffected by
        reads/searches. Gates the background autosave so idle periods cost no
        disk writes. Caller must already hold self._lock."""
        if self._cms is None:
            return None
        total = 0
        superseded = 0
        for band in self._cms.bands:
            entries = band.entries
            total += len(entries)
            for e in entries:
                if getattr(e, "superseded_at", None) is not None:
                    superseded += 1
        # Fold the cortex into the signature so confirm (last_confirmed bump),
        # insert (record count) and supersede (log growth) all trigger autosave.
        cortex_sig = (0, 0, 0.0)
        if self._cortex is not None:
            recs = self._cortex.records
            cortex_sig = (
                len(recs),
                len(self._cortex.supersession_log),
                round(sum(r.last_confirmed for r in recs), 3),
            )
        return (total, superseded, cortex_sig)

    def autosave_if_changed(self):
        """Flush CMS tensors only if mutating state changed since last save.
        Driven by the background autosave loop in mcp_server."""
        with self._lock:
            if self._cms is None:
                return None
            fp = self._entry_fingerprint()
            if fp == self._last_saved_fingerprint:
                return None
            out = self._persist_all(kind="auto")
            self._last_saved_fingerprint = fp
            out["auto"] = True
            return out

    def flush(self):
        """Unconditional save for clean-exit / signal handlers. Captures the
        latest state (incl. band migrations). No-op if never initialised."""
        with self._lock:
            if self._cms is None:
                return None
            out = self._persist_all(kind="flush")
            self._last_saved_fingerprint = self._entry_fingerprint()
            out["flush"] = True
            return out

    # ------------------------------------------------------------------
    # Cortex — sibling slot-keyed canonical-fact store (schema v7)
    # ------------------------------------------------------------------

    def _cortex_path(self) -> str:
        return str(self.data_dir / "cortex_state.pt")

    def _save_cortex(self) -> None:
        if self._cortex is None:
            return
        try:
            if self._storage is not None:
                from pseudolife_memory.storage import sync as _sync
                _sync.snapshot_cortex(self._cortex, self._storage)
            else:
                self._cortex.save(self._cortex_path())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cortex save failed: %s", exc)

    def _save_world(self) -> None:
        if getattr(self, "_world", None) is None or self._storage is None:
            return
        try:
            from pseudolife_memory.storage import sync as _sync
            _sync.snapshot_world_cortex(self._world, self._storage)
        except Exception as exc:  # noqa: BLE001
            logger.warning("World cortex save failed: %s", exc)

    def _persist_all(self, *, kind: str) -> dict[str, Any]:
        """Shared body of save/autosave/flush. Caller holds the lock.

        Storage mode: weights are the only file artifact (atomic);
        entries are already transactional in PG, so we only sync the
        lazily-updated access counts and snapshot the cortex.
        File mode: legacy full-bank torch.save (v0.1 behavior).
        """
        assert self._cms is not None
        if self._storage is not None:
            self._cms.save_weights(self.config.memory.save_dir)
            pairs = [
                (e.db_id, e.access_count)
                for band in self._cms.bands
                for e in band.entries
                if e.db_id is not None
            ]
            try:
                self._storage.update_access_counts(pairs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("access-count sync failed: %s", exc)
            self._save_cortex()
            return {"saved_to": self.config.memory.save_dir,
                    "mode": "postgres+weights", "kind": kind}
        self._cms.save(self.config.memory.save_dir)
        self._save_cortex()
        return {"saved_to": self.config.memory.save_dir, "kind": kind}

    def cortex_write(
        self,
        entity: str,
        attribute: str,
        value: str,
        *,
        confidence: float = 0.7,
        provenance: list[str] | None = None,
        support: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Write / confirm / supersede a canonical fact at the
        ``(entity, attribute)`` slot. The claim is embedded through the same
        pipeline as memories so cortex search shares the embedding space.

        ``support`` records who asserted the fact — ``"user"`` (the human stated
        it), ``"action"`` (a tool/agent action confirmed it), or ``"agent"`` (the
        agent merely said it). It accumulates on the record (``origin`` = the
        strongest tier seen), so corroboration is first-class.

        Returns ``{"action": "inserted"|"confirmed"|"superseded"|"contested",
        ...record fields}``. On ``"contested"`` the returned record is the parked
        *contender* and a ``"current"`` key carries the canonical value that won,
        so the caller sees both sides of the conflict.
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cortex is not None
            claim = f"{entity} {attribute} {value}".strip()
            emb = self._embedder.encode_single(claim)
            slot_emb = self._embedder.encode_single(f"{entity} {attribute}".strip())
            res = self._cortex.write_fact(
                Slot(entity, attribute, value),
                emb,
                slot_embedding=slot_emb,
                confidence=confidence,
                provenance=provenance or (),
                support=support,
                now=now,
            )
            self._ensure_subject_entity(entity)
            self._save_cortex()
            out = {"action": res.action, **_cortex_record_to_dict(res.record)}
            if res.action == "contested":
                cur = self._cortex.lookup(entity, attribute)
                out["current"] = _cortex_record_to_dict(cur) if cur is not None else None
            return out

    def cortex_lookup(self, entity: str, attribute: str) -> dict[str, Any] | None:
        """Exact slot lookup — the one ``current`` fact, or ``None``.

        Alias-aware: on a direct slot miss, the entity name is resolved through
        the graph's ``entity_aliases`` (Postgres) and the canonical name is
        retried, so a fact stored under e.g. ``dev-box`` surfaces regardless of
        which alias (``4090``) the caller queried — honouring the contract that
        every fact lookup resolves aliases first."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            rec = self._cortex.lookup(entity, attribute)
            if rec is None and self._storage is not None:
                from pseudolife_memory.graph import norm_name
                node = self._storage.find_entity(norm_name(entity))
                if node is not None:
                    canon = node.get("canonical")
                    if canon and norm_name(canon) != norm_name(entity):
                        rec = self._cortex.lookup(canon, attribute)
            return _cortex_record_to_dict(rec) if rec is not None else None

    def cortex_contenders(self, entity: str, attribute: str) -> dict[str, Any]:
        """Active contenders parked at a slot — a conflicting lower-tier / below-
        margin value that did NOT supersede the current fact (0 or 1)."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            recs = self._cortex.contenders_for(entity, attribute)
            return {
                "entity": entity, "attribute": attribute,
                "contenders": [_cortex_record_to_dict(r) for r in recs],
            }

    def cortex_resolve(self, entity: str, attribute: str, accept: bool) -> dict[str, Any]:
        """Promote (accept) or retire (reject) the active contender at a slot.
        Persists. Returns ``{"resolved": False, "reason": "no_contender"}`` when
        there is nothing parked to resolve."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            res = self._cortex.resolve(entity, attribute, accept)
            if res is None:
                return {"resolved": False, "reason": "no_contender",
                        "entity": entity, "attribute": attribute}
            self._save_cortex()
            cur = self._cortex.lookup(entity, attribute)
            return {
                "resolved": True,
                "accepted": bool(accept),
                "action": res.action,
                "current": _cortex_record_to_dict(cur) if cur is not None else None,
                "record": _cortex_record_to_dict(res.record),
            }

    def cortex_search(
        self, query: str, top_k: int = 5, min_score: float = 0.0,
    ) -> dict[str, Any]:
        """Fuzzy search over ``current`` canonical facts only. Each entry is
        flagged ``"contested": bool`` and, when true, carries
        ``"contender_value"`` / ``"contender_origin"`` for the parked rival, so a
        discrepancy is visible during normal recall."""
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cortex is not None
            emb = self._embedder.encode_single(query)
            hits = self._cortex.search(emb, top_k=top_k, min_score=min_score)
            entries = []
            for r, s in hits:
                d = {**_cortex_record_to_dict(r), "score": round(float(s), 4)}
                conts = self._cortex.contenders_for(r.entity, r.attribute)
                if conts:
                    d["contested"] = True
                    d["contender_value"] = conts[0].value
                    d["contender_origin"] = conts[0].origin
                else:
                    d["contested"] = False
                entries.append(d)
            return {"count": len(entries), "entries": entries}

    def cortex_stats(self) -> dict[str, Any]:
        """Cortex sizes: total / current / superseded / slots."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            return self._cortex.stats()

    def cortex_vocab(self, limit: int = 120) -> dict[str, Any]:
        """Existing canonical slot keys (entity.attribute), for the dream
        extractor to reuse — the prompt-side half of key normalisation."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            slots = self._cortex.vocab(limit)
            return {"slots": slots, "count": len(slots)}

    # ── world-knowledge cortex (schema v9) ──────────────────────────────

    def world_write(self, entity: str, attribute: str, value: str, *,
                    confidence: float = 0.7, source_url: str = "",
                    source_quote: str = "", freshness_class: str = "volatile",
                    retrieved_at: float | None = None, content_hash: str | None = None,
                    source_doc_id: int | None = None, now: float | None = None) -> dict[str, Any]:
        """Assert a canonical WORLD fact (origin=source). Newer source supersedes."""
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._world is not None
            emb = self._embedder.encode_single(f"{entity} {attribute} {value}".strip())
            action, rec = self._world.write_fact(
                entity, attribute, value, emb,
                confidence=confidence, source_url=source_url, source_quote=source_quote,
                freshness_class=freshness_class, retrieved_at=retrieved_at,
                content_hash=content_hash, source_doc_id=source_doc_id, now=now)
            self._save_world()
            return {"action": action, **_world_record_to_dict(rec)}

    def world_lookup(self, entity: str, attribute: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_init()
            assert self._world is not None
            rec = self._world.lookup(entity, attribute)
            return _world_record_to_dict(rec) if rec is not None else None

    def world_search(self, query: str, top_k: int = 5, min_score: float = 0.0) -> dict[str, Any]:
        """Fuzzy search over current world facts; entries carry decayed
        effective_confidence + stale flag + citation."""
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._world is not None
            emb = self._embedder.encode_single(query)
            hits = self._world.search(emb, top_k=top_k, min_score=min_score)
            entries = [{**_world_record_to_dict(r), "score": round(float(s), 4)}
                       for r, s in hits]
            return {"count": len(entries), "entries": entries}

    def world_dump(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            assert self._world is not None
            rows = [_world_record_to_dict(r) for r in self._world.current_records()]
            rows.sort(key=lambda d: (d["entity"].lower(), d["attribute"].lower()))
            return {"count": len(rows), "entries": rows}

    def world_forget(self, entity: str, attribute: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            assert self._world is not None
            removed = self._world.forget(entity, attribute)
            if removed:
                self._save_world()
            return {"removed": removed, "entity": entity, "attribute": attribute}

    def cortex_dump(self) -> dict[str, Any]:
        """All current canonical facts (entity, attribute, value, origin, …) for
        introspection / cleanup. Sorted by (entity, attribute)."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            rows = [_cortex_record_to_dict(r) for r in self._cortex.current_records()]
            rows.sort(key=lambda d: (d["entity"].lower(), d["attribute"].lower()))
            if self._storage is not None:
                from pseudolife_memory.graph import norm_name
                emap = self._storage.entity_id_map()
                for d in rows:
                    d["entity_id"] = emap.get(norm_name(d["entity"]))
            return {"count": len(rows), "entries": rows}

    def cortex_forget(self, entity: str, attribute: str | None = None) -> dict[str, Any]:
        """Hard-delete facts at an entity (or one exact slot). Persists. Use for
        purging test / garbage facts — normal corrections go through supersession."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            removed = self._cortex.forget(entity, attribute)
            if removed:
                self._save_cortex()
            return {"removed": removed, "entity": entity, "attribute": attribute}

    # ------------------------------------------------------------------
    # Dream pass — episode cursor + regex floor (LLM step is gateway-side)
    # ------------------------------------------------------------------

    def dream_pull(self, limit: int = 20) -> dict[str, Any]:
        """Recent episodic conversation turns not yet consolidated (timestamp >
        cortex.dream_cursor), oldest-first, capped at ``limit``. The gateway
        runs LLM/regex extraction over these, then calls ``dream_commit``."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None and self._cortex is not None
            cfg = self.config.memory.dream
            excluded = set(cfg.exclude_sources or [])
            allowed = set(cfg.eligible_sources) if cfg.eligible_sources else None
            cursor = self._cortex.dream_cursor
            rows: list[MemoryEntry] = []
            for band in self._cms.bands:
                for e in band.entries:
                    if allowed is not None:
                        if e.source not in allowed:
                            continue
                    elif e.source in excluded:
                        continue
                    if e.timestamp <= cursor:
                        continue
                    rows.append(e)
            rows.sort(key=lambda e: e.timestamp)
            rows = rows[: max(0, int(limit))]
            return {
                "cursor": cursor,
                "count": len(rows),
                "entries": [
                    {
                        "text": e.text,
                        "timestamp": e.timestamp,
                        "episode_id": e.episode_id,
                    }
                    for e in rows
                ],
            }

    def extract_slots_regex(self, texts: list[str]) -> dict[str, Any]:
        """Deterministic no-LLM claim-extraction floor (delegates to
        ``RegexExtractor`` so the regex implementation lives in exactly one
        place). The gateway dream uses this when the active model yields nothing
        usable."""
        from pseudolife_memory.memory.dream import RegexExtractor
        claims = RegexExtractor().extract(list(texts or []), vocab=[])
        return {"claims": [{"entity": c["entity"], "attribute": c["attribute"],
                            "value": c["value"], "confidence": c["confidence"]}
                           for c in claims]}

    def dream_commit(self, cursor: float) -> dict[str, Any]:
        """Advance the dream cursor (monotonic) and persist it with the cortex."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            c = float(cursor or 0.0)
            if c > self._cortex.dream_cursor:
                self._cortex.dream_cursor = c
                self._save_cortex()
            return {"dream_cursor": self._cortex.dream_cursor}

    def _resolve_dream_slot(self, entity: str, attribute: str) -> tuple[str, str]:
        """Map a dreamed claim's (entity, attribute) onto an existing current slot
        when a confident value-free slot-embedding match exists, so a paraphrased
        update supersedes instead of forking a sibling. Dream-path only; returns
        the original pair when disabled, on an exact-key hit, or below threshold.
        Never raises — a resolver failure falls back to the original slot."""
        threshold = float(self.config.memory.cortex.dream_slot_match_threshold)
        if threshold <= 0.0:
            return entity, attribute
        try:
            with self._lock:
                self._ensure_init()
                assert self._embedder is not None and self._cortex is not None
                # Exact slot already exists -> let the normal write path supersede.
                if self._cortex.lookup(entity, attribute) is not None:
                    return entity, attribute
                slot_emb = self._embedder.encode_single(
                    f"{entity} {attribute}".strip())
                match = self._cortex.resolve_slot(slot_emb, threshold)
            return match or (entity, attribute)
        except Exception as exc:  # noqa: BLE001 — resolution must never break a dream
            logger.warning("dream slot resolve failed (%s); using literal slot", exc)
            return entity, attribute

    def dream_run(self, extractor, *, limit: int | None = None) -> dict[str, Any]:
        """One dream cycle: pull eligible unconsolidated memories, extract claims
        via ``extractor`` (regex floor fallback if it yields nothing), write each
        to the cortex, advance the dream cursor. Returns a summary. The single
        consolidation path shared by the MCP tool and (later) the daemon sweep."""
        from pseudolife_memory.memory.dream import RegexExtractor
        cap = int(limit if limit is not None else self.config.memory.dream.max_batch)
        pulled = self.dream_pull(limit=cap)
        entries = pulled["entries"]
        if not entries:
            return {"pulled": 0, "claims": 0, "inserted": 0, "confirmed": 0,
                    "contested": 0, "superseded": 0, "cursor": pulled["cursor"]}
        texts = [e["text"] for e in entries]
        vocab = self.cortex_vocab().get("slots", [])
        try:
            claims = extractor.extract(texts, vocab)
        except Exception as exc:  # noqa: BLE001 — an extractor must never break a dream
            logger.warning("dream extractor failed (%s); using regex floor", exc)
            claims = []
        if not claims:
            claims = RegexExtractor().extract(texts, vocab)
        tally = {"inserted": 0, "confirmed": 0, "contested": 0, "superseded": 0}
        for c in claims:
            ent, attr = self._resolve_dream_slot(c["entity"], c["attribute"])
            res = self.cortex_write(
                ent, attr, c["value"],
                confidence=float(c.get("confidence", 0.55)),
                support=c.get("origin", "agent"),
            )
            tally[res["action"]] = tally.get(res["action"], 0) + 1
        newest = max(e["timestamp"] for e in entries)
        self.dream_commit(newest)
        return {"pulled": len(entries), "claims": len(claims),
                "cursor": newest, **tally}

    def dream_status(self) -> dict[str, Any]:
        """Backlog (eligible unconsolidated memories), idle seconds since the most
        recent store, and whether the trigger would fire. Read-only — safe for a
        SessionStart nudge hook."""
        import time as _t
        cfg = self.config.memory.dream
        backlog = self.dream_pull(limit=10**9)["count"]
        with self._lock:
            self._ensure_init()
            assert self._cms is not None and self._cortex is not None
            latest = max(
                (e.timestamp for b in self._cms.bands for e in b.entries),
                default=0.0,
            )
            cursor = self._cortex.dream_cursor
        idle = (_t.time() - latest) if latest else 0.0
        would_fire = bool(cfg.enabled and (
            backlog >= cfg.min_batch
            or (backlog >= 1 and idle >= cfg.idle_seconds)
        ))
        return {"backlog": backlog, "idle_seconds": idle,
                "dream_cursor": cursor, "would_fire": would_fire}

    def warmup(self):
        """Eagerly load embedder + reranker + NLI so the first real tool call
        is warm. Safe to run in a background thread at startup."""
        try:
            with self._lock:
                self._ensure_init()
                self._last_saved_fingerprint = self._entry_fingerprint()
        except Exception as exc:  # noqa: BLE001
            logger.warning("warmup init failed: %s", exc)
            return
        try:
            self.search("warmup probe", top_k=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("warmup search failed: %s", exc)

    # ------------------------------------------------------------------
    # Tier C — episode lifecycle + tag hygiene
    # ------------------------------------------------------------------

    @staticmethod
    def _episode_to_dict(ep) -> dict[str, Any]:
        """Serialise an :class:`Episode` for MCP transport."""
        return {
            "id": ep.id,
            "title": ep.title,
            "started_at": ep.started_at,
            "ended_at": ep.ended_at,
            "hint": ep.hint,
            "closed_by_new_start": ep.closed_by_new_start,
        }

    def episode_start(
        self, title: str, hint: str | None = None,
    ) -> dict[str, Any]:
        """Open a new episode. Auto-closes any currently-open episode.

        Every memory stored while the episode is open carries
        ``episode_id`` / ``episode_title`` for later episode-scoped
        retrieval. Returns the freshly-opened episode dict.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            ep = self._cms.episodes.start(title=title, hint=hint)
            self._persist_episodes()
            return self._episode_to_dict(ep)

    def episode_end(self) -> dict[str, Any]:
        """Close the currently-open episode. Empty dict if none open."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            closed = self._cms.episodes.end()
            self._persist_episodes()
            return self._episode_to_dict(closed) if closed is not None else {}

    def _persist_episodes(self) -> None:
        """Write-through the episode log (small; start auto-closes priors,
        so a full upsert sweep is the simplest correct sync). Caller holds
        the lock. No-op in file mode."""
        if self._storage is None or self._cms is None:
            return
        from pseudolife_memory.storage.sync import episode_row
        try:
            for ep in self._cms.episodes.episodes.values():
                self._storage.upsert_episode(episode_row(ep))
        except Exception as exc:  # noqa: BLE001
            logger.warning("episode write-through failed: %s", exc)

    def episode_list(
        self, limit: int = 20, include_open: bool = True,
    ) -> dict[str, Any]:
        """List episodes newest-first, with per-episode entry counts.

        Counts walk all bands once and bucket by ``episode_id``, so they
        match what retrieval would see — entries promoted to deeper
        bands are still counted under their original episode.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            eps = self._cms.episodes.list(
                limit=limit, include_open=include_open,
            )
            counts: dict[str, int] = {}
            for band in self._cms.bands:
                for entry in band.entries:
                    if entry.episode_id:
                        counts[entry.episode_id] = counts.get(entry.episode_id, 0) + 1
            rows = []
            for ep in eps:
                row = self._episode_to_dict(ep)
                row["entry_count"] = counts.get(ep.id, 0)
                rows.append(row)
            return {"count": len(rows), "episodes": rows}

    def episode_summary(self, id: str) -> dict[str, Any]:
        """Return stats + tag distribution + recent entries for an episode.

        Returns ``{"found": False, "id": id}`` when the id is unknown so
        callers can branch without parsing an error.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            ep = self._cms.episodes.get(id)
            if ep is None:
                return {"found": False, "id": id}

            entries: list[MemoryEntry] = []
            for band in self._cms.bands:
                for e in band.entries:
                    if e.episode_id == id:
                        entries.append(e)
            entries.sort(key=lambda e: e.timestamp, reverse=True)

            tag_counts: dict[str, int] = {}
            for e in entries:
                for t in e.tags:
                    tag_counts[t] = tag_counts.get(t, 0) + 1
            tag_rows = sorted(
                ({"tag": t, "count": c} for t, c in tag_counts.items()),
                key=lambda r: (-r["count"], r["tag"]),
            )

            source_counts: dict[str, int] = {}
            for e in entries:
                source_counts[e.source] = source_counts.get(e.source, 0) + 1
            source_rows = sorted(
                ({"source": s, "count": c} for s, c in source_counts.items()),
                key=lambda r: (-r["count"], r["source"]),
            )

            return {
                "found": True,
                **self._episode_to_dict(ep),
                "entry_count": len(entries),
                "tag_distribution": tag_rows,
                "source_distribution": source_rows,
                # Cap recent entries — even a small dict times N entries
                # gets unwieldy on long episodes. Use ``memory_recent``
                # filtered by episode for the full list.
                "recent_entries": [_entry_to_dict(e) for e in entries[:20]],
            }

    # ------------------------------------------------------------------
    # Tier C — consolidation workflow
    # ------------------------------------------------------------------

    def consolidation_candidates(
        self,
        query: str | None = None,
        episode: str | None = None,
        sources: list[str] | None = None,
        tags: list[str] | None = None,
        top_k: int = 20,
        min_cohesion: float = 0.6,
        min_cluster_size: int = 2,
        max_clusters: int = 10,
    ) -> dict[str, Any]:
        """Surface clusters of mutually-similar memories for consolidation.

        Two modes:

        * **Query-driven** (``query`` given): embed the query, run
          retrieval through the standard CMS pipeline (so filters /
          rerank / BM25 all apply), then cluster the top-N hits by
          mutual similarity. Returns clusters scoped to the topic.
        * **Episode-scoped** (``query=None``, ``episode`` given): walk
          the episode's entries directly, treat them as the candidate
          pool, cluster. Returns clusters within the session — useful
          for "summarise what we worked on" style consolidation.

        The clustering algorithm is exposed in
        :mod:`pseudolife_memory.memory.consolidation`. This method is
        glue: filter + score → cluster → serialise.

        Args:
            query: Topic to consolidate around. None when episode-scoping.
            episode: Restrict to this episode id. AND-combined with the
                tag / source filters.
            sources / tags: Same semantics as ``search``.
            top_k: Max candidates considered. Beyond this, the candidate
                pool is too noisy for clustering to be meaningful.
            min_cohesion: Min cosine between seed and cluster member.
                Default 0.6 is conservative — surface only clearly-
                related groups.
            min_cluster_size: Drop clusters with fewer members.
                Default 2 (the natural floor).
            max_clusters: Hard cap on returned clusters.

        Returns:
            ``{"query": str|None, "episode": str|None, "count": int,
            "clusters": [{"cohesion", "seed_score", "size", "members":
            [<entry>...]}, ...]}``. Each member is the same dict shape
            as ``search``'s entries — text, source, tags, episode,
            timestamp, etc.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None and self._embedder is not None

            # Build the candidate pool — either via retrieval (query) or
            # by direct band scan (episode).
            candidates: list[tuple[MemoryEntry, float]] = []
            if query:
                embedding = self._embedder.encode_single(query)
                result = self._cms.retrieve(
                    embedding,
                    top_k=top_k,
                    sources=sources,
                    episodes=[episode] if episode else None,
                    tags=tags,
                    query_text=query,
                    # Wider net than the default — clustering wants more
                    # to work with.
                    min_score=0.0,
                )
                candidates = list(zip(result.entries, result.scores))
            elif episode:
                # Pull every entry tagged with this episode, ordered by
                # recency. Score is 1.0 across the board so the seed
                # decision falls back to insertion order — fine for a
                # one-episode scan.
                seen_texts: set[str] = set()
                for band in self._cms.bands:
                    for e in band.entries:
                        if e.episode_id != episode:
                            continue
                        if e.text in seen_texts:
                            continue
                        if sources and e.source not in sources:
                            continue
                        if tags and not (set(e.tags) & set(tags)):
                            continue
                        candidates.append((e, 1.0))
                        seen_texts.add(e.text)
                # Cap to ``top_k`` to keep clustering bounded.
                candidates = candidates[:top_k]
            else:
                # Neither query nor episode — there's nothing principled
                # to cluster, so return empty. Callers should pass at
                # least one anchor.
                return {
                    "query": None,
                    "episode": None,
                    "count": 0,
                    "clusters": [],
                }

            clusters: list[Cluster] = cluster_candidates(
                candidates,
                min_cohesion=min_cohesion,
                min_cluster_size=min_cluster_size,
                max_clusters=max_clusters,
            )
            return {
                "query": query,
                "episode": episode,
                "count": len(clusters),
                "clusters": [
                    {
                        "cohesion": round(c.cohesion, 4),
                        "seed_score": round(c.seed_score, 4),
                        "size": len(c.members),
                        "members": [_entry_to_dict(m) for m in c.members],
                    }
                    for c in clusters
                ],
            }

    def consolidate(
        self,
        replaces: list[str],
        new_text: str,
        source: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Atomic supersede-and-store: replace a cluster with one note.

        The cluster of stale entries (``replaces`` — list of exact texts
        or near-paraphrases) gets marked superseded by ``new_text``;
        the new note is stored as a fresh memory carrying ``source``
        (defaults to ``"consolidation"``) and ``tags``. Reuses the
        existing supersession machinery so deeper-band promotion +
        retrieval ordering already work correctly with consolidated
        entries.

        Defensive: empty ``replaces`` returns a no-op rather than just
        storing ``new_text`` — the caller should use ``memory_store``
        for that. Keeps the "consolidate" semantics unambiguous.

        Args:
            replaces: Exact or near-paraphrase texts to retire. Exact
                match first; embedding-fallback per text.
            new_text: The consolidated summary to store.
            source: Defaults to ``"consolidation"`` for audit clarity.
            tags: Optional tag list — useful for marking the new entry
                as ``["consolidated"]`` so it's discoverable.

        Returns:
            ``{"superseded_count": N, "superseded_texts": [...],
            "new_memory_stored": bool, "new_memory_surprise": float}``.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None and self._embedder is not None

            replaces = [t for t in (replaces or []) if (t or "").strip()]
            new_text = (new_text or "").strip()
            if not replaces or not new_text:
                return {
                    "superseded_count": 0,
                    "superseded_texts": [],
                    "new_memory_stored": False,
                    "error": "replaces and new_text must both be non-empty",
                }

            now = time.time()
            superseded: list[str] = []
            for old_text in replaces:
                marked_this_round = False
                # Exact-text pass for this specific replacement.
                for band in self._cms.bands:
                    for entry in band.entries:
                        if (
                            entry.text == old_text
                            and entry.superseded_at is None
                        ):
                            entry.superseded_at = now
                            entry.superseded_by_text = new_text
                            superseded.append(entry.text)
                            marked_this_round = True
                if marked_this_round:
                    continue
                # Embedding fallback for paraphrases.
                emb = self._embedder.encode_single(old_text)
                result = self._cms.retrieve(emb, top_k=1, query_text=old_text)
                if result.entries:
                    target = result.entries[0]
                    if target.superseded_at is None:
                        target.superseded_at = now
                        target.superseded_by_text = new_text
                        superseded.append(target.text)

            # Always store the consolidated entry — source defaults to
            # ``"consolidation"`` for audit / filtering.
            store_emb = self._embedder.encode_single(new_text)
            stored, surprise = self._cms.store(
                new_text,
                store_emb,
                source=source or "consolidation",
                tags=tags,
            )
            return {
                "superseded_count": len(superseded),
                "superseded_texts": superseded,
                "new_memory_stored": stored,
                "new_memory_surprise": round(float(surprise), 4),
            }

    def list_tags(self) -> dict[str, Any]:
        """Enumerate every tag in the bank, with occurrence counts.

        Useful before scoped searches — surface tags Claude has actually
        stored, instead of guessing. Sorted by count descending, ties
        broken alphabetically. ``total`` is the sum of occurrence counts
        (one entry with two tags counts as 2), not the unique tag count.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            counts: dict[str, int] = {}
            total = 0
            for band in self._cms.bands:
                for entry in band.entries:
                    for t in entry.tags:
                        counts[t] = counts.get(t, 0) + 1
                        total += 1
            rows = sorted(
                ({"tag": t, "count": c} for t, c in counts.items()),
                key=lambda r: (-r["count"], r["tag"]),
            )
            return {"tags": rows, "total": total}

    # ------------------------------------------------------------------
    # Phase 2 — knowledge graph (Postgres mode only)
    # ------------------------------------------------------------------

    _GRAPH_UNAVAILABLE = {
        "error": "graph_requires_postgres",
        "hint": "The graph lives in Postgres — set PSEUDOLIFE_MCP_DATABASE_URL "
                "(see ops/docker-compose.yml). File mode has no graph tables.",
    }

    def entity_ref(self, entity: str) -> dict[str, Any] | None:
        """Resolve an entity name (alias-aware) to its graph node, or None.
        Used to enrich fact answers with entity_id + alias info."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return None
            from pseudolife_memory.graph import norm_name
            return self._storage.find_entity(norm_name(entity))

    def _resolve_or_create_entity(self, name: str, etype: str | None = None) -> dict:
        """Alias-aware find; auto-create on miss. Caller holds the lock."""
        from pseudolife_memory.graph import norm_name
        st = self._storage
        n = norm_name(name)
        found = st.find_entity(n)
        if found is not None:
            if etype and not found.get("etype"):
                st.ensure_entity(found["canonical"], etype=etype)
                found["etype"] = etype
            return found
        eid = st.ensure_entity(n, display=name.strip(), etype=etype)
        self._age_mirror(lambda: self._age.upsert_entity(n, name.strip(), etype))
        return {"id": eid, "canonical": n, "display": name.strip(),
                "etype": etype, "aliases": []}

    def graph_relate(
        self,
        src: str,
        relation: str,
        dst: str,
        origin: str | None = None,
        confidence: float = 0.8,
        src_type: str | None = None,
        dst_type: str | None = None,
    ) -> dict[str, Any]:
        """Upsert a typed edge. Entities auto-create; the relation must be
        in the registry (closed vocabulary) — a miss returns suggestions,
        never stores under a drifted name. Soft type mismatches warn but
        store anyway (a hard reject would put a weak model into retry
        loops; a stored-with-warning edge keeps the bank growing)."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            registry = {r["name"]: r for r in st.load_relations()}
            resolved, suggestions = G.resolve_relation(list(registry), relation)
            if resolved is None:
                return {
                    "error": "unknown_relation",
                    "relation": relation,
                    "suggestions": suggestions,
                    "hint": "Define it with memory_relation_define, or use "
                            "'related-to' as the lawful fallback.",
                }
            src_e = self._resolve_or_create_entity(src, etype=src_type)
            dst_e = self._resolve_or_create_entity(dst, etype=dst_type)
            warnings: list[str] = []
            rmeta = registry[resolved]
            for side, ent, want in (
                ("src", src_e, rmeta.get("src_type")),
                ("dst", dst_e, rmeta.get("dst_type")),
            ):
                if want and ent.get("etype") and ent["etype"] != want:
                    warnings.append(
                        f"{side} '{ent['display']}' has type '{ent['etype']}' "
                        f"but relation '{resolved}' expects '{want}' — "
                        f"edge stored anyway",
                    )
            edge = st.upsert_edge(
                src_e["id"], resolved, dst_e["id"],
                confidence=confidence, origin=origin,
            )
            self._age_mirror(lambda: self._age.upsert_edge(
                src_e["canonical"], resolved, dst_e["canonical"]))
            return {
                "src": src_e["display"],
                "relation": resolved,
                "dst": dst_e["display"],
                "confidence": round(edge["confidence"], 4),
                "warnings": warnings,
            }

    def graph_unrelate(self, src: str, relation: str, dst: str) -> dict[str, Any]:
        """Mark an edge superseded (kept for audit, hidden from queries)."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            registry = [r["name"] for r in st.load_relations()]
            resolved, suggestions = G.resolve_relation(registry, relation)
            if resolved is None:
                return {"error": "unknown_relation", "relation": relation,
                        "suggestions": suggestions}
            src_e = st.find_entity(G.norm_name(src))
            dst_e = st.find_entity(G.norm_name(dst))
            if src_e is None or dst_e is None:
                missing = src if src_e is None else dst
                return {"removed": False, "reason": "unknown_entity",
                        "entity": missing}
            removed = st.supersede_edge(src_e["id"], resolved, dst_e["id"])
            if removed:
                self._age_mirror(lambda: self._age.remove_edge(
                    src_e["canonical"], resolved, dst_e["canonical"]))
            return {"removed": removed, "src": src_e["display"],
                    "relation": resolved, "dst": dst_e["display"]}

    def graph_alias(self, entity: str, alias: str) -> dict[str, Any]:
        """Bind ``alias`` → ``entity`` (auto-created). All fact and graph
        lookups resolve aliases first."""
        from pseudolife_memory.graph import norm_name
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            a = norm_name(alias)
            if not a:
                return {"error": "empty_alias"}
            ent = self._resolve_or_create_entity(entity)
            if a == ent["canonical"]:
                return {"error": "alias_is_canonical", "entity": ent["display"]}
            self._storage.add_alias(a, ent["id"])
            ent = self._storage.find_entity(ent["canonical"])
            return {"entity": ent["display"], "canonical": ent["canonical"],
                    "aliases": ent["aliases"]}

    def relation_define(
        self,
        name: str,
        description: str,
        transitive: bool = False,
        inverse_of: str | None = None,
        src_type: str | None = None,
        dst_type: str | None = None,
    ) -> dict[str, Any]:
        """Grow the closed relation vocabulary — a deliberate, strong-model
        act. Builtins cannot be redefined."""
        from pseudolife_memory.graph import norm_name
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            n = norm_name(name)
            if not n or not (description or "").strip():
                return {"error": "name_and_description_required"}
            registry = {r["name"]: r for r in st.load_relations()}
            if registry.get(n, {}).get("builtin"):
                return {"error": "builtin_relation",
                        "hint": f"'{n}' is a builtin and cannot be redefined."}
            inv = None
            if inverse_of:
                inv = norm_name(inverse_of)
                if inv not in registry and inv != n:
                    return {"error": "unknown_inverse", "inverse_of": inv,
                            "known": sorted(registry)}
            st.upsert_relation(
                n, description.strip(), src_type=src_type, dst_type=dst_type,
                transitive=bool(transitive), inverse_of=inv,
            )
            return {"defined": n, "transitive": bool(transitive),
                    "inverse_of": inv, "src_type": src_type,
                    "dst_type": dst_type}

    def graph_neighborhood(
        self,
        entity: str,
        depth: int = 1,
        include_facts: bool = True,
        to: str | None = None,
    ) -> dict[str, Any]:
        """Subgraph within ``depth`` hops (cap 3): nodes with their current
        facts, edges (derived ones marked with rule provenance), plus the
        shortest path when ``to`` names a second entity."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            root = st.find_entity(G.norm_name(entity))
            if root is None:
                return {"found": False, "entity": entity}
            to_id = None
            to_missing = None
            if to:
                to_e = st.find_entity(G.norm_name(to))
                if to_e is None:
                    to_missing = to
                else:
                    to_id = to_e["id"]
            g = st.load_graph()
            registry = {
                r["name"]: {"transitive": r["transitive"],
                            "inverse_of": r["inverse_of"]}
                for r in st.load_relations()
            }
            edges = [
                {"src": e["src_id"], "relation": e["relation"],
                 "dst": e["dst_id"], "confidence": e["confidence"],
                 "origin": e["origin"]}
                for e in g["edges"]
            ]
            sub = G.build_subgraph(edges, registry, root["id"],
                                   depth=depth, to=to_id)
            by_id = {e["id"]: e for e in g["entities"]}

            facts_by_norm: dict[str, list[dict]] = {}
            if include_facts and self._cortex is not None:
                for rec in self._cortex.current_records():
                    facts_by_norm.setdefault(
                        G.norm_name(rec.entity), [],
                    ).append({
                        "attribute": rec.attribute,
                        "value": rec.value,
                        "origin": rec.origin,
                        "confidence": round(float(rec.confidence), 4),
                    })

            nodes = []
            for nid in sorted(sub["nodes"]):
                e = by_id.get(nid)
                if e is None:
                    continue
                node = {
                    "entity": e["display"],
                    "canonical": e["canonical"],
                    "etype": e["etype"],
                    "aliases": g["aliases"].get(nid, []),
                }
                if include_facts:
                    node["facts"] = facts_by_norm.get(e["canonical"], [])
                nodes.append(node)

            def _disp(nid: int) -> str:
                return by_id[nid]["display"] if nid in by_id else str(nid)

            out_edges = []
            for e in sub["edges"]:
                row = {"src": _disp(e["src"]), "relation": e["relation"],
                       "dst": _disp(e["dst"]), "derived": e["derived"]}
                if e["derived"]:
                    row["via"] = e["via"]
                else:
                    row["confidence"] = round(float(e["confidence"]), 4)
                    if e.get("origin"):
                        row["origin"] = e["origin"]
                out_edges.append(row)

            result: dict[str, Any] = {
                "found": True,
                "entity": root["display"],
                "depth": max(1, min(int(depth), G.MAX_DEPTH)),
                "nodes": nodes,
                "edges": out_edges,
                "paths": [[_disp(n) for n in p] for p in sub["paths"]],
            }
            if to_missing is not None:
                result["to_not_found"] = to_missing
            return result

    def graph_cypher(self, cypher: str, limit: int = 50) -> dict[str, Any]:
        """Read-only openCypher via AGE — strong-model tool (spec §5.2)."""
        from pseudolife_memory.storage.age import is_mutating
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            if self._age is None:
                return {
                    "error": "age_unavailable",
                    "hint": "The AGE extension is not installed on this "
                            "Postgres. Use the ops/Dockerfile.pg compose "
                            "image (apache/age PG16 + pgvector).",
                }
            keyword = is_mutating(cypher)
            if keyword:
                return {"error": "mutating_clause_rejected", "keyword": keyword,
                        "hint": "memory_graph_query is read-only; mutate via "
                                "memory_graph_relate / memory_graph_unrelate."}
            try:
                rows = self._age.cypher(cypher, limit=limit)
            except Exception as exc:  # noqa: BLE001
                self._storage.conn.rollback()
                return {"error": "cypher_failed", "detail": str(exc)}
            return {"count": len(rows), "rows": rows}

    def age_sync(self) -> dict[str, Any]:
        """Full AGE re-sync from the graph tables (heals a drifted mirror)."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            if self._age is None:
                return {"error": "age_unavailable"}
            return self._age.resync(self._storage)
