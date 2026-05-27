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
import time
from pathlib import Path
from threading import Lock
from typing import Any

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.context_builder import ContextBuilder
from pseudolife_memory.memory.contrastive import ContrastiveUpdater
from pseudolife_memory.memory.embedding import EmbeddingPipeline
from pseudolife_memory.memory.reference_bank import ReferenceBank
from pseudolife_memory.memory.reranker import CrossEncoderReranker
from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
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
    ) -> None:
        self._lock = Lock()
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
        self._last_user_query: str | None = None

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

        Leaves the MIRAS preset alone — the ``continuum`` 8-tier default
        is fine for Claude's use too.
        """
        config.memory.surprise_threshold = 0.2
        config.embedding.batch_size = 16

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
        self._cms = ContinuumMemorySystem(
            self.config.memory,
            reference_bank=self._reference,
            reranker=self._reranker,
        )
        # Restore persisted state if any (silently no-ops on fresh install).
        try:
            self._cms.load(self.config.memory.save_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CMS load skipped: %s", exc)

        self._contrastive = ContrastiveUpdater(
            self.config.memory.contrastive, self._embedder,
        )
        self._context_builder = ContextBuilder(self.config.context)

    # ------------------------------------------------------------------
    # Tool: store
    # ------------------------------------------------------------------

    def store(self, text: str, source: str = "claude") -> dict[str, Any]:
        """Embed and store a memory through the CMS pipeline.

        Returns ``{"stored": bool, "surprise": float, "reason": str|None}``.
        Stores can be rejected by either the meta-filter (looks like
        self-reference) or the surprise gate (already known) — the
        ``reason`` field surfaces which.
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cms is not None
            text = (text or "").strip()
            if not text:
                return {"stored": False, "surprise": 0.0, "reason": "empty"}
            embedding = self._embedder.encode_single(text)
            stored, surprise = self._cms.store(text, embedding, source=source)
            reason: str | None = None
            if not stored:
                # Mirror the gates in CMS.store so callers know why.
                if surprise == 0.0:
                    reason = "filtered_meta"
                elif surprise < self.config.memory.surprise_threshold:
                    reason = "below_surprise_threshold"
                else:
                    reason = "rejected"
            return {
                "stored": stored,
                "surprise": round(float(surprise), 4),
                "reason": reason,
            }

    # ------------------------------------------------------------------
    # Tool: search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int | None = None,
        sources: list[str] | None = None,
        bands: list[str] | None = None,
        min_score: float | None = None,
        disable_recency_boost: bool = False,
        rerank: bool | None = None,
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
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cms is not None
            query = (query or "").strip()
            if not query:
                return {"entries": [], "query": "", "count": 0}
            embedding = self._embedder.encode_single(query)
            result = self._cms.retrieve(
                embedding,
                top_k=top_k,
                bands=bands,
                sources=sources,
                query_text=query,
                min_score=min_score,
                disable_recency_boost=disable_recency_boost,
                rerank=rerank,
            )
            # Stash the query so memory_supersede / contrastive flows have
            # something to anchor against on the next call.
            self._last_user_query = query
            return {
                "query": query,
                "count": len(result.entries),
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
        rerank: bool | None = None,
    ) -> dict[str, Any]:
        """Like :meth:`search` but also returns the structured ranking trace.

        Use to diagnose retrieval misses: each tier's candidates show
        raw_score, recency, source/supersession multipliers, and the
        drop_reason (or ``kept=True``) — so callers can see *why* a
        fact didn't surface.

        ``rerank`` plumbs the cross-encoder override through to
        ``cms.retrieve_with_trace`` so the trace ``reranker`` field
        records both whether it fired and per-candidate ce/fused scores.
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
                query_text=query,
                rerank=rerank,
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
        self, n: int = 10, sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """List the N most recently stored memories across all bands.

        Useful for debugging ("what did I just store?"). Unlike ``search``,
        this returns by ``timestamp`` not relevance.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            source_filter = set(sources) if sources else None
            all_entries: list[MemoryEntry] = []
            for band in self._cms.bands:
                for entry in band.entries:
                    if source_filter and entry.source not in source_filter:
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

            # Exact-text pass.
            for band in self._cms.bands:
                for entry in band.entries:
                    if entry.text == old_text and entry.superseded_at is None:
                        entry.superseded_at = now
                        entry.superseded_by_text = new_text
                        superseded.append(entry.text)

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
        """Persist CMS tensors to disk. ChromaDB persists itself."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            self._cms.save(self.config.memory.save_dir)
            return {"saved_to": self.config.memory.save_dir}
