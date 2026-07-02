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

import heapq
import logging
import os
import re
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
from pseudolife_memory.memory.context_builder import ContextBuilder, _relative_time
from pseudolife_memory.memory.contrastive import ContrastiveUpdater
from pseudolife_memory.memory.embedding import EmbeddingPipeline
from pseudolife_memory.memory.reference_bank import ReferenceBank
from pseudolife_memory.memory.reranker import CrossEncoderReranker
from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.writer_context import resolve_writer
from pseudolife_memory.utils.config import (
    AppConfig,
    ContextConfig,
    EmbeddingConfig,
    MemoryConfig,
    ReferenceConfig,
    load_config,
)

logger = logging.getLogger(__name__)


class PersistenceError(RuntimeError):
    """A durable save (cortex / world / lessons snapshot) failed — the in-memory
    write succeeded but did NOT reach Postgres/disk. Surfaced to the caller and
    counted in ``MemoryService._persist_errors`` (health-visible), never silently
    swallowed: silent save loss is the one failure a memory system must not hide."""


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


def _cortex_record_to_dict(rec, relative_age: bool = True) -> dict[str, Any]:
    """Serialise a :class:`CortexRecord` for transport (JSON-safe).

    Surfaces the v0.4 temporal/provenance stamp (tx_time, valid_time, writer_id,
    session_id) and — when ``relative_age`` is on — a human ``age`` string so the
    agent reads a sense of time without parsing epoch seconds."""
    d = {
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
        # v0.4 writer-aware temporal stamp.
        "tx_time": rec.tx_time,
        "valid_time": rec.valid_time,
        "writer_id": rec.writer_id,
        "session_id": rec.session_id,
    }
    if relative_age:
        d["age"] = _relative_time(rec.tx_time or rec.asserted_at)
    return d


# A URL scheme per RFC 3986: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) ":".
_URL_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]*:")


def _is_safe_source_url(url: str) -> bool:
    """True iff a world-fact citation URL is safe to PERSIST. A world citation is
    agent/LLM-authored (often distilled from fetched web content), so a
    prompt-injected ``javascript:`` / ``data:`` / ``vbscript:`` scheme must never
    land in the bank — not merely be neutralised at one render site.

    Safe = empty (no citation), an ``http(s)`` URL, or a scheme-LESS string (a
    bare path/host is inert; the console renders it as plain text). Rejected = a
    non-empty string carrying any scheme other than http(s). Leading
    whitespace/control chars are stripped first, the way a browser would, so
    ``"\\tjavascript:..."`` can't slip past the scheme check.
    """
    if not url:
        return True
    cleaned = re.sub(r"[\x00-\x20]", "", url).lower()  # strip ctrl/space like a browser
    if cleaned.startswith(("http://", "https://")):
        return True
    return _URL_SCHEME.match(cleaned) is None  # no scheme at all → inert, allow


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


