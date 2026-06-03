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

    def store(
        self,
        text: str,
        source: str = "claude",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Embed and store a memory through the CMS pipeline.

        ``tags`` (schema v6) is an optional multi-valued label list.
        Normalised by the underlying CMS (lowercased / stripped /
        deduped). Tags exist alongside ``source``, not as a replacement.

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
                return {"entries": [], "query": "", "count": 0}
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
        """Persist CMS tensors to disk. ChromaDB persists itself."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            self._cms.save(self.config.memory.save_dir)
            self._last_saved_fingerprint = self._entry_fingerprint()
            return {"saved_to": self.config.memory.save_dir}

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
        return (total, superseded)

    def autosave_if_changed(self):
        """Flush CMS tensors only if mutating state changed since last save.
        Driven by the background autosave loop in mcp_server."""
        with self._lock:
            if self._cms is None:
                return None
            fp = self._entry_fingerprint()
            if fp == self._last_saved_fingerprint:
                return None
            self._cms.save(self.config.memory.save_dir)
            self._last_saved_fingerprint = fp
            return {"saved_to": self.config.memory.save_dir, "auto": True}

    def flush(self):
        """Unconditional save for clean-exit / signal handlers. Captures the
        latest state (incl. band migrations). No-op if never initialised."""
        with self._lock:
            if self._cms is None:
                return None
            self._cms.save(self.config.memory.save_dir)
            self._last_saved_fingerprint = self._entry_fingerprint()
            return {"saved_to": self.config.memory.save_dir, "flush": True}

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
            return self._episode_to_dict(ep)

    def episode_end(self) -> dict[str, Any]:
        """Close the currently-open episode. Empty dict if none open."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            closed = self._cms.episodes.end()
            return self._episode_to_dict(closed) if closed is not None else {}

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