def _lesson_record_to_dict(rec) -> dict[str, Any]:
    """Serialise a LessonRecord for transport. Uses procedural field names
    (task / aspect / lesson) rather than the slot's entity/attribute/value."""
    return {
        "task": rec.entity,
        "aspect": rec.attribute,
        "lesson": rec.value,
        "about": rec.about,
        "polarity": rec.polarity,
        "outcome": rec.outcome,
        "status": rec.status,
        "confidence": round(float(rec.confidence), 4),
        "origin": rec.origin,
        "provenance": sorted(rec.provenance),
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


def _k_core_peel(entities: list[str], edges: list[dict], max_nodes: int) -> set[str]:
    """Shrink ``entities`` to ``max_nodes`` by repeatedly removing the
    globally lowest-degree node (decrementing its neighbours as it goes) —
    i.e. a k-core peel, not a single top-degree sort.

    A single sort-by-raw-degree cap can keep a node whose entire neighbourhood
    consists of low-degree leaves that themselves don't survive the cap: the
    node individually ranks high, but ends up with zero edges once the kept
    set is filtered. On a real bank (1091 entities, capped to 300) this
    stranded ~1/6 of the kept nodes with no edges at all, and those
    force-sim-scattered orphans dragged the canvas's auto-fit camera off the
    dense cluster — reproducing the very 'off to the side' bug the cap was
    meant to help fix. Peeling by current (not original) degree means a node
    is only kept if it's still meaningfully connected to the *rest of the
    kept set* at the moment it would otherwise be cut."""
    if len(entities) <= max_nodes:
        return set(entities)
    adj: dict[str, set[str]] = {e: set() for e in entities}
    for e in edges:
        if e["src"] in adj and e["dst"] in adj and e["src"] != e["dst"]:
            adj[e["src"]].add(e["dst"])
            adj[e["dst"]].add(e["src"])
    deg = {e: len(adj[e]) for e in entities}
    alive = set(entities)
    heap = [(d, e) for e, d in deg.items()]
    heapq.heapify(heap)
    while len(alive) > max_nodes:
        d, name = heapq.heappop(heap)
        if name not in alive or d != deg[name]:
            continue   # stale heap entry — degree changed since this was pushed
        alive.discard(name)
        for nb in adj[name]:
            if nb in alive:
                deg[nb] -= 1
                heapq.heappush(heap, (deg[nb], nb))
    return alive


def _user_yaml_leaves(path: str | Path) -> frozenset[str]:
    """Dotted leaf keys explicitly set in the user's config.yaml.

    Empty set when the file is missing or unreadable. Feeds
    :meth:`MemoryService._apply_mcp_defaults` so the MCP-tuned defaults
    only fill keys the user left unset.
    """
    try:
        import yaml
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt file = no user keys
        return frozenset()
    leaves: set[str] = set()

    def _walk(node: object, prefix: str) -> None:
        if isinstance(node, dict) and node:
            for k, v in node.items():
                _walk(v, f"{prefix}{k}.")
        elif prefix:
            leaves.add(prefix[:-1])

    _walk(raw, "")
    return frozenset(leaves)


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
        self._graph = None  # GraphStore
        # Resolve data directory first — that's where memory_state lives
        # AND where the default config sits (if config_path not given).
        self.data_dir = Path(data_dir) if data_dir else Path.cwd() / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        if config_path is None:
            # Sentinel for "use defaults" — load_config returns an
            # AppConfig() when the file doesn't exist.
            cfg_candidate = self.data_dir / "config.yaml"
        else:
            cfg_candidate = Path(config_path)
        self.config = load_config(cfg_candidate)

        # Override save_dir so memory tensors land inside data_dir even
        # when the config wasn't tailored for this install.
        self.config.memory.save_dir = str(self.data_dir / "memory_state")
        self.config.memory.reference.persist_dir = str(self.data_dir / "chromadb")

        # Defaults that make sense for the *Claude* use-case differ from
        # the human-chat defaults shipped with PseudoLife — see README.
        # Overlay only: keys the user explicitly set in config.yaml win.
        self._apply_mcp_defaults(self.config, user_keys=_user_yaml_leaves(cfg_candidate))

        # Lazy components — built on first use.
        self._embedder: EmbeddingPipeline | None = None
        self._cms: ContinuumMemorySystem | None = None
        self._reference: ReferenceBank | None = None
        self._contrastive: ContrastiveUpdater | None = None
        self._context_builder: ContextBuilder | None = None
        self._reranker: CrossEncoderReranker | None = None
        self._cortex: CortexStore | None = None
        self._world = None  # WorldCortexStore | None (world-knowledge cortex, v9)
        self._lessons = None  # LessonStore | None (procedural / outcome memory, v10)
        from pseudolife_memory.memory.hlc import HybridLogicalClock
        self._hlc = HybridLogicalClock()  # write ordering authority (memory/hlc.py)
        # Default writer identity; the daemon overrides per-connection (v0.4 T4).
        self._writer_id = os.environ.get("PSEUDOLIFE_WRITER_ID") or "unknown"
        self._last_user_query: str | None = None
        self._last_saved_fingerprint = None
        # Poison-pill guard for the dream: consecutive extraction-failure
        # counts per entry (db_id). In-memory only — a daemon restart resets
        # the strikes, which just means a poison entry needs its three
        # failures again before quarantine.
        self._dream_entry_failures: dict[Any, int] = {}
        # Count of durable-save failures (cortex/world/lessons). Exposed via the
        # daemon /health probe so swallowed-then-surfaced saves are observable.
        self._persist_errors = 0

    def _resolve_writer(self) -> tuple[str, str | None]:
        """The ``(writer_id, session_id)`` to attribute the current write to —
        per-request when inside the daemon (``X-PL-Writer`` header), else the
        process default. See :mod:`pseudolife_memory.writer_context`."""
        return resolve_writer(self._writer_id)

    def _assert_public_search_path(self) -> None:
        """Fail loud if the shared connection would resolve unqualified tables to
        the role-named ``pseudolife`` shadow schema instead of the real ``public``
        bank. ``$user`` expands to the DB role ``pseudolife``, which is also a
        schema name — if it lands ahead of ``public`` in search_path it silently
        shadows the real bank, so we refuse to run in that configuration."""
        if self._storage is None:
            return
        path = self._storage.conn.execute("SHOW search_path").fetchone()[0]
        schemas = [s.strip().strip('"') for s in path.split(",")]
        if "public" not in schemas:
            raise RuntimeError(
                f"search_path must include 'public' (got {path!r}); the real bank "
                "lives in public — refusing to run against a shadow schema.")
        if "$user" in schemas and schemas.index("$user") < schemas.index("public"):
            raise RuntimeError(
                f"search_path resolves $user (role 'pseudolife', which is also a "
                f"schema name) ahead of public (got {path!r}) — this shadows the "
                "real bank. Pin search_path to public first.")

    # ------------------------------------------------------------------
    # Lazy construction
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_mcp_defaults(
        config: AppConfig, user_keys: frozenset[str] = frozenset(),
    ) -> None:
        """Tweak PseudoLife defaults for the MCP / Claude use case.

        ``user_keys`` is the set of dotted leaf keys the user explicitly
        set in config.yaml — those are respected, never clobbered (the
        pre-2026-07-02 behavior overwrote them unconditionally, which made
        the corresponding YAML knobs dead in the daemon).

        Differences from the user-facing chat defaults:

        * ``surprise_threshold`` 0.0: the v0.5 gate measures *novelty*
          (``1 − max cos`` to existing entries); Claude stores deliberately,
          so the gate stays permissive (store everything; novelty still drives
          eviction/promotion scoring). Raise it to enable dedup of
          near-duplicate stores.
        * Smaller embedder batch size: MCP calls one-at-a-time, no point
          paying the warmup overhead of a large batch.
        * Meta-filter OFF: it exists to drop auto-captured chat noise;
          every MCP store is a deliberate tool call.
        * Recency base half-life 24h (vs 1h): Claude Code sessions are
          hours-to-days apart, so a 1h half-life made the recency boost
          effectively always zero.
        * retention_boost 1.0: graded MTT retention on for the daemon (library default 0.0).

        Leaves the MIRAS preset alone — the ``continuum`` 8-tier default
        is fine for Claude's use too.
        """
        def absent(key: str) -> bool:
            return key not in user_keys

        if absent("memory.surprise_threshold"):
            config.memory.surprise_threshold = 0.0
        if absent("embedding.batch_size"):
            config.embedding.batch_size = 16
        if absent("memory.meta_filter.enabled"):
            config.memory.meta_filter.enabled = False
        if absent("memory.recency_base_half_life_s"):
            config.memory.recency_base_half_life_s = 86400.0
        # Graded MTT retention ON for the daemon (provenance-as-link Phase 2): a
        # reinforced episode resists eviction by retention_boost*log1p(reinforcements).
        # 1.0 is the largest boost with ~no recency displacement — the honest
        # retention_bench knee (P1.6, evals/retention_bench.py). Most reinforced-entry
        # protection already comes from access-coupling (reinforcing bumps access_count);
        # 1.0 is a modest free nudge on top, higher values trade recency for more. The
        # library default stays 0.0 (no-op) — this is a deployment-build choice.
        if absent("memory.traces.retention_boost"):
            config.memory.traces.retention_boost = 1.0

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
            # Invariant: unqualified tables MUST resolve to the real `public`
            # bank, never the role-named `pseudolife` shadow schema (v0.4
            # collision fix). PostgresStorage pins this; fail loud if regressed.
            self._assert_public_search_path()
            from pseudolife_memory.memory.graph_store import PostgresNetworkxGraphStore
            self._graph = PostgresNetworkxGraphStore(self._storage)
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

        # Procedural / outcome memory (schema v10) — sibling slot store for the
        # lessons the agent learns from its own work (what worked / dead-ended /
        # got corrected). Postgres-only (a v0.2+ feature; no .pt fallback).
        from pseudolife_memory.memory.lessons import LessonStore
        self._lessons = LessonStore()
        if self._storage is not None:
            from pseudolife_memory.storage import sync as _sync
            try:
                _sync.hydrate_lessons(self._lessons, self._storage)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Lesson store hydration skipped: %s", exc)

        # Re-seed the HLC from the stored high-water stamp (2026-07-02 P1): a
        # wall-clock step-back across restarts (NTP, laptop resume) must not
        # let stored stamps outrank every new write — pre-fix, a user
        # correction landing "before" history got parked as a contender until
        # real time caught up.
        best = (0, 0)
        for recs in ((self._cortex.records if self._cortex else ()),
                     (self._world.records if self._world else ()),
                     (self._lessons.records if self._lessons else ())):
            for r in recs:
                if r.hlc_phys:
                    cand = (int(r.hlc_phys), int(r.hlc_logical or 0))
                    if cand > best:
                        best = cand
        if best > (0, 0):
            self._hlc.observe(*best)

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
            _, session_id = self._resolve_writer()
            self._ensure_session_episode(session_id)
            stored, surprise = self._cms.store(
                text, embedding, source=source, tags=tags,
                session_key=session_id,
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
        # Stamp like cortex_write does (2026-07-02 P1): unstamped auto-promotes
        # carried (0,0) HLC — they could never supersede a stamped row, and the
        # v11 backfill retro-labeled them writer_id='legacy' on every boot.
        writer_id, session_id = self._resolve_writer()
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
                    hlc=self._hlc.tick(),
                    writer_id=writer_id,
                    session_id=session_id,
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
        from pseudolife_memory.memory.graph_consolidation import junk_name_reason
        if junk_name_reason(entity):
            return  # junk-shaped subject: keep the fact, skip the graph node
        n = norm_name(entity)
        if n and self._storage.find_entity(n) is None:
            self._storage.ensure_entity(n, display=entity.strip())

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
            episodes = self._episode_subtree(episodes)
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
                session_key=self._resolve_writer()[1],
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
            result = self._cms.stats()
            if self._storage is not None:
                _c = self._storage.load_communities()["communities"]
                result["communities"] = len(_c)
                result["graph_digest_at"] = (self._storage.get_meta("graph_digest") or {}).get("computed_at")
            return result

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
                # Per-slot write-through (2026-07-02 P1): persists only the
                # slots mutated since the last sync. The full snapshot runs
                # on explicit save / flush (see _persist_all).
                _sync.sync_cortex_slots(self._cortex, self._storage)
            else:
                self._cortex.save(self._cortex_path())
        except Exception as exc:
            self._persist_errors += 1
            logger.error("Cortex save failed (NOT durably persisted): %s", exc)
            raise PersistenceError(f"cortex save failed: {exc}") from exc

    def _save_world(self) -> None:
        if getattr(self, "_world", None) is None or self._storage is None:
            return
        try:
            from pseudolife_memory.storage import sync as _sync
            _sync.sync_world_slots(self._world, self._storage)
        except Exception as exc:
            self._persist_errors += 1
            logger.error("World cortex save failed (NOT durably persisted): %s", exc)
            raise PersistenceError(f"world cortex save failed: {exc}") from exc

    def _save_lessons(self) -> None:
        if getattr(self, "_lessons", None) is None or self._storage is None:
            return
        try:
            from pseudolife_memory.storage import sync as _sync
            _sync.sync_lesson_slots(self._lessons, self._storage)
        except Exception as exc:
            self._persist_errors += 1
            logger.error("Lesson store save failed (NOT durably persisted): %s", exc)
            raise PersistenceError(f"lesson store save failed: {exc}") from exc

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
            if kind in ("explicit", "flush"):
                # Full resync on the rare explicit/exit saves — belt and
                # braces against any dirty-mark gap in the per-slot path.
                from pseudolife_memory.storage import sync as _sync
                _sync.snapshot_cortex(self._cortex, self._storage)
                if self._world is not None:
                    _sync.snapshot_world_cortex(self._world, self._storage)
                if self._lessons is not None:
                    _sync.snapshot_lessons(self._lessons, self._storage)
            else:
                self._save_cortex()
                self._save_world()
                self._save_lessons()
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
            writer_id, session_id = self._resolve_writer()
            res = self._cortex.write_fact(
                Slot(entity, attribute, value),
                emb,
                slot_embedding=slot_emb,
                confidence=confidence,
                provenance=provenance or (),
                support=support,
                now=now,
                hlc=self._hlc.tick(),
                writer_id=writer_id,
                session_id=session_id,
            )
            self._ensure_subject_entity(entity)
            self._save_cortex()
            # Auto-tag a user correction: a user-tier write that REPLACED an older
            # value is a genuine "Y -> Z" correction signal for procedural memory.
            # Gated to support=="user" so dream/agent consolidation (support=agent)
            # never feeds itself a correction (no synthesis feedback loop).
            if (res.action == "superseded"
                    and (support or "").strip().lower() == "user"):
                self._emit_correction_signal(
                    entity, attribute, res.record.supersedes_value, value)
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
            if rec is None:
                return None
            d = _cortex_record_to_dict(rec, relative_age=self.config.time.relative_age)
            if self._storage is not None:
                from pseudolife_memory.memory.cortex import _norm_key
                d["source_entries"] = self._storage.traces_for_slot(
                    _norm_key(rec.entity), _norm_key(rec.attribute))
            return d

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
                if self._storage is not None:
                    from pseudolife_memory.memory.cortex import _norm_key
                    d["source_entries"] = self._storage.traces_for_slot(
                        _norm_key(r.entity), _norm_key(r.attribute))
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
        if not _is_safe_source_url(source_url):
            # Refuse a citation carrying a non-http(s) scheme (javascript:, data:,
            # …) at the write boundary so a prompt-injected payload never lands —
            # data-at-rest safety, complementing the console's render-time allowlist.
            return {"action": "rejected", "reason": "unsafe_source_url",
                    "source_url": source_url}
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._world is not None
            emb = self._embedder.encode_single(f"{entity} {attribute} {value}".strip())
            writer_id, session_id = self._resolve_writer()
            action, rec = self._world.write_fact(
                entity, attribute, value, emb,
                confidence=confidence, source_url=source_url, source_quote=source_quote,
                freshness_class=freshness_class, retrieved_at=retrieved_at,
                content_hash=content_hash, source_doc_id=source_doc_id, now=now,
                hlc=self._hlc.tick(), writer_id=writer_id, session_id=session_id)
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

    # ------------------------------------------------------------------
    # Procedural / outcome memory — lessons (schema v10)
    # ------------------------------------------------------------------

    def _current_episode_id(self) -> str | None:
        try:
            return self._cms.episodes.current_id if self._cms is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _emit_correction_signal(self, entity, attribute, old, new) -> None:
        """Record a correction signal for a user-driven supersession. Caller holds
        the lock. Best-effort: never let signal capture break a cortex write."""
        if self._storage is None or not self.config.memory.lessons.enabled:
            return
        try:
            self._storage.add_signal(
                task=entity, outcome="correction", about=entity,
                detail=f"{attribute}: {old} → {new}", polarity=None,
                origin="action", episode_id=self._current_episode_id())
        except Exception as exc:  # noqa: BLE001
            logger.warning("correction signal emit failed: %s", exc)

    def record_outcome(self, task: str, outcome: str, about: str | None = None,
                       detail: str | None = None, polarity: str | None = None,
                       origin: str = "action") -> dict[str, Any]:
        """Record a cheap in-session outcome signal (success | failure |
        correction). Single-writer: this never writes a lesson — the dream
        synthesises lessons from the accumulated signals."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"recorded": False, "reason": "signals require Postgres storage"}
            if not self.config.memory.lessons.enabled:
                return {"recorded": False, "reason": "lessons disabled"}
            oc = outcome if outcome in ("success", "failure", "correction") else "success"
            sid = self._storage.add_signal(
                task=task, outcome=oc, about=about, detail=detail,
                polarity=polarity, origin=origin,
                episode_id=self._current_episode_id())
            return {"recorded": True, "signal_id": sid, "task": task, "outcome": oc}

    def lesson_write(self, task: str, aspect: str, lesson: str, *,
                     about: str | None = None, outcome: str = "success",
                     polarity: str = "+", confidence: float = 0.6,
                     origin: str = "agent",
                     provenance: set[str] | list[str] | None = None,
                     now: float | None = None,
                     valid_time: float | None = None) -> dict[str, Any]:
        """Write / confirm / supersede a lesson at the ``(task, aspect)`` slot and
        keep the graph joined: upsert the task-type entity, the ``about`` object,
        and the ``prefers`` (positive) / ``avoids`` (negative) edge between them.

        This is the dream's writer (single author); it is not an agent-facing tool.
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._lessons is not None
            emb = self._embedder.encode_single(f"{task} {aspect} {lesson}".strip())
            writer_id, session_id = self._resolve_writer()
            action, rec = self._lessons.write_fact(
                task, aspect, lesson, emb, about=about, outcome=outcome,
                polarity=polarity, confidence=confidence, origin=origin,
                provenance=provenance, now=now, valid_time=valid_time,
                hlc=self._hlc.tick(), writer_id=writer_id, session_id=session_id)
            self._link_lesson_graph(task, rec.about, rec.polarity)
            self._save_lessons()
            return {"action": action, **_lesson_record_to_dict(rec)}

    def _link_lesson_graph(self, task: str, about: str | None, polarity: str) -> None:
        """Upsert the task-type entity + object entity + prefers/avoids edge so a
        lesson is traversable via memory_graph. Caller holds the lock; no-op in
        file mode."""
        if self._storage is None:
            return
        from pseudolife_memory.graph import norm_name
        st = self._storage
        tn = norm_name(task)
        if not tn:
            return
        tid = st.ensure_entity(tn, display=task.strip(), etype="task-type")
        if not about:
            return
        an = norm_name(about)
        if not an or an == tn:
            return
        oid = st.ensure_entity(an, display=about.strip())
        relation = "avoids" if polarity == "-" else "prefers"
        self._graph.upsert_edge(tid, relation, oid, confidence=0.7, origin="action")

    def _link_dream_relations(self, relations: list[dict]) -> int:
        """Upsert dream-extracted (src,relation,dst) edges. Closed-vocab
        (resolve_relation; unknown -> related-to), entities resolved alias-aware
        and pinned to the Postgres hub, self-loops dropped, origin='agent'.
        Caller holds the lock; no-op in file mode. Returns edges written."""
        if self._storage is None or not relations:
            return 0
        from pseudolife_memory import graph as G
        from pseudolife_memory.memory.relation_quality import edge_confidence
        known = [r["name"] for r in self._graph.load_relations()
                 if r["name"] not in ("prefers", "avoids")]
        floor = float(self.config.memory.dream.min_relation_confidence)
        n = 0
        from pseudolife_memory.memory.graph_consolidation import junk_name_reason
        for r in relations:
            raw_src, raw_dst = str(r.get("src", "")), str(r.get("dst", ""))
            src_n, dst_n = G.norm_name(raw_src), G.norm_name(raw_dst)
            if not src_n or not dst_n or src_n == dst_n:
                continue
            # Write-time junk gate: the 2B extractor's known artifact classes
            # (concat names, bare numbers, status words) never become entities.
            junk = junk_name_reason(raw_src) or junk_name_reason(raw_dst)
            if junk:
                logger.debug("dream relation dropped (%s): %r -> %r",
                             junk, raw_src, raw_dst)
                continue
            resolved, _ = G.resolve_relation(known, str(r.get("relation", "")))
            relation = resolved or "related-to"
            conf = edge_confidence(raw_src, relation, raw_dst)
            if conf < floor:
                continue
            src_e = self._resolve_or_create_entity(raw_src)
            dst_e = self._resolve_or_create_entity(raw_dst)
            # revive=False: a dream re-assertion must not resurrect an edge
            # a human (or deep-dream) superseded — removals stay sticky.
            self._graph.upsert_edge(src_e["id"], relation, dst_e["id"],
                                    confidence=conf, origin="agent",
                                    revive=False)
            n += 1
        return n

    def _dream_extract_relations(self, extractor, texts: list[str]) -> int:
        """Gated, best-effort graph-from-text for one dream batch: run the LLM
        relations call UNLOCKED (slow network), then write edges LOCKED. A
        failure logs and returns 0 — it must never break fact consolidation or
        drop claims (relations are best-effort, like lessons)."""
        cfg = self.config.memory.dream
        rel_fn = getattr(extractor, "extract_relations", None)
        if not (cfg.extract_relations and rel_fn is not None and texts):
            return 0
        try:
            with self._lock:
                self._ensure_init()
                if self._storage is None:
                    return 0
                registry = [(r["name"], r["description"])
                            for r in self._graph.load_relations()
                            if r["name"] not in ("prefers", "avoids")]
            rels = rel_fn(texts, registry)
            with self._lock:
                return self._link_dream_relations(rels)
        except Exception as exc:  # noqa: BLE001 — best-effort; never break the dream
            logger.warning("dream relation extraction failed (%s); claims kept",
                           exc)
            return 0

    def lesson_search(self, query: str, top_k: int | None = None,
                      min_score: float = 0.0) -> dict[str, Any]:
        """Embedding-on-query retrieval over current lessons (mirrors world_search).
        Returns lessons with polarity/outcome so a caller can surface dead-ends."""
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._lessons is not None
            k = int(top_k if top_k is not None else self.config.memory.lessons.top_k)
            floor = max(float(min_score), float(self.config.memory.lessons.min_confidence))
            emb = self._embedder.encode_single(query)
            hits = self._lessons.search(emb, top_k=k, min_score=floor)
            entries = [{**_lesson_record_to_dict(r), "score": round(float(s), 4)}
                       for r, s in hits]
            return {"count": len(entries), "entries": entries}

    def lessons_dump(self, limit: int = 120) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            assert self._lessons is not None
            rows = [_lesson_record_to_dict(r) for r in self._lessons.current_records()]
            rows.sort(key=lambda d: (d["task"].lower(), d["aspect"].lower()))
            return {"count": len(rows), "entries": rows[: max(0, int(limit))]}

    def lesson_forget(self, task: str, aspect: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            assert self._lessons is not None
            removed = self._lessons.forget(task, aspect)
            if removed:
                self._save_lessons()
            return {"removed": removed, "task": task, "aspect": aspect}

    def synthesize_lessons(self, extractor, *, limit: int | None = None) -> dict[str, Any]:
        """Drain pending outcome signals and synthesise lessons via ``extractor``.

        Single-writer: an extractor with no ``extract_lessons`` (the no-op / a
        plain regex floor) writes nothing and leaves the signals pending. Old
        signals are pruned by retention so the log can't grow unbounded.
        """
        import time as _t
        cfg = self.config.memory.lessons
        if self._storage is None:
            return {"signals": 0, "lessons": 0, "skipped": "no-storage"}
        if not (cfg.enabled and cfg.synthesize_in_dream):
            return {"signals": 0, "lessons": 0, "skipped": "disabled"}
        cutoff = _t.time() - cfg.signal_retention_days * 86400
        with self._lock:
            self._ensure_init()
            self._storage.prune_signals(cutoff)
            signals = self._storage.pending_signals(limit=limit)
        if not signals:
            return {"signals": 0, "lessons": 0}
        fn = getattr(extractor, "extract_lessons", None)
        if fn is None:
            return {"signals": len(signals), "lessons": 0, "skipped": "no-extractor"}
        try:
            claims = fn(signals)
        except Exception as exc:  # noqa: BLE001 — never let synthesis break the dream
            logger.warning("lesson synthesis failed (%s); leaving signals pending", exc)
            return {"signals": len(signals), "lessons": 0, "error": str(exc)}
        # Bitemporal event time: the synthesised lesson became *true* when its
        # underlying outcomes were observed, not when the dream wrote it. Claims
        # don't map 1:1 to signals, so use the earliest contributing signal's
        # created_at as the batch valid_time (None → store defaults to tx_time).
        created = [s["created_at"] for s in signals if s.get("created_at")]
        batch_valid_time = min(created) if created else None
        written = 0
        for c in claims:
            try:
                self.lesson_write(
                    c["task"], c.get("aspect", "lesson"), c["lesson"],
                    about=c.get("about"), outcome=c.get("outcome", "success"),
                    polarity=c.get("polarity", "+"),
                    confidence=float(c.get("confidence", 0.6)),
                    origin=c.get("origin", "agent"),
                    provenance=set(c.get("provenance") or []),
                    valid_time=batch_valid_time)
                written += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("lesson write skipped (%s): %s", exc, c)
        if written:
            with self._lock:
                self._storage.consume_signals([s["id"] for s in signals])
        else:
            # Nothing landed (empty extraction or every write failed): leave
            # the signals pending so the next sweep retries — they are the
            # only feeder for procedural memory. Retention pruning bounds
            # the retry window.
            logger.info("lesson synthesis wrote nothing; leaving %d signals "
                        "pending", len(signals))
        return {"signals": len(signals), "lessons": written}

    def cortex_dump(self) -> dict[str, Any]:
        """All current canonical facts (entity, attribute, value, origin, …) for
        introspection / cleanup. Sorted by (entity, attribute)."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            ra = self.config.time.relative_age
            rows = [_cortex_record_to_dict(r, relative_age=ra)
                    for r in self._cortex.current_records()]
            rows.sort(key=lambda d: (d["entity"].lower(), d["attribute"].lower()))
            if self._storage is not None:
                from pseudolife_memory.graph import norm_name
                from pseudolife_memory.memory.cortex import _norm_key
                emap = self._storage.entity_id_map()
                for d in rows:
                    d["entity_id"] = emap.get(norm_name(d["entity"]))
                    d["source_entries"] = self._storage.traces_for_slot(
                        _norm_key(d["entity"]), _norm_key(d["attribute"]))
            return {"count": len(rows), "entries": rows}

    def history(self, entity: str, attribute: str) -> dict[str, Any]:
        """The version timeline at a ``(entity, attribute)`` slot — current +
        superseded records, oldest→newest by tx_time, each attributed
        (writer_id / session_id) with its temporal stamp. The agent's "how did
        this fact change, and who changed it?" view (v0.4 T8)."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            ra = self.config.time.relative_age
            recs = self._cortex.records_for(entity, attribute)
            recs = sorted(recs, key=lambda r: (r.tx_time or r.asserted_at))
            return {
                "entity": entity, "attribute": attribute, "count": len(recs),
                "versions": [_cortex_record_to_dict(r, relative_age=ra)
                             for r in recs],
            }

    def get_entry(self, entry_id: int) -> dict[str, Any]:
        """Dereference a trace pointer: the dense episode + the facts it formed.
        Bumps access_count (ambient reinforcement). {found: False, faded: True}
        when the episode has evicted."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"found": False, "faded": True}
            row = self._storage.get_entry(int(entry_id))
            if row is None:
                return {"found": False, "faded": True}
            self._storage.bump_access_count(int(entry_id), 1)
            if self._cms is not None:
                self._cms.bump_entry_access_count(int(entry_id), 1)
            facts = self._storage.facts_for_entry(int(entry_id))
        return {"found": True, "entry_id": row["id"], "text": row["text"],
                "source": row.get("source"),
                "reinforcements": row.get("reinforcements", 0),
                "access_count": row.get("access_count", 0) + 1,  # +1 for the bump just applied
                "consolidated_into": facts}

    def reinforce(self, entry_id: int) -> dict[str, Any]:
        """The 'this episode was useful' signal — bump reinforcements (Phase-2
        retention reads it). No-op on a faded episode."""
        with self._lock:
            self._ensure_init()
            if self._storage is None or self._storage.get_entry(int(entry_id)) is None:
                return {"reinforced": False, "faded": True}
            self._storage.bump_reinforcements(int(entry_id), 1)
            if self._cms is not None:
                self._cms.bump_entry_reinforcements(int(entry_id), 1)
        return {"reinforced": True, "entry_id": int(entry_id)}

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
                        "db_id": e.db_id,
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
                self._cortex.meta_dirty = True   # cursor rides the meta sync
                self._save_cortex()
            return {"dream_cursor": self._cortex.dream_cursor}

    def cortex_dedup(self, threshold: float = 0.90, dry_run: bool = True) -> dict[str, Any]:
        """One-time, reviewed cleanup of paraphrase sibling slots left by past
        regex auto-promotes. Dry-run by default (reports, writes nothing). Reuses
        the value-free slot embedding, backfilling any current record missing one.
        Ops-only (see ``ops/dedup_cortex.py``) — back up the bank before applying.

        Returns ``{"dry_run", "threshold", "clusters", "merged"}`` where
        ``clusters`` is a list of ``{"canonical", "retired"}`` and ``merged`` is
        the number of sibling slots retired."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None and self._embedder is not None
            for r in self._cortex.current_records():
                if r.slot_embedding is None:
                    r.slot_embedding = self._embedder.encode_single(
                        f"{r.entity} {r.attribute}".strip())
            report = self._cortex.dedup_siblings(float(threshold), apply=not dry_run)
            if not dry_run and report and self._storage is not None:
                self._save_cortex()
        return {
            "dry_run": bool(dry_run),
            "threshold": float(threshold),
            "clusters": report,
            "merged": sum(len(c["retired"]) for c in report),
        }

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
        via ``extractor`` (an extractor that yields nothing writes nothing —
        single-writer cortex, no regex fallback), write each to the cortex, advance
        the dream cursor. Returns a summary. The single consolidation path shared by
        the MCP tool and (later) the daemon sweep."""
        cap = int(limit if limit is not None else self.config.memory.dream.max_batch)
        pulled = self.dream_pull(limit=cap)
        entries = pulled["entries"]
        if not entries:
            # No new memories to consolidate, but outcome signals may still be
            # pending — synthesise lessons regardless. Still refresh the graph
            # digest so manual graph edits (cleanup, direct graph_relate) are
            # reflected even when there is no memory backlog.
            lessons = self.synthesize_lessons(extractor)
            graph_insight = self._safe_refresh_graph_insight()
            return {"pulled": 0, "claims": 0, "inserted": 0, "confirmed": 0,
                    "contested": 0, "superseded": 0, "relations": 0,
                    "cursor": pulled["cursor"], "lessons": lessons,
                    "graph_insight": graph_insight}
        from pseudolife_memory.memory.cortex import _norm_key
        import time as _time
        traces_cfg = self.config.memory.traces
        vocab = self.cortex_vocab().get("slots", [])
        tally = {"inserted": 0, "confirmed": 0, "contested": 0, "superseded": 0}
        traces_n = 0
        max_entry_failures = 3
        quarantined = 0

        def _held(reason: str, exc: Exception) -> dict[str, Any]:
            logger.warning("dream %s (%s); cursor NOT advanced, will retry "
                           "next sweep", reason, exc)
            return {"pulled": len(entries), "claims": 0, "inserted": 0,
                    "confirmed": 0, "contested": 0, "superseded": 0, "relations": 0,
                    "cursor": self._cortex.dream_cursor, "extractor_failed": True,
                    "lessons": {"signals": 0, "lessons": 0}}

        for e in entries:
            src_id = e.get("db_id")
            fail_key = src_id if src_id is not None else e["text"][:200]
            try:
                claims = list(extractor.extract([e["text"]], vocab))
            except Exception as exc:  # noqa: BLE001 — an extractor must never break a dream
                fails = self._dream_entry_failures.get(fail_key, 0) + 1
                self._dream_entry_failures[fail_key] = fails
                if fails < max_entry_failures:
                    return _held(
                        f"extractor failed ({fails}/{max_entry_failures} "
                        f"for entry {fail_key})", exc)
                # Poison pill: a deterministically-failing entry would stall
                # consolidation forever (and every retry used to re-confirm
                # the batch prefix, ratcheting its confidence). Skip it; the
                # batch commit advances the cursor past it.
                logger.warning("dream: quarantining entry %s after %d failed "
                               "extractions (%s)", fail_key, fails, exc)
                quarantined += 1
                continue
            self._dream_entry_failures.pop(fail_key, None)
            try:
                for c in claims:
                    ent, attr = self._resolve_dream_slot(c["entity"], c["attribute"])
                    if (traces_cfg.enabled and src_id is not None
                            and self._storage is not None):
                        with self._lock:
                            already = self._storage.has_trace(
                                _norm_key(ent), _norm_key(attr), src_id)
                        if already:
                            # This source entry already formed this slot once
                            # (batch retry after a mid-batch failure). A
                            # re-dream must be a no-op, not a confirmation —
                            # the confirm path ratchets confidence.
                            continue
                    res = self.cortex_write(
                        ent, attr, c["value"],
                        confidence=float(c.get("confidence", 0.55)),
                        support=c.get("origin", "agent"))
                    tally[res["action"]] = tally.get(res["action"], 0) + 1
                    if (traces_cfg.enabled and src_id is not None
                            and self._storage is not None):
                        # Serialize trace writes on the shared psycopg connection:
                        # dream_run holds no outer lock (cortex_write locks internally
                        # and has already released), so the trace writes must take
                        # self._lock themselves. Scope it to JUST these calls — the
                        # lock is non-reentrant, so including cortex_write would deadlock.
                        with self._lock:
                            if self._storage.add_trace(
                                    _norm_key(ent), _norm_key(attr), src_id, _time.time()):
                                self._storage.bump_reinforcements(src_id, 1)
                                if self._cms is not None:
                                    self._cms.bump_entry_reinforcements(src_id, 1)
                                traces_n += 1
            except Exception as exc:  # noqa: BLE001 — a write failure must hold the cursor too
                return _held("claim write failed", exc)
        newest = max(e["timestamp"] for e in entries)
        self.dream_commit(newest)
        texts = [e["text"] for e in entries]
        relations_n = self._dream_extract_relations(extractor, texts)
        lessons = self.synthesize_lessons(extractor)
        graph_insight = self._safe_refresh_graph_insight()
        sources_attributed = self.graph_backfill_sources().get("attributed", 0)
        return {"pulled": len(entries), "claims": sum(tally.values()),
                "cursor": newest, "relations": relations_n, **tally,
                "lessons": lessons, "graph_insight": graph_insight,
                "traces": traces_n, "sources_attributed": sources_attributed,
                "quarantined": quarantined}

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

    def _episode_subtree(self, ids: list[str] | None) -> list[str] | None:
        """Expand each episode id to itself + all descendant episode ids, so a
        session-scoped query also returns entries from its sub-episodes."""
        if not ids:
            return ids
        assert self._cms is not None
        all_eps = self._cms.episodes.episodes
        want = set(ids)
        # walk parent chains; an episode is in-scope if any ancestor is requested
        out = set(ids)
        for ep in all_eps.values():
            cur = ep
            seen: set[str] = set()
            while cur is not None and cur.id not in seen:
                if cur.id in want:
                    out.add(ep.id)
                    break
                seen.add(cur.id)
                cur = all_eps.get(cur.parent_id) if cur.parent_id else None
        return list(out)

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
            "session_key": getattr(ep, "session_key", None),
            "parent_id": getattr(ep, "parent_id", None),
        }

    def episode_start(
        self, title: str, hint: str | None = None,
    ) -> dict[str, Any]:
        """Open a NESTED sub-episode under the CALLER's open session episode;
        the parent stays open. The caller's session is the request's
        ``X-PL-Session`` (resolved via the writer seam); without one it nests
        under the global current leaf. Falls back to a root when nothing open."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            _, session_id = self._resolve_writer()
            ep = self._cms.episodes.start_nested(
                title=title, hint=hint, session_key=session_id)
            self._persist_episodes()
            return self._episode_to_dict(ep)

    def episode_end(self) -> dict[str, Any]:
        """Close the caller's currently-open leaf episode and pop to its parent.
        Empty dict if none open for the caller's session."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            _, session_id = self._resolve_writer()
            closed = self._cms.episodes.end_leaf(session_key=session_id)
            self._persist_episodes()
            return self._episode_to_dict(closed) if closed is not None else {}

    def episode_start_session(
        self, session_key: str | None, title: str, hint: str | None = None,
    ) -> dict[str, Any]:
        """Idempotent open for a shim-driven session episode.

        If an episode is already open with the same ``session_key`` (a
        resume/compact re-fire), return it unchanged. Otherwise open a new
        root — WITHOUT closing any other session's open episode, so concurrent
        sessions (different projects) coexist cleanly.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            ep = self._cms.episodes.start_session(
                title=title, session_key=session_key, hint=hint)
            self._persist_episodes()
            return self._episode_to_dict(ep)

    def episode_end_session(
        self, session_key: str | None, run_dream: bool = True,
    ) -> dict[str, Any]:
        """Cascade-close the root session episode matching ``session_key`` and
        any still-open descendants. If the closed subtree captured ZERO entries
        it is deleted (prune-on-empty-close) — no empty husk is persisted, no
        dream fires, and ``{}`` is returned. Otherwise (optionally) fire a
        background dream so the session's outcome signals become lessons by the
        next session start, and return the closed root episode dict.

        ``session_key=None`` closes ANY open root (a force-close escape hatch).
        The shim always supplies a real key, so that only applies to a direct
        ``POST /api/episode/end`` with no ``session_key``."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            result, fire = self._close_session_locked(session_key, run_dream)
        if fire:
            self._fire_and_forget_dream()
        return result

    def _close_session_locked(
        self, session_key: str | None, run_dream: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Cascade-close the session root for ``session_key`` and prune the
        subtree if it captured zero entries. Caller MUST hold the lock and have
        ensured init (so both ``episode_end_session`` and the idle reaper can
        reuse it without re-entering the non-reentrant lock). Returns
        ``(result_dict, should_fire_dream)``; ``result_dict`` is ``{}`` when
        nothing matched or the subtree was pruned empty."""
        assert self._cms is not None
        em = self._cms.episodes
        closed = em.end_session(session_key)
        result = self._episode_to_dict(closed) if closed is not None else {}
        pruned = False
        if closed is not None:
            subtree = {closed.id} | {
                e.id for e in em.episodes.values()
                if em._descends_from(e, closed.id)
            }
            counts = self._episode_entry_counts()
            if sum(counts.get(i, 0) for i in subtree) == 0:
                for i in subtree:
                    em.remove(i)
                    self._delete_episode_row(i)
                pruned = True
        if not pruned:
            self._persist_episodes()
        fire = bool(run_dream and result and not pruned)
        return ({} if pruned else result), fire

    def reap_idle_sessions(
        self, idle_seconds: float, now: float | None = None,
    ) -> dict[str, Any]:
        """Close session episodes with no activity for ``idle_seconds``.

        In the direct-HTTP transport there is no session-end signal, so a
        session episode is closed here once idle: empty ones are pruned, and
        non-empty ones are closed (firing one end-of-session dream so outcome
        signals become lessons). A later store from the same client lazily
        opens a fresh episode. ``now`` is injectable for tests. Returns
        ``{"reaped": int, "session_keys": [...]}``."""
        now = time.time() if now is None else now
        reaped: list[str] = []
        fired_any = False
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            em = self._cms.episodes
            # newest entry timestamp per episode (session activity proxy)
            last_ts: dict[str, float] = {}
            for band in self._cms.bands:
                for e in band.entries:
                    if e.episode_id and e.timestamp > last_ts.get(e.episode_id, 0.0):
                        last_ts[e.episode_id] = e.timestamp
            # candidate roots: open, session-keyed; activity = newest across subtree
            targets: list[str] = []
            for root in list(em.episodes.values()):
                if (root.ended_at is not None or root.parent_id is not None
                        or not root.session_key):
                    continue
                activity = last_ts.get(root.id, root.started_at)
                for e in em.episodes.values():
                    if (e.id != root.id and e.id in last_ts
                            and em._descends_from(e, root.id)):
                        activity = max(activity, last_ts[e.id])
                if now - activity >= idle_seconds:
                    targets.append(root.session_key)
            for sk in targets:
                _result, fire = self._close_session_locked(sk, run_dream=True)
                reaped.append(sk)
                fired_any = fired_any or fire
        if fired_any:
            self._fire_and_forget_dream()
        return {"reaped": len(reaped), "session_keys": reaped}

    def _fire_and_forget_dream(self) -> None:
        """Run one dream cycle in a daemon thread so SessionEnd never blocks on
        the extractor. Errors are logged, never raised."""
        import threading

        def _run() -> None:
            try:
                from pseudolife_memory.memory.dream import build_extractor
                self.dream_run(build_extractor(self.config.memory.dream))
            except Exception:  # noqa: BLE001 — background best-effort
                logger.warning("session-end dream failed", exc_info=True)

        threading.Thread(target=_run, name="session-end-dream",
                         daemon=True).start()

    def _persist_episodes(self) -> None:
        """Write-through the episode log (small; a full upsert sweep is the
        simplest correct sync). Caller holds the lock. No-op in file mode."""
        if self._storage is None or self._cms is None:
            return
        from pseudolife_memory.storage.sync import episode_row
        try:
            for ep in self._cms.episodes.episodes.values():
                self._storage.upsert_episode(episode_row(ep))
        except Exception as exc:  # noqa: BLE001
            logger.warning("episode write-through failed: %s", exc)

    def _episode_entry_counts(self) -> dict[str, int]:
        """entry_id-count per episode, walking all bands once. Promoted entries
        still count under their original episode (they keep ``episode_id``)."""
        counts: dict[str, int] = {}
        if self._cms is None:
            return counts
        for band in self._cms.bands:
            for entry in band.entries:
                if entry.episode_id:
                    counts[entry.episode_id] = counts.get(entry.episode_id, 0) + 1
        return counts

    def _delete_episode_row(self, episode_id: str) -> None:
        """Best-effort persistent delete of one episode row. No-op in file mode."""
        if self._storage is None:
            return
        try:
            self._storage.delete_episode(episode_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("episode delete failed: %s", exc)

    def _ensure_session_episode(self, session_key: str | None) -> str | None:
        """Daemon-owned lazy episode open. In the direct-HTTP transport there is
        no stdio shim (and no SessionStart hook) to open a session episode, so
        the first store carrying a stable session id — the transport's
        ``mcp-session-id``, or a shim's ``X-PL-Session`` — opens one here. No-op
        when an episode is already open for the key, or when there's no key
        (e.g. a background/internal writer). Returns the open episode id or None.

        Caller holds the lock and has ensured init. Title is generic (the daemon
        has no project ``cwd``); see the session-title follow-up."""
        if not session_key or self._cms is None:
            return None
        em = self._cms.episodes
        existing = em.open_leaf_for(session_key)
        if existing is not None:
            return existing.id
        title = time.strftime("session - %Y-%m-%d %H:%M")
        ep = em.start_session(title=title, session_key=session_key)
        self._persist_episodes()
        logger.info("opened session episode %s (session_key=%s)", ep.id, session_key)
        return ep.id

    def set_session_title(self, title: str) -> dict[str, Any]:
        """Rename THIS request's session episode (the root keyed by the caller's
        session id). The daemon can't see the client's project directory, so
        session titles default to a generic ``session - <date> <time>``; an
        agent that knows its project calls this to name the session. Opens a
        session episode if none is open yet (so it can be called up front).
        Returns ``{"ok": bool, "id": str, "title": str}`` or
        ``{"ok": False, "reason": ...}``."""
        title = (title or "").strip()
        if not title:
            return {"ok": False, "reason": "empty title"}
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            _, session_id = self._resolve_writer()
            if not session_id:
                return {"ok": False, "reason": "no session id on this request"}
            em = self._cms.episodes
            root = next(
                (e for e in em.episodes.values()
                 if e.session_key == session_id and e.parent_id is None
                 and e.ended_at is None),
                None,
            )
            if root is None:
                root = em.start_session(title=title, session_key=session_id)
            else:
                root.title = title
            self._persist_episodes()
            return {"ok": True, "id": root.id, "title": title}

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
            counts = self._episode_entry_counts()
            rows = []
            for ep in eps:
                row = self._episode_to_dict(ep)
                row["entry_count"] = counts.get(ep.id, 0)
                rows.append(row)
            return {"count": len(rows), "episodes": rows}

    def episode_prune_empty(self, include_open: bool = False) -> dict[str, Any]:
        """Delete episodes that have zero attached entries. By default only
        CLOSED ones — the currently-open session episodes are live and kept.
        Returns ``{"deleted": int, "ids": [...]}``. This is the one-shot
        cleanup for the empty/spurious husks accumulated under the old
        single-pointer model."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            counts = self._episode_entry_counts()
            em = self._cms.episodes
            victims = [
                e.id for e in list(em.episodes.values())
                if counts.get(e.id, 0) == 0
                and (include_open or e.ended_at is not None)
            ]
            for i in victims:
                em.remove(i)
                self._delete_episode_row(i)
            return {"deleted": len(victims), "ids": victims}

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
                session_key=self._resolve_writer()[1],
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
            registry = {r["name"]: r for r in self._graph.load_relations()}
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
            edge = self._graph.upsert_edge(
                src_e["id"], resolved, dst_e["id"],
                confidence=confidence, origin=origin,
            )
            return {
                "src": src_e["display"],
                "relation": resolved,
                "dst": dst_e["display"],
                "confidence": round(edge["confidence"], 4),
                "warnings": warnings,
            }

    def graph_assign_scope(self, entity: str, source: str) -> dict[str, Any]:
        """Assign a project/source scope to an entity via manual entity_sources entry."""
        from pseudolife_memory import graph as G
        import time as _time
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            e = self._storage.find_entity(G.norm_name(entity))
            if e is None:
                return {"assigned": False, "reason": "unknown_entity", "entity": entity}
            self._storage.upsert_entity_source(e["id"], source, "manual", _time.time())
        return {"assigned": True, "entity": e["display"], "source": source}

    def graph_unrelate(self, src: str, relation: str, dst: str) -> dict[str, Any]:
        """Mark an edge superseded (kept for audit, hidden from queries)."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            registry = [r["name"] for r in self._graph.load_relations()]
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
            removed = self._graph.supersede_edge(src_e["id"], resolved, dst_e["id"])
            return {"removed": removed, "src": src_e["display"],
                    "relation": resolved, "dst": dst_e["display"]}

    def graph_delete_entity(self, entity: str) -> dict[str, Any]:
        """Hard-delete a graph entity (and cascade its edges/aliases). Facts/lessons
        that reference it are unlinked (entity_id set to NULL) but not deleted."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            e = self._storage.find_entity(G.norm_name(entity))
            if e is None:
                return {"deleted": False, "reason": "unknown_entity", "entity": entity}
            ok = self._storage.delete_entity(e["id"])
        return {"deleted": ok, "entity": e["display"]}

    def graph_merge(self, from_entity: str, into_entity: str) -> dict[str, Any]:
        """Fold ``from_entity`` into ``into_entity``: re-point edges/facts/lessons,
        carry aliases + sources, then delete ``from`` (CASCADE clears leftovers)."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            a = self._storage.find_entity(G.norm_name(from_entity))
            b = self._storage.find_entity(G.norm_name(into_entity))
            if a is None or b is None:
                return {"merged": False, "reason": "unknown_entity",
                        "from": from_entity, "into": into_entity}
            if a["id"] == b["id"]:
                return {"merged": False, "reason": "same_entity", "into": b["display"]}
            ok = self._storage.merge_entity(a["id"], b["id"])
        return {"merged": ok, "from": a["display"], "into": b["display"]}

    def graph_review(self, scope: str | None = None) -> dict[str, Any]:
        from pseudolife_memory.memory import graph_review as gr
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"findings": [], "counts": {"total": 0}}
            g = self._storage.load_graph()
            src_map = self._storage.entity_sources_map()
            proposals = self._storage.pending_proposals()
            entity_proposals = self._storage.pending_entity_proposals()
        entities, edges = g["entities"], g["edges"]
        if scope and scope != "all":
            keep = {eid for eid, ss in src_map.items() if scope in ss}
            entities = [e for e in entities if e["id"] in keep]
            edges = [e for e in edges if e["src_id"] in keep and e["dst_id"] in keep]
        return gr.review(edges, entities, src_map, proposals=proposals,
                         entity_proposals=entity_proposals)

    def entity_provenance(self, entity: str, *, limit: int = 20) -> dict[str, Any]:
        """Why does this entity exist? Its project attribution (entity_sources)
        plus the MIRAS source entries behind its facts — band/source/ts/text — so
        a human reviewing a merge/junk/link finding can judge from real evidence
        instead of names alone."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            e = self._storage.find_entity(G.norm_name(entity))
            if e is None:
                return {"found": False, "entity": entity, "sources": [], "entries": []}
            sources = self._storage.sources_for_entity(e["id"])
            entries = self._storage.entries_for_entity(e["id"], limit=limit)
        return {"found": True, "entity": e["display"],
                "sources": sources, "entries": entries}

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
            n = norm_name(name)
            if not n or not (description or "").strip():
                return {"error": "name_and_description_required"}
            registry = {r["name"]: r for r in self._graph.load_relations()}
            if registry.get(n, {}).get("builtin"):
                return {"error": "builtin_relation",
                        "hint": f"'{n}' is a builtin and cannot be redefined."}
            inv = None
            if inverse_of:
                inv = norm_name(inverse_of)
                if inv not in registry and inv != n:
                    return {"error": "unknown_inverse", "inverse_of": inv,
                            "known": sorted(registry)}
            self._graph.upsert_relation(
                n, description.strip(), src_type=src_type, dst_type=dst_type,
                transitive=bool(transitive), inverse_of=inv,
            )
            return {"defined": n, "transitive": bool(transitive),
                    "inverse_of": inv, "src_type": src_type,
                    "dst_type": dst_type}

    def graph_projects(self) -> dict[str, Any]:
        """Return all project sources with their entity counts.

        Returns ``{"projects": [{"source": str, "entities": int}, ...]}``.
        """
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"projects": []}
            return {"projects": self._storage.project_source_counts()}

    def _whole_graph(self, scope: str | None, include_facts: bool,
                     max_nodes: int | None = None) -> dict[str, Any]:
        """Return every entity/edge in the graph, optionally filtered to a
        source ``scope``. Each node carries a ``sources`` list. Used by the
        seedless ``graph_neighborhood(entity=None)`` path.

        When more than ``max_nodes`` nodes match, keep only the highest-degree
        hubs (edges are filtered to the kept set) and flag ``truncated`` with
        the pre-cap ``total_nodes``/``total_edges`` — an unbounded whole graph
        pours 800+ nodes onto the canvas and pegs the O(n²) force sim."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            g = self._storage.load_graph()
            comm = self._storage.load_communities()["assignment"]
            src_map = self._storage.entity_sources_map()
            facts_by_norm: dict[str, list[dict]] = {}
            if include_facts and self._cortex is not None:
                for rec in self._cortex.current_records():
                    facts_by_norm.setdefault(G.norm_name(rec.entity), []).append({
                        "attribute": rec.attribute, "value": rec.value,
                        "origin": rec.origin,
                        "confidence": round(float(rec.confidence), 4)})
        keep = None
        if scope and scope != "all":
            keep = {eid for eid, ss in src_map.items() if scope in ss}
        by_id, nodes = {}, []
        for e in g["entities"]:
            if keep is not None and e["id"] not in keep:
                continue
            by_id[e["id"]] = e["display"]
            node = {"entity": e["display"], "canonical": e["canonical"],
                    "etype": e["etype"], "aliases": g["aliases"].get(e["id"], []),
                    "community": comm.get(e["id"]), "sources": src_map.get(e["id"], [])}
            if include_facts:
                node["facts"] = facts_by_norm.get(e["canonical"], [])
            nodes.append(node)
        edges = [
            {"src": by_id[e["src_id"]], "relation": e["relation"],
             "dst": by_id[e["dst_id"]], "derived": False,
             "confidence": round(float(e["confidence"]), 4),
             "origin": e.get("origin")}
            for e in g["edges"]
            if e["src_id"] in by_id and e["dst_id"] in by_id]
        total_nodes, total_edges, truncated = len(nodes), len(edges), False
        if max_nodes and total_nodes > max_nodes:
            kept = _k_core_peel([n["entity"] for n in nodes], edges, max_nodes)
            nodes = [n for n in nodes if n["entity"] in kept]
            edges = [e for e in edges if e["src"] in kept and e["dst"] in kept]
            truncated = True
        return {"found": True, "entity": None, "scope": scope or "all",
                "nodes": nodes, "edges": edges, "paths": [], "truncated": truncated,
                "total_nodes": total_nodes, "total_edges": total_edges}

    def graph_neighborhood(
        self,
        entity=None,
        depth: int = 1,
        include_facts: bool = True,
        to: str | None = None,
        scope: str | None = None,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        """Subgraph within ``depth`` hops (cap 3): nodes with their current
        facts, edges (derived ones marked with rule provenance), plus the
        shortest path when ``to`` names a second entity.

        When ``entity`` is ``None`` (or falsy), returns the whole graph
        filtered to ``scope`` (a source name; ``None`` / ``"all"`` = no
        filter) via :meth:`_whole_graph`, capped to ``max_nodes`` hubs."""
        if not entity:
            return self._whole_graph(scope=scope, include_facts=include_facts,
                                     max_nodes=max_nodes)
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            _comm = st.load_communities()["assignment"]
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
            reg_for_view = self._graph.subgraph(
                root["id"], depth=depth, to_id=to_id)
            sub = {"nodes": reg_for_view["nodes"],
                   "edges": reg_for_view["edges"],
                   "paths": reg_for_view["paths"]}
            by_id = reg_for_view["entities"]
            aliases = reg_for_view["aliases"]

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
                    "aliases": aliases.get(nid, []),
                }
                if include_facts:
                    node["facts"] = facts_by_norm.get(e["canonical"], [])
                node["community"] = _comm.get(nid)
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

    def _contested_facts(self) -> list[dict]:
        """Contested cortex facts shaped for graph_insight.suggest_questions.
        Mirrors how cortex_search detects contention: current_records() +
        contenders_for(). CortexRecord exposes .entity/.attribute/.value."""
        out = []
        with self._lock:
            self._ensure_init()
            if self._cortex is None:
                return out
            for r in self._cortex.current_records():
                conts = self._cortex.contenders_for(r.entity, r.attribute)
                if conts:
                    out.append({
                        "entity": r.entity, "attribute": r.attribute, "value": r.value,
                        "contender_value": conts[0].value,
                        "contender_origin": conts[0].origin,
                    })
        return out

    def _refresh_graph_insight(self) -> dict[str, Any]:
        """Recompute communities + digest from the live graph and persist. Read
        inputs under the lock, compute lock-free, persist under the lock."""
        import time as _time
        from pseudolife_memory.memory import graph_insight as gi
        cfg = self.config.memory.graph_insight
        if not cfg.enabled:
            return {"refreshed": False, "reason": "disabled"}
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"refreshed": False, "reason": "no_storage"}
            g = self._storage.load_graph()
            prior = self._storage.load_communities()["assignment"]
        if not g["edges"]:
            return {"refreshed": False, "reason": "empty_graph"}
        contested = self._contested_facts()
        communities = gi.detect_communities(
            g["edges"], resolution=cfg.resolution,
            max_community_fraction=cfg.max_community_fraction, algorithm=cfg.algorithm)
        communities = gi.remap_to_previous(communities, prior)
        summaries = gi.summarize_communities(communities, g["edges"], g["entities"])
        assignment = {eid: cid for cid, ids in communities.items() for eid in ids}
        computed_at = _time.time()
        digest = gi.build_digest(
            communities, summaries, g["edges"], g["entities"], contested, computed_at,
            god_top_n=cfg.god_nodes_top_n, surprises_top_n=cfg.surprises_top_n,
            questions_top_n=cfg.questions_top_n, betweenness_sample=cfg.betweenness_sample)
        with self._lock:
            self._storage.replace_communities(assignment, summaries, computed_at)
            self._storage.set_meta("graph_digest", digest)
        return {"refreshed": True, "communities": len(summaries)}

    def _safe_refresh_graph_insight(self) -> dict[str, Any]:
        """Run _refresh_graph_insight, swallowing any failure so a refresh can
        never break a dream. Shared by both dream_run paths."""
        try:
            return self._refresh_graph_insight()
        except Exception as exc:  # noqa: BLE001 — insight must never break a dream
            logger.warning("graph-insight refresh failed (%s); dream unaffected", exc)
            return {"refreshed": False, "error": str(exc)}

    def graph_backfill_sources(self) -> dict[str, Any]:
        """Refresh entity->project attribution from fact provenance. Cheap,
        idempotent, manual overrides preserved. Takes the lock itself, so callers
        must NOT hold it (mirrors graph_backfill in dream_run, which runs after
        the lock is released)."""
        import time as _time
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"attributed": 0}
            n = self._storage.backfill_entity_sources(_time.time())
        return {"attributed": n}

    def graph_digest(self) -> dict[str, Any]:
        """The persisted digest snapshot, or {available: False} if dream hasn't run."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"available": False, "reason": "no_storage"}
            digest = self._storage.get_meta("graph_digest")
        if not digest:
            return {"available": False, "reason": "no_digest"}
        return {"available": True, "digest": digest}

    def session_briefing(self, max_unsure: int = 3, max_lessons: int = 3,
                         max_world: int = 3) -> dict[str, Any]:
        """Assemble the session-start briefing: graph 'unsure-about' + avoid-first
        lessons + fresh world facts + a one-line recap of the last closed session.
        Read-only; no LLM. Each sub-call takes the lock itself, so this
        orchestrator must not hold it."""
        from pseudolife_memory.memory.briefing import format_briefing, select_lessons
        dg = self.graph_digest()
        surprises: list[dict] = []
        questions: list[dict] = []
        if dg.get("available"):
            d = dg.get("digest") or {}
            surprises = (d.get("surprises") or [])[:max_unsure]
            questions = (d.get("questions") or [])[:max_unsure]
        lessons_all = (self.lessons_dump(limit=120) or {}).get("entries", [])
        lessons = select_lessons(lessons_all, max_lessons)

        # Fresh, high-confidence world facts (drop stale; best-confidence first).
        world_all = (self.world_dump() or {}).get("entries", [])
        world = sorted(
            (w for w in world_all if not w.get("stale")),
            key=lambda w: w.get("effective_confidence", 0.0), reverse=True,
        )[:max_world]

        # Recap: newest CLOSED episode that actually captured memories.
        recap = None
        eps = (self.episode_list(limit=20, include_open=False)
               or {}).get("episodes", [])
        for e in eps:  # episode_list is newest-first
            if (e.get("entry_count") or 0) > 0:
                recap = {"title": e.get("title"), "entry_count": e.get("entry_count")}
                break

        markdown = format_briefing(surprises, questions, lessons,
                                   world=world, recap=recap)
        return {
            "available": bool(markdown),
            "markdown": markdown,
            "unsure": {"surprises": surprises, "questions": questions},
            "lessons": lessons,
            "world": world,
            "recap": recap,
        }

    def communities(self, community_id: int | None = None) -> dict[str, Any]:
        """List communities, or the members of one when community_id is given."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            loaded = self._storage.load_communities()
            g = self._storage.load_graph()
        disp = {e["id"]: e["display"] for e in g["entities"]}
        if community_id is None:
            return {"communities": loaded["communities"]}
        members = [disp.get(eid, str(eid)) for eid, cid in loaded["assignment"].items()
                   if cid == community_id]
        return {"community_id": community_id, "members": sorted(members)}

    def graph_path(self, source: str, target: str,
                   max_hops: int = 8) -> dict[str, Any]:
        """Targeted shortest path between two entities (how A connects to C).

        Bidirectional BFS over the read-model; ``max_hops`` is a path-length
        cutoff. Read-only. Returns ``{found, path, edges, hops, source,
        target}`` — ``path=[]`` / ``hops=None`` when no path within max_hops.
        """
        from pseudolife_memory import graph as Gmod
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            s = st.find_entity(Gmod.norm_name(source))
            t = st.find_entity(Gmod.norm_name(target))
            if s is None:
                return {"found": False, "missing": source}
            if t is None:
                return {"found": False, "missing": target}
            g = st.load_graph()
        by_id = {e["id"]: e for e in g["entities"]}

        def _disp(nid: int) -> str:  # mirror graph_neighborhood's guarded lookup
            return by_id[nid]["display"] if nid in by_id else str(nid)

        rel: dict[tuple[int, int], str] = {}
        for e in g["edges"]:
            rel[(e["src_id"], e["dst_id"])] = e["relation"]
        node_path = Gmod.shortest_path(g["edges"], s["id"], t["id"],
                                       max_hops=max_hops)
        if node_path is None:
            return {"found": True, "path": [], "edges": [], "hops": None,
                    "source": source, "target": target}
        labels = [_disp(nid) for nid in node_path]
        edges = []
        for a, b in zip(node_path, node_path[1:]):
            if (a, b) in rel:
                edges.append({"src": _disp(a), "relation": rel[(a, b)],
                              "dst": _disp(b)})
            elif (b, a) in rel:
                edges.append({"src": _disp(b), "relation": rel[(b, a)],
                              "dst": _disp(a)})
        return {"found": True, "path": labels, "edges": edges,
                "hops": len(node_path) - 1, "source": source, "target": target}

    def deep_dream(self, *, apply: bool = False) -> dict[str, Any]:
        """Manual full-corpus graph consolidation. Step A (self-clean) + Step B
        (candidate generation), both deterministic. dry-run (default) computes and
        returns a preview + candidates without writing; apply commits the re-score and
        (when auto_apply_safe) the provably-safe supersede/merge class. Discovered
        links are NOT written here — Step C (subagents) proposes them via
        graph_propose_links. Backup-first on apply is a runbook step, not in-method."""
        from pseudolife_memory.memory import graph_consolidation as gc
        cfg = self.config.memory.deep_dream
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            g = self._storage.load_graph()
            scope_map = self._storage.entity_sources_map()
            traces = self._storage.traces_by_entity_norm()
            entries = self._storage.load_entries()
        entities, edges = g["entities"], g["edges"]

        rescore = gc.rescore_edges(edges, entities)
        violations = gc.hard_violation_edges(edges, entities)
        dups = gc.exact_duplicate_pairs(entities, edges)
        vectors, mentions = gc.entity_context_vectors(
            entities, entries, traces, min_mentions=cfg.min_entity_mentions)
        near = gc.candidate_pairs(
            vectors, edges, entities, scope_map, mentions,
            min_similarity=cfg.min_similarity, top_k=cfg.top_k_candidates)
        merge_cands, link_cands = gc.partition_candidates(
            near, entities, edges, merge_min_similarity=cfg.merge_min_similarity)
        junk = gc.junk_entities(entities, edges, max_degree=cfg.junk_max_degree)
        candidates = self._attach_candidate_snippets(link_cands, entities, entries,
                                                     traces, cfg.max_context_snippets)

        totals = {"entities": len(entities), "edges": len(edges),
                  "candidates": len(candidates)}
        if not apply:
            return {"dry_run": True, "rescored": len(rescore),
                    "would_supersede": [self._edge_label(e, entities) for e in violations],
                    "would_merge": self._merge_labels(dups, entities),
                    "would_merge_propose": [{"from": m["from"], "into": m["into"],
                                             "similarity": m["similarity"], "reason": m["reason"]}
                                            for m in merge_cands],
                    "would_junk": [{"entity": j["display"], "reason": j["reason"]} for j in junk],
                    "candidates": candidates, "totals": totals}

        import time as _t
        superseded = merged = merge_proposed = junk_proposed = 0
        with self._lock:
            # Writes apply against the snapshot read above; like the dream's
            # graph-relation extraction, a concurrent edit between the two lock
            # windows is tolerated — supersede/merge/rescore are no-ops on a row
            # that has since changed.
            for eid, conf in rescore:
                self._storage.set_edge_confidence(eid, conf)
            if cfg.auto_apply_safe:
                for e in violations:
                    if self._storage.supersede_edge(e["src_id"], e["relation"], e["dst_id"]):
                        superseded += 1
                for frm, into in dups:
                    if self._storage.merge_entity(frm, into):
                        merged += 1
            # Non-destructive: populate the review queue regardless of auto_apply_safe.
            for m in merge_cands:
                if self._storage.insert_entity_proposal(
                        "merge", m["from_id"], m["into_id"], m["similarity"], m["reason"], _t.time()) is not None:
                    merge_proposed += 1
            for j in junk:
                if self._storage.insert_entity_proposal(
                        "junk", j["entity_id"], None, None, j["reason"], _t.time()) is not None:
                    junk_proposed += 1
        return {"applied": True, "rescored": len(rescore), "superseded": superseded,
                "merged": merged, "merge_proposed": merge_proposed,
                "junk_proposed": junk_proposed, "candidates": candidates, "totals": totals}

    def _edge_label(self, e: dict, entities: list[dict]) -> dict:
        disp = {x["id"]: x["display"] for x in entities}
        return {"src": disp.get(e["src_id"], str(e["src_id"])), "relation": e["relation"],
                "dst": disp.get(e["dst_id"], str(e["dst_id"])), "confidence": e.get("confidence")}

    def _merge_labels(self, dups: list[tuple[int, int]], entities: list[dict]) -> list[dict]:
        disp = {x["id"]: x["display"] for x in entities}
        return [{"from": disp.get(f, str(f)), "into": disp.get(t, str(t))} for f, t in dups]

    def _attach_candidate_snippets(self, candidates, entities, entries, traces, k):
        """Attach up to k context snippets per side, for the Step-C subagent prompt."""
        by_id = {e["id"]: e for e in entries}
        canon = {e["id"]: e["canonical"] for e in entities}
        def snippets(eid):
            ids = traces.get(canon.get(eid, ""), [])[:k]
            return [by_id[i]["text"] for i in ids if i in by_id][:k]
        for c in candidates:
            c["src_snippets"] = snippets(c["src_id"])
            c["dst_snippets"] = snippets(c["dst_id"])
        return candidates

    def graph_propose_links(self, proposals: list[dict]) -> dict[str, Any]:
        """Ingest Step-C subagent link proposals. Each is gated by the SAME mechanism
        production uses (resolve_relation -> closed vocab; edge_confidence; drop hard
        type-violations) and inserted into edge_proposals — never into edges."""
        from pseudolife_memory import graph as G
        from pseudolife_memory.memory.relation_quality import (
            edge_confidence, is_hard_type_violation)
        import time as _t
        proposed = skipped = 0
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            known = [r["name"] for r in self._graph.load_relations()
                     if r["name"] not in ("prefers", "avoids")]
            for p in proposals:
                src, dst = str(p.get("src", "")), str(p.get("dst", ""))
                resolved, _ = G.resolve_relation(known, str(p.get("relation", "")))
                relation = resolved or "related-to"
                if not src or not dst or G.norm_name(src) == G.norm_name(dst) \
                        or is_hard_type_violation(src, relation, dst):
                    skipped += 1
                    continue
                se = self._resolve_or_create_entity(src)
                de = self._resolve_or_create_entity(dst)
                conf = edge_confidence(src, relation, dst)
                pid = self._storage.insert_proposal(
                    se["id"], relation, de["id"], conf,
                    p.get("similarity"), p.get("rationale"), "deep-dream", _t.time())
                if pid is not None:
                    proposed += 1
                else:
                    skipped += 1
        return {"proposed": proposed, "skipped": skipped}

    def graph_accept_proposal(self, proposal_id: int) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            prop = self._storage.get_proposal(proposal_id)
            if prop is None or prop["status"] != "pending":
                return {"accepted": False, "reason": "not_pending", "id": proposal_id}
            self._graph.upsert_edge(prop["src_id"], prop["relation"], prop["dst_id"],
                                    confidence=prop["confidence"], origin="agent")
            self._storage.set_proposal_status(proposal_id, "accepted")
            disp = {e["id"]: e["display"] for e in self._storage.load_graph()["entities"]}
        return {"accepted": True, "src": disp.get(prop["src_id"]),
                "relation": prop["relation"], "dst": disp.get(prop["dst_id"])}

    def graph_reject_proposal(self, proposal_id: int) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            ok = self._storage.set_proposal_status(proposal_id, "rejected")
        return {"rejected": ok, "id": proposal_id}

    def graph_accept_entity_merge(self, proposal_id: int) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            prop = self._storage.get_entity_proposal(proposal_id)
            if prop is None or prop["status"] != "pending" or prop["kind"] != "merge":
                return {"accepted": False, "reason": "not_pending", "id": proposal_id}
            disp = {e["id"]: e["display"] for e in self._storage.load_graph()["entities"]}
            ok = self._storage.merge_entity(prop["entity_id"], prop["into_id"])
            self._storage.set_entity_proposal_status(proposal_id, "accepted")
        return {"accepted": ok, "from": disp.get(prop["entity_id"]),
                "into": disp.get(prop["into_id"])}

    def graph_accept_entity_junk(self, proposal_id: int) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            prop = self._storage.get_entity_proposal(proposal_id)
            if prop is None or prop["status"] != "pending" or prop["kind"] != "junk":
                return {"accepted": False, "reason": "not_pending", "id": proposal_id}
            disp = {e["id"]: e["display"] for e in self._storage.load_graph()["entities"]}
            ok = self._storage.delete_entity(prop["entity_id"])
            self._storage.set_entity_proposal_status(proposal_id, "accepted")
        return {"accepted": ok, "entity": disp.get(prop["entity_id"])}

    def graph_reject_entity_proposal(self, proposal_id: int) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            ok = self._storage.set_entity_proposal_status(proposal_id, "rejected")
        return {"rejected": ok, "id": proposal_id}

    def _recall_vocab(self) -> list[str]:
        """Live entity vocabulary (display names + aliases) for seed matching.
        Short locked read; released before the lock-free recall loop."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return []
            g = self._storage.load_graph()
        names: list[str] = [e["display"] for e in g.get("entities", [])]
        for al in g.get("aliases", {}).values():
            names.extend(al)
        return list(dict.fromkeys(n for n in names if n))

    def _graph_degrees(self) -> dict[str, int]:
        """Asserted undirected degree by display name, from the read-model.
        Short locked read; released before the lock-free recall loop."""
        from pseudolife_memory.graph import degrees_by_name
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {}
            g = self._storage.load_graph()
        return degrees_by_name(g["edges"], g["entities"])

    def recall(self, query: str, hops: int | None = None,
               top_k: int | None = None, driver: str | None = None) -> dict[str, Any]:
        """Read-only multi-hop retrieval: search → graph-expand → re-query.

        Composes the public ``search`` + ``graph_neighborhood`` (each manages the
        lock); ``recall`` holds no lock itself. Returns the bridging
        edges/facts/paths single-shot search can't produce. ``low_confidence`` is
        True when no seed entity resolves (caller falls back to ``search``)."""
        from pseudolife_memory.memory.recall import (
            LLMController, MechanicalController, run_recall, simple_complete,
            _hub_threshold,
        )
        cfg = self.config.memory.recall
        hops = (max(1, min(int(cfg.default_hops), 5)) if hops is None
                else max(1, min(int(hops), 5)))
        top_k = (max(1, int(cfg.default_top_k)) if top_k is None
                 else max(1, int(top_k)))
        driver = driver or os.environ.get("PSEUDOLIFE_RECALL_DRIVER", cfg.driver)
        query = (query or "").strip()
        if not query:
            return {"query": "", "seeds": [], "entities": [], "edges": [],
                    "paths": [], "texts": [], "iterations": 0, "hops": hops,
                    "low_confidence": True}
        vocab = self._recall_vocab()
        if driver == "llm":
            dcfg = self.config.memory.dream
            controller = LLMController(lambda p: simple_complete(dcfg, p))
        else:
            controller = MechanicalController()
        degrees = self._graph_degrees() if cfg.hub_gate else {}
        threshold = (_hub_threshold(degrees.values(), cfg.hub_percentile,
                                    cfg.hub_floor) if cfg.hub_gate else None)
        state = run_recall(
            self.search, self.graph_neighborhood, vocab, query, controller,
            hops=hops, top_k=top_k, max_entities=cfg.max_entities,
            degree_fn=(degrees.get if cfg.hub_gate else None),
            hub_threshold=threshold,
            expand_budget=(cfg.expand_budget or None),
        )
        return {
            "query": query,
            "seeds": state.seeds,
            "entities": [{"entity": n, "facts": state.entity_facts.get(n, [])}
                         for n in state.entities],
            "edges": state.edges,
            "paths": state.paths,
            "texts": state.texts,
            "iterations": state.iterations,
            "hops": hops,
            "low_confidence": state.low_confidence,
        }

