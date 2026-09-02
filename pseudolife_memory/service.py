"""MemoryService — high-level wrapper over the Pseudolife memory stack.

One ``MemoryService`` per data directory. The MCP server (see
:mod:`pseudolife_memory.mcp_server`) holds a single instance for the
process lifetime and routes every MCP tool call through one of the
methods below. All methods return plain-JSON-serialisable dicts /
lists so the MCP layer can ``json.dumps`` them without further work.

Design notes
------------
* **No LLM dependency.** Reflection / HyDE were dropped from this build —
  Claude is the LLM, so the natural way to reflect is for Claude to call
  ``memory_store`` with a summary it composes itself (and, since the
  auto-outcome stage, for the dream to infer what a session never logged).

* **No silent fallbacks.** Pseudolife's chat path swallows memory errors so
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

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.consolidation import (
    Cluster,
    cluster_candidates,
)
from pseudolife_memory.memory.context_builder import _relative_time
from pseudolife_memory.memory.embedding import EmbeddingPipeline
from pseudolife_memory.memory import freshness
from pseudolife_memory.memory.reference_bank import ReferenceBank
from pseudolife_memory.memory.reranker import CrossEncoderReranker
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.service_dream import DreamOps
from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.session_title import (
    GENERIC_TITLE_RE, derive_session_title)
from pseudolife_memory.writer_context import resolve_writer_detailed
from pseudolife_memory.utils.config import AppConfig, load_config
from pseudolife_memory.utils.locks import SLOW_LOCK_SECONDS, MonitoredLock

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

    The embedding tensor is dropped by default — it's a large float vector
    (1024-d as of embedding-backbone-v25) that bloats the response and is
    meaningless to the LLM consumer.
    Pass ``include_embedding=True`` only for debug tooling.
    """
    out: dict[str, Any] = {
        # Storage row id (None in file mode / pre-persist): pairs a search
        # hit with memory_get/memory_reinforce and lets the Console render
        # its engram-trace drawer (2026-07-02 review M3).
        "id": entry.db_id,
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


# Serving-side staleness policy (memory.search.stale_policy; spec
# 2026-08-09-serving-side-staleness-design.md). ret-0809 measured that the
# annotation flags halve unqualified stale serving but the answerer's
# compliance keys on value shape — the policy binds regardless of how
# authoritative the value looks. Applied inside the shared record
# serialisers so every read surface behaves identically; the version-history
# chain (audit surface) deliberately renders with the default "annotate".
_STALE_WARNING = "stale — re-verify before relying on this value"
_STALE_QUARANTINE_WRAPPER = "(stale — re-verify; last known value below)"


def _apply_stale_policy(d: dict[str, Any], policy: str) -> dict[str, Any]:
    """Transform one rendered record per the staleness policy.

    Non-stale records are returned UNTOUCHED under every policy — the
    prereg's no-harm gate is structural, not statistical. "quarantine"
    moves data, never hides it: the raw value stays adjacent in
    ``last_known_value``."""
    if not d.get("stale") or policy == "annotate":
        return d
    if policy == "demote":
        d["warning"] = _STALE_WARNING
    elif policy == "quarantine":
        d["last_known_value"] = d["value"]
        d["value"] = _STALE_QUARANTINE_WRAPPER
    return d


def _demote_stale(entries: list[dict], policy: str) -> list[dict]:
    """Stable stale-last ordering for list surfaces under "demote" — score
    (or alphabetical) order is preserved within each group."""
    if policy == "demote":
        entries.sort(key=lambda d: bool(d.get("stale")))
    return entries


def _cortex_record_to_dict(rec, relative_age: bool = True,
                           stale_policy: str = "annotate") -> dict[str, Any]:
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
        "kind": rec.kind,
        "confidence": round(float(rec.confidence), 4),
        "origin": rec.origin,
        "support": sorted(rec.support),
        "provenance": sorted(rec.provenance),
        "asserted_at": rec.asserted_at,
        "last_confirmed": rec.last_confirmed,
        # v23 read-time currency. ``effective_confidence`` is the age-decayed
        # trust for this fact's class and equals ``confidence`` for evergreen
        # (the default), so an unmarked bank reads exactly as it did before.
        "freshness_class": rec.freshness_class,
        "effective_confidence": round(float(rec.effective_confidence()), 4),
        "stale": rec.is_stale(),
        "supersedes_value": rec.supersedes_value,
        "superseded_by_value": rec.superseded_by_value,
        "superseded_at": rec.superseded_at,
        # v0.4 writer-aware temporal stamp.
        "tx_time": rec.tx_time,
        "valid_time": rec.valid_time,
        "writer_id": rec.writer_id,
        "session_id": rec.session_id,
    }
    # v29 epistemic stance: present ONLY when the latest asserting write
    # hedged — an absent key is the plainly-asserted common case, so the
    # payload stays byte-identical for every pre-v29 fact.
    if getattr(rec, "stance", None):
        d["stance"] = rec.stance
    if relative_age:
        d["age"] = _relative_time(rec.tx_time or rec.asserted_at)
    return _apply_stale_policy(d, stale_policy)


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


def _world_record_to_dict(rec, now=None,
                          stale_policy: str = "annotate") -> dict[str, Any]:
    """Serialise a WorldRecord for transport, with read-time effective confidence
    (age-decayed) and a stale flag, plus the per-fact citation."""
    return _apply_stale_policy({
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
    }, stale_policy)


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


# Slot stores the deep dream lists cross-key duplicate candidates for, and the
# dismissed_pairs namespace prefix each uses ("lesson:" / "world:"). The
# namespaced rows can never collide with the graph-name dismissals sharing the
# table because graph.norm_name strips ":" (its separator class) while every
# curation row starts with a colon-bearing store prefix — NOT because slot
# keys are colon-free: cortex._norm_key's separator class is [\s._-/], so a
# literal ":" (arXiv ids, "MCP: X") survives inside a component. That is
# harmless here since _store_dismissed strips the prefix by fixed length,
# never by splitting on ":".
_CURATION_STORES = ("lesson", "world")

# How many derived slots ``supersede`` names inline in ``derived_flagged``.
# The exact-text pass can supersede several entries at once and one verbose
# memory can seed many slots, so the report is unbounded by construction and
# lands whole in an MCP response. 50 is a review-batch bound, not a measured
# one: a correction whose blast radius runs past it is a case for
# ``MemoryService.derived_from_entries``, the uncapped service-level form
# (no MCP surface), and ``derived_flagged_truncated`` says when that
# happened. The list is ordered live-slots-first so the cap keeps the rows a
# reader can actually act on.
DERIVED_FLAGGED_CAP = 50


def _slot_key(entity_norm: str, attribute_norm: str) -> str:
    """Identity string for a slot: normalized components joined with ``|``.
    ``_norm_key`` does NOT strip ``|``, so a literal pipe in a component would
    make the joined form ambiguous (("a|b","c") vs ("a","b|c")); fold pipes to
    ``-`` first. Both the listing (_curation_records) and the dismissal
    (curation_dismiss_duplicate) must build keys through this helper so a
    dismissal always matches the listing that produced it."""
    return f"{entity_norm.replace('|', '-')}|{attribute_norm.replace('|', '-')}"


def _store_dismissed(dismissed: set[tuple[str, str]], store: str) -> set[tuple[str, str]]:
    """The subset of dismissed_pairs rows belonging to ``store``'s namespace,
    with the prefix stripped back to bare slot keys."""
    pre = store + ":"
    return {(a[len(pre):], b[len(pre):]) for a, b in dismissed
            if a.startswith(pre) and b.startswith(pre)}


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


# ── consolidation quarantine (spec 2026-08-09-consolidation-quarantine) ──
# The two-man rule keys on WHO wrote — persisted entry metadata only (the
# prereg mandates no schema change, and the entries table stores neither
# origin nor writer_id): origin is derived from the entry's source via the
# same _origin_from_source mapping the auto-promote write path uses, and
# witness identity is ep:<episode_id> when the entry has one (the daemon's
# session identity), else src:<source>. A claim with no resolvable backing
# entry has no witness at all — it follows its own origin field.

def _quarantine_low_trust(claim: dict, src_entry: dict | None,
                          trusted: set[str]) -> bool:
    """True iff the claim's backing is agent-tier and outside ``trusted``."""
    if src_entry is not None:
        src = src_entry.get("source") or ""
        return _origin_from_source(src) == "agent" and src not in trusted
    return (claim.get("origin") or "agent") == "agent"


def _quarantine_witness(claim: dict, src_entry: dict | None) -> str:
    """Independence token stamped into the parked contender's provenance;
    a later matching claim promotes only when its token differs."""
    if src_entry is not None:
        ep = src_entry.get("episode_id")
        if ep:
            return f"ep:{ep}"
        return f"src:{src_entry.get('source') or ''}"
    return f"origin:{claim.get('origin') or 'agent'}"


def _onnx_embedding_available() -> bool:
    """True when the optional ``[onnx]`` extra (optimum) is installed."""
    import importlib.util  # noqa: PLC0415

    return importlib.util.find_spec("optimum") is not None


class MemoryService(DreamOps):
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
        # Monitored so a hold or wait past ~1s names its holder in the log
        # (2026-09-01: two sessions of external probing to localize one
        # stall that this line would have named). Drop-in for Lock().
        self._lock = MonitoredLock("service")
        # Schema v8: when a database URL is configured (param or
        # PSEUDOLIFE_MCP_DATABASE_URL), Postgres is the source of truth
        # and the in-memory bands are a write-through cache. Without it,
        # the v0.1 file mode is preserved bit-for-bit.
        self._db_url = database_url or os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
        self._storage = None
        self._graph = None  # GraphStore
        # Activity clock for handle-attributed writes. The idle reaper
        # proxies activity by band-entry timestamps, but two of the three
        # episode-handle callers (record_outcome, cortex_write) never
        # produce a band entry — without this, a handle-resumed episode is
        # re-reaped on the next sweep, firing a dream per resume cycle.
        # In-memory only: a restart re-fires the SessionStart hook anyway.
        self._episode_touches: dict[str, float] = {}
        # Tombstones for EMPTY session roots the reaper's sweep deleted:
        # id -> (session_key, ended_at, title). A briefing handle presented
        # after the sweep recreates its episode from here instead of
        # degrading (2026-09-02 incident). Persisted to storage meta —
        # unlike the touch map, a mid-break daemon restart must not lose
        # these.
        self._episode_tombstones: dict[str, tuple[str, float, str]] = {}
        # Roots the reaper closed EMPTY (deferred prune): id -> ended_at.
        # The sweep's candidate set is exactly this map — never the whole
        # episode log, where "zero live band entries" also matches history
        # whose memories were evicted or forgotten (review finding,
        # 2026-09-02). Persisted: the deferral window outlives a restart.
        self._deferred_empty_roots: dict[str, float] = {}
        # Retrieval-log write failures since process start. Both log paths
        # swallow their exceptions (a logging failure must not break the
        # search it rides on), so without this counter a broken log is
        # indistinguishable from an idle one: zero rows, green health.
        # In-memory only, like the CMS's own tier counters.
        self._retrieval_log_errors = 0
        # Same hazard, same remedy for the v33 slot-read counter: a dead
        # counter reads as "no fact is ever used" unless failures surface.
        self._slot_read_errors = 0
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
        # the human-chat defaults shipped with Pseudolife — see
        # docs/guide/configuration.md.
        # Overlay only: keys the user explicitly set in config.yaml win.
        self._apply_mcp_defaults(self.config, user_keys=_user_yaml_leaves(cfg_candidate))

        # Lazy components — built on first use.
        self._embedder: EmbeddingPipeline | None = None
        self._cms: ContinuumMemorySystem | None = None
        self._reference: ReferenceBank | None = None
        self._reranker: CrossEncoderReranker | None = None
        self._cortex: CortexStore | None = None
        self._world = None  # WorldCortexStore | None (world-knowledge cortex, v9)
        self._lessons = None  # LessonStore | None (procedural / outcome memory, v10)
        from pseudolife_memory.memory.hlc import HybridLogicalClock
        self._hlc = HybridLogicalClock()  # write ordering authority (memory/hlc.py)
        # Default writer identity; the daemon overrides per-connection (v0.4 T4).
        self._writer_id = os.environ.get("PSEUDOLIFE_WRITER_ID") or "unknown"
        self._last_saved_fingerprint = None
        # Poison-pill guard for the dream: consecutive extraction-failure
        # counts per entry (db_id). In-memory only — a daemon restart resets
        # the strikes, which just means a poison entry needs its three
        # failures again before quarantine.
        self._dream_batch_failures: dict[Any, int] = {}
        # Dream single-flight guard: every live trigger (sweep, console, MCP
        # tool, session-end) funnels into dream_run, and the extractor call
        # runs outside _lock, so two triggers racing consume the same cursor
        # window twice (observed live 2026-08-10, runs 68/69 — absorbed by
        # dedup, but double extractor cost and a contested-write window).
        # In-process is sufficient: one daemon process owns the bank.
        self._dream_run_guard = Lock()
        # Count of durable-save failures (cortex/world/lessons). Exposed via the
        # daemon /health probe so swallowed-then-surfaced saves are observable.
        self._persist_errors = 0
        # Set by _ensure_init when storage construction refuses to start
        # (schema v25's embedding-dim mismatch guard, schema.py's
        # RuntimeError) -- exposed via /health so the daemon doesn't report
        # "ok" on a bank every memory tool is about to fail against. The
        # exception still propagates to the caller; this is purely for
        # visibility.
        self._init_refusal: str | None = None
        # Set by _ensure_init when the legacy .pt import left a partial
        # bank behind (#187). Boot deliberately continues -- a half-imported
        # bank is still usable -- but the state must not be silent, so it
        # is logged at ERROR and surfaced via /health until a later boot
        # resumes the import cleanly.
        self._migration_partial: str | None = None
        # Last extractor selection made by dream_run_auto (sonnet-sidecar-cutover,
        # 2026-07-11): {"which": "primary"|"fallback", "base_url": str | None,
        # "at": float} — surfaced via dream_status. None until a dream has run.
        self._last_dream_extractor: dict | None = None
        # Identity tier 3 (spec 2026-07-18): machine-scoped active-session
        # pointer, set by the SessionStart hook / cleared by SessionEnd.
        # ``(session_id, ts)`` or None. In-memory always; persisted to
        # Postgres meta (loaded once in _ensure_init) when storage is up —
        # file-mode keeps this process-local only.
        self._active_session: tuple[str, float] | None = None
        # entity_norm -> kind cache for freshness_class="auto" inference
        # (schema v24). Loaded once from storage; None means "not loaded
        # yet", distinct from an empty (but loaded) map.
        self._entity_kind_cache: dict[str, str] | None = None

    _ACTIVE_SESSION_META_KEY = "active_session_pointer"
    _EPISODE_TOMBSTONES_META_KEY = "episode_tombstones"
    _DEFERRED_EMPTY_META_KEY = "deferred_empty_roots"

    def _resolve_writer(self) -> tuple[str, str | None]:
        """Identity tiers 1/3/4 (spec 2026-07-18): X-PL-Session header ->
        hook-registered active session -> legacy mcp-session-id (removed
        from MCP 2026-07-28). Tier 2 (episode handle) is per-call at the
        write sites; tier 5 is the reaper's idle-gap floor."""
        w, header_s, transport_s = resolve_writer_detailed(self._writer_id)
        if header_s:
            return (w, header_s)
        active_id = self._active_session_id()
        if active_id is not None:
            return (w, active_id)
        return (w, transport_s)

    def _active_session_id(self, now: float | None = None) -> str | None:
        """The tier-3 active-session pointer's session id, or None when it is
        absent or stale (finding 4, 2026-07-19).

        A client that crashes/is killed never fires SessionEnd, so its pointer
        would otherwise attribute every later tier-3 write to a dead session
        indefinitely. A pointer older than
        ``PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS`` (default 6 h — the resume
        window, past which a return starts a fresh episode anyway; ``0``
        disables the TTL, matching the resume-window convention) is treated as
        stale and ignored, so tier 3 falls through to the transport/idle-gap
        floor.

        Refresh is on-set only: SessionStart re-stamps ``ts`` (Claude Code
        re-fires it on resume/compact, keeping a genuinely active session
        alive), and resolution never mutates. A legacy pointer with no stored
        timestamp hydrates to ``ts=0.0``, which reads as infinitely old and is
        ignored until the next SessionStart re-registers it — fail-safe, never
        a crash."""
        active = getattr(self, "_active_session", None)
        if active is None:
            return None
        ttl = float(os.environ.get("PSEUDOLIFE_ACTIVE_SESSION_TTL_SECONDS",
                                   "21600"))
        if ttl > 0:
            now = time.time() if now is None else now
            if now - active[1] > ttl:
                return None
        return active[0]

    def set_active_session(self, session_id: str | None) -> None:
        """Machine-scoped active-session pointer (identity tier 3): set by
        the SessionStart hook, cleared by SessionEnd. Last-start-wins by
        design — concurrent unheaded sessions are the shim's/handle's job."""
        with self._lock:
            self._ensure_init()
            if session_id:
                self._active_session = (str(session_id), time.time())
                if self._storage is not None:
                    self._storage.set_meta(
                        self._ACTIVE_SESSION_META_KEY,
                        {"session_id": str(session_id),
                         "ts": self._active_session[1]})
            else:
                self._active_session = None
                if self._storage is not None:
                    self._storage.set_meta(self._ACTIVE_SESSION_META_KEY, None)

    def clear_active_session(self, session_id: str) -> bool:
        """Clear the pointer only if it currently names ``session_id``.

        Must not hold ``self._lock`` while calling :meth:`set_active_session`
        (non-reentrant) — check ownership under the lock, release, then
        delegate the actual clear."""
        with self._lock:
            self._ensure_init()
            cur = getattr(self, "_active_session", None)
            if cur is None or cur[0] != session_id:
                return False
        self.set_active_session(None)
        return True

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
        """Tweak Pseudolife defaults for the MCP / Claude use case.

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
        # ONNX embedder whenever the optional extra is installed (the
        # daemon image bakes it): ~3x faster single-text encode on CPU
        # with bit-identical embeddings (fp32 ONNX) -- true for MiniLM,
        # which has a baked ONNX export. Qwen3-Embedding-0.6B (the default
        # since embedding-backbone-v25) has NO in-repo ONNX export, so with
        # the [onnx] extra installed this now fires the warn-and-fall-back
        # path in EmbeddingPipeline on every construction (harmless -- it
        # falls back to torch cleanly -- but no longer silent; expect it in
        # the daemon log on every deploy that uses the Qwen default).
        # A plain pip install (no [onnx] extra) still never takes this
        # branch at all.
        if absent("embedding.backend") and _onnx_embedding_available():
            config.embedding.backend = "onnx"

    def _refuse_on_stale_hydrated_dims(self) -> None:
        """Refuse to serve a bank whose hydrated embeddings don't fit the
        live embedder.

        The Postgres path can't normally get here mismatched — schema.py's
        ``_refuse_on_embedding_dim_mismatch`` rejects a wrong-dimensioned
        bank before any DDL — but the v0.1 FILE mode hydrates ``.pt`` state
        with no dimension check at all. In the 2026-08-29 incident a
        shim-spawned fallback daemon served a retired 384-d (MiniLM-era)
        file bank against the live 1024-d embedder, and every search/store
        died with a bare torch shape error ("size mismatch, mat (12x384),
        vec (1024)") that named neither the bank nor the fix. Refusing at
        boot names both; like the schema-level guard, the remedy is a
        deliberate migration, never an automatic re-embed of a bank this
        daemon may be serving by mistake.

        On refusal ``_cms`` is dropped, so every retry of ``_ensure_init``
        rebuilds the cheap components and re-loads the ``.pt`` before
        refusing again — costlier than the schema-level guard's early
        raise, but harmless: the autosave/exit flush paths no-op on
        ``_cms is None``, so the on-disk bank is never touched.
        """
        def _dim(emb) -> int:
            return int(emb.shape[-1]) if hasattr(emb, "shape") else len(emb)

        live = int(self._embedder.embedding_dim)
        stale = 0
        found: set[int] = set()
        for band in self._cms.bands:
            for e in band.entries:
                if e.embedding is None:
                    continue
                d = _dim(e.embedding)
                if d != live:
                    stale += 1
                    found.add(d)
        for r in self._cortex.records:
            if r.embedding is None:
                continue
            d = _dim(r.embedding)
            if d != live:
                stale += 1
                found.add(d)
        if not stale:
            return
        dims = ", ".join(str(d) for d in sorted(found))
        msg = (
            f"Refusing to serve: {stale} hydrated row(s) in the bank at "
            f"{self.data_dir} are embedded at {dims} dims, but the live "
            f"embedder ({self.config.embedding.model_name}) produces "
            f"{live}-d vectors — every search/store would crash with a "
            f"torch shape error. If this daemon was started by accident "
            f"against an old or retired bank (e.g. a shim-spawned fallback "
            f"while the real Docker daemon was still booting), stop it and "
            f"point the client at the intended daemon. Otherwise migrate "
            f"deliberately: a file-mode bank is re-embedded on import when "
            f"the daemon is given Postgres storage (set "
            f"PSEUDOLIFE_MCP_DATABASE_URL, or install "
            f"pseudolife-mcp[lite]); a Postgres bank migrates with "
            f"`python ops/migrate_embeddings.py`. Or configure the "
            f"embedding model that produced these vectors."
        )
        # Surface at /health exactly like the schema-level refusal…
        self._init_refusal = msg
        # …and drop the half-built CMS: ``_ensure_init`` gates on
        # ``_cms is not None``, so leaving it set would let the next tool
        # call skip init and serve the mismatched band after all.
        self._cms = None
        raise RuntimeError(msg)

    def _ensure_postgres_storage(self):
        """Connect the durable store without loading an embedding model.

        Exact/hash-addressed paths that never embed can call this directly
        and stay cheap even as the first tool used in a session;
        ``_ensure_init`` reuses the same connection when an embedding-backed
        tool is called later. Reused across failed attempts too: a retry
        after a mid-init failure must never rebuild the connection.
        """
        if self._storage is not None:
            return self._storage
        if not self._db_url:
            raise RuntimeError(
                "this operation requires the durable Postgres tier; "
                "configure PSEUDOLIFE_MCP_DATABASE_URL or install the "
                "lite tier")
        from pseudolife_memory.storage.postgres import PostgresStorage
        try:
            self._storage = PostgresStorage(self._db_url)
        except RuntimeError as exc:
            # schema.py's dim-mismatch refusal (schema v25) fires here —
            # record it for /health, then let it propagate: this call
            # (and every retry until the bank is migrated) must still
            # fail loudly, not just silently degrade.
            self._init_refusal = str(exc)
            raise
        self._init_refusal = None
        logger.info("storage: postgres (%s)",
                    self._db_url.rsplit("@", 1)[-1])
        # Invariant: unqualified tables MUST resolve to the real `public`
        # bank, never the role-named `pseudolife` shadow schema (v0.4
        # collision fix). PostgresStorage pins this; fail loud if regressed.
        # Lives here, not in _ensure_init, so every connect path is covered.
        # A failed check must not leave a connection the reuse guard would
        # return — that would skip the invariant on every retry.
        try:
            self._assert_public_search_path()
        except Exception:
            self._storage.close()
            self._storage = None
            raise
        return self._storage

    def _ensure_init(self) -> None:
        if self._cms is not None:
            return
        logger.info("MemoryService: initialising embedder + CMS (first call).")
        # Storage connects BEFORE any model load (2026-08-04 boot balloon):
        # while Postgres is in crash-recovery after machine boot, every
        # incoming call retries this whole method, and each retry used to
        # load a fresh ~2.4 GB embedder before failing on the connect —
        # a dozen queued retries ballooned the daemon to a 31.5 GB cgroup
        # peak. A down database must cost a fast connect error, never a
        # model load.
        if self._db_url:
            self._ensure_postgres_storage()
            from pseudolife_memory.memory.graph_store import PostgresNetworkxGraphStore
            self._graph = PostgresNetworkxGraphStore(self._storage)
            # Identity tier 3: hydrate the active-session pointer left by a
            # prior process (daemon restart) so tier resolution survives it.
            raw = self._storage.get_meta(self._ACTIVE_SESSION_META_KEY)
            if isinstance(raw, dict) and raw.get("session_id"):
                self._active_session = (str(raw["session_id"]),
                                         float(raw.get("ts") or 0.0))
            # Episode tombstones survive a restart the same way — a handle
            # presented after a mid-break daemon restart must still recreate
            # its swept episode.
            raw_t = self._storage.get_meta(self._EPISODE_TOMBSTONES_META_KEY)
            if isinstance(raw_t, dict):
                self._episode_tombstones = {
                    str(tid): (str(m["session_key"]),
                               float(m.get("ended_at") or 0.0),
                               str(m.get("title") or ""))
                    for tid, m in raw_t.items()
                    if isinstance(m, dict) and m.get("session_key")}
            raw_d = self._storage.get_meta(self._DEFERRED_EMPTY_META_KEY)
            if isinstance(raw_d, dict):
                self._deferred_empty_roots = {
                    str(rid): float(ts or 0.0) for rid, ts in raw_d.items()}
        if self._embedder is None:
            # Reused across failed attempts: a retry after a mid-init
            # failure must never rebuild the model (same incident).
            self._embedder = EmbeddingPipeline(self.config.embedding)
        # Make sure the embedder dim matches the configured memory dim —
        # Qwen3-Embedding-0.6B (the default since embedding-backbone-v25) is
        # 1024-d; all-MiniLM-L6-v2 is 384-d. Whatever model is configured,
        # this line keeps memory.embedding_dim honest without hand-tuning.
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
            storage=self._storage,
        )
        if self._storage is not None:
            from pseudolife_memory.storage import migrate as _migrate
            from pseudolife_memory.storage import sync as _sync
            try:
                summary = _migrate.migrate_legacy(
                    self.data_dir, self._storage, self._embedder,
                )
                if summary.get("migrated"):
                    logger.warning("legacy .pt bank migrated: %s", summary)
                    self._migration_partial = None
                elif summary.get("reason") in _migrate.PARTIAL_REASONS:
                    self._migration_partial = summary["reason"]
                    logger.error(
                        "legacy .pt import is incomplete (%s) — the bank is "
                        "SHORT. See the '%s' meta row; restore the original "
                        ".pt sources under %s and restart to resume.",
                        summary["reason"], _migrate.MIGRATION_META_KEY,
                        self.data_dir)
            except Exception as exc:  # noqa: BLE001
                # Boot continues (a half-imported bank still serves), but
                # the remainder is NOT lost any more: migrate_legacy has
                # already recorded in_progress, so the next boot resumes.
                # ERROR rather than WARNING because this line used to be the
                # only trace of a bank that was permanently short (#187).
                self._migration_partial = f"import_failed: {exc}"
                logger.error(
                    "legacy migration failed part-way (continuing with a "
                    "SHORT bank): %s — progress is recorded in the '%s' meta "
                    "row; fix the cause and restart to resume the import.",
                    exc, _migrate.MIGRATION_META_KEY)
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

        # (ContrastiveUpdater / ContextBuilder were constructed here for the
        # legacy chat product but never called on any daemon path — the
        # construction went in the 2026-07-02 zombie sweep, the classes
        # themselves in the 2026-07-30 dead-code sweep.)

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

        self._refuse_on_stale_hydrated_dims()

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
        source: str = "agent",
        tags: list[str] | None = None,
        origin: str | None = None,
        episode: str | None = None,
    ) -> dict[str, Any]:
        """Embed and store a memory through the CMS pipeline.

        ``tags`` (schema v6) is an optional multi-valued label list.
        Normalised by the underlying CMS (lowercased / stripped /
        deduped). Tags exist alongside ``source``, not as a replacement.

        ``origin`` (``"user"`` / ``"action"`` / ``"agent"``) records who asserted
        any canonical facts auto-promoted from this text into the cortex. When
        omitted it is defaulted from ``source`` (conversation->user, claude->
        agent, tool->action). See :meth:`_promote_slots`.

        ``episode`` (identity tier 2, spec 2026-07-18): an open episode id or
        unambiguous prefix (>=8 chars) — attributes this entry to that
        episode. A header session (tier 1) still wins overall identity, but
        the entry's ``episode_id`` targets the handle regardless. An unknown/
        closed/ambiguous handle degrades silently: the store still proceeds,
        and ``"episode_warning"`` is added to the result.

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
            resolved = self._resolve_episode_handle(episode)
            episode_warning = bool(episode) and resolved is None
            if resolved is not None:
                _, header_session, _ = resolve_writer_detailed(self._writer_id)
                if not header_session:
                    session_id = resolved[1]
            self._ensure_session_episode(session_id)
            # Attribution always targets the handle's episode, even when a
            # header session won identity above (spec: identity and target
            # episode are separable) — passed into CMS.store so it lands
            # before that call's own promotion walk, which would otherwise
            # move the entry to a new object and strand a post-hoc override.
            stored, surprise = self._cms.store(
                text, embedding, source=source, tags=tags,
                session_key=session_id,
                attribution_episode_id=resolved[0] if resolved is not None else None,
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
            out = {
                "stored": stored,
                "surprise": round(float(surprise), 4),
                "reason": reason,
                "cortex_promoted": promoted,
            }
            # Nudge the agent while the lazily-opened session episode still
            # carries the generic fallback title (the daemon has no project
            # signal of its own; the agent does).
            root = self._session_root_locked(session_id)
            if root is not None and GENERIC_TITLE_RE.match(root.title or ""):
                out["episode_hint"] = (
                    "session episode is untitled — call "
                    "memory_session_title('<project> - <topic>')")
            if episode_warning:
                out["episode_warning"] = "unknown or closed episode handle"
            return out

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
                self._ensure_subject_entity(s.entity, propose_dupes=True)
                written += 1
            except ValueError as exc:
                if "holds a set" in str(exc):
                    # Task 4 spec: the extractor rule for a set-valued slot is
                    # logged-and-dropped, not auto-routed to add_member —
                    # explicit set ops come via memory_set_add/memory_set_remove
                    # (or the op field, Task 7). Caught specifically (before the
                    # catch-all below) so this doesn't collapse into the same
                    # debug-level silence as an unrelated write failure.
                    logger.info(
                        "scalar auto-promote skipped: slot holds a set: %s.%s",
                        s.entity, s.attribute)
                else:
                    logger.debug("cortex auto-promote skipped (%s): %s", claim, exc)
            except Exception as exc:  # noqa: BLE001
                logger.debug("cortex auto-promote skipped (%s): %s", claim, exc)
        return written

    def _ensure_subject_entity(self, entity: str, *,
                               propose_dupes: bool = False) -> None:
        """Fact writes create the subject's graph node (spec §5.1) so the
        cortex and graph stay joined. No-op in file mode. Caller holds the
        lock. ``propose_dupes`` (dream auto-promote only) files write-dedup
        merge proposals for near-duplicate creates."""
        if self._storage is None:
            return
        from pseudolife_memory.graph import norm_name
        from pseudolife_memory.memory.graph_consolidation import junk_name_reason
        if junk_name_reason(entity):
            return  # junk-shaped subject: keep the fact, skip the graph node
        n = norm_name(entity)
        if n and self._storage.find_entity(n) is None:
            eid = self._storage.ensure_entity(n, display=entity.strip())
            if propose_dupes:
                self._propose_write_dedup(eid, entity)

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
        contiguity_neighbors: int | None = None,
        timeline: bool | None = None,
        return_event_id: bool = False,
    ) -> dict[str, Any]:
        """Retrieve relevant memories ranked by associative similarity.

        ``return_event_id=True`` adds ``retrieval_event_id`` (the id of the
        retrieval-log row this search wrote) to the result, so the caller
        can attach the fact half of the response via
        :meth:`attach_served_facts`. Opt-in on purpose: the id is training
        plumbing, not part of the public search shape, and must not leak
        through recall/Console callers. Absent when the log is disabled or
        the write failed.

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

        ``contiguity_neighbors`` overrides
        ``config.memory.search.contiguity_neighbors`` (``None`` follows
        config): when > 0, each hit also surfaces up to that many
        stream-adjacent neighbors per side — same episode, else same
        source — placed around their parent hit in timestamp order and
        marked ``"via": "contiguity"`` with score 0.0. Passing an
        explicit ``0`` pins vanilla retrieval regardless of config (the
        eval harness's control arm relies on this).

        ``timeline`` overrides ``config.memory.search.timeline_channel``
        (``None`` follows config): when on and the query carries a
        temporal cue, lexically-relevant entries are injected (marked
        ``"via": "timeline"``) and the memory portion of the result is
        ordered by stream position instead of score. Explicit ``False``
        pins vanilla retrieval (control-arm contract, as above).
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cms is not None
            self._validate_band_filter(bands)
            episodes = self._episode_subtree(episodes)
            query = (query or "").strip()
            if not query:
                return {"entries": [], "query": "", "count": 0, "low_confidence": True}
            embedding = self._embedder.encode_query(query)
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
                timeline=timeline,
            )
            from pseudolife_memory.memory.abstain import low_confidence
            n_ctg = (self.config.memory.search.contiguity_neighbors
                     if contiguity_neighbors is None
                     else int(contiguity_neighbors))
            vias = result.via or [None] * len(result.entries)
            comps = result.components or [None] * len(result.entries)
            ranked: list[tuple[Any, float | None, str | None, dict | None]] = [
                (e, s, v, c) for e, s, v, c in
                zip(result.entries, result.scores, vias, comps)
            ]
            if n_ctg > 0 and ranked:
                # Direct hits are all pre-seen so a neighbor can never
                # duplicate one; neighbors of later hits skip entries an
                # earlier hit already surfaced.
                seen = {e.text for e, _, _, _ in ranked}
                expanded: list[
                    tuple[Any, float | None, str | None, dict | None]] = []
                for e, s, via, comp in ranked:
                    before, after = self._cms.temporal_neighbors(e, n_ctg)
                    for nb in before:
                        if nb.text not in seen:
                            expanded.append(
                                (nb, 0.0, "contiguity",
                                 {"channel": "contiguity"}))
                            seen.add(nb.text)
                    expanded.append((e, s, via, comp))
                    for nb in after:
                        if nb.text not in seen:
                            expanded.append(
                                (nb, 0.0, "contiguity",
                                 {"channel": "contiguity"}))
                            seen.add(nb.text)
                ranked = expanded
            entries_out = []
            served_components: list[dict | None] = []
            for e, s, via, comp in ranked:
                d = _entry_to_dict(e, s)
                if via is not None:
                    d["via"] = via
                entries_out.append(d)
                served_components.append(comp)
            # Chronicle events (schema v28): a temporally-cued query also
            # serves matching live events, chronologically ascending.
            # Needs no knob — an empty table (chronicle extraction
            # disabled) serves nothing, and non-cued queries skip the
            # lookup entirely.
            events_block = None
            agg_cued = False
            if self._storage is not None:
                from pseudolife_memory.memory.cms import (
                    has_aggregation_cue, has_date_cue, has_temporal_cue,
                )
                # Aggregation cues serve the FULL list (a count over a
                # capped prefix is wrong by construction); temporal-only
                # cues (cue words, or an explicit calendar date — the
                # 2026-08-12 soak-review gap) keep the original 6 — same
                # ordering, so the limit-6 result is a prefix of the
                # limit-30 one.
                agg_cued = has_aggregation_cue(query)
                if (agg_cued or has_temporal_cue(query)
                        or has_date_cue(query)):
                    hits = self._storage.chronicle_search(
                        query, limit=30 if agg_cued else 6)
                    if hits:
                        events_block = [
                            {"description": h["description"],
                             "actor": h["actor"],
                             "date": h["occurred_date"],
                             "phrase": h["occurred_phrase"]}
                            for h in hits]
            out = {
                "query": query,
                "count": len(entries_out),
                # Confidence is judged on the *direct* hits only —
                # structural neighbors carry score 0.0 by construction
                # and must not drag the floor check down.
                "low_confidence": low_confidence(
                    list(result.scores),
                    self.config.memory.search_confidence_floor,
                ),
                "entries": entries_out,
            }
            if events_block:
                out["events"] = events_block
                if agg_cued:
                    # A computed property of the list (not a claimed
                    # answer): lets the answerer do arithmetic over a
                    # long enumeration without recounting lines.
                    out["events_total"] = len(events_block)
            # Retrieval event log (schema v31/v32): purely observational —
            # the (query, served, components, params) training tuple for a
            # learned reranker.
            params = dict(result.params or {})
            params["contiguity_neighbors"] = n_ctg
            evt_id = self._log_retrieval_event(
                query, entries_out, served_components, params)
            if return_event_id and evt_id is not None:
                out["retrieval_event_id"] = evt_id
            return out

    # ------------------------------------------------------------------
    # Tool: trace — search + structured ranking trace
    # ------------------------------------------------------------------

    def _validate_band_filter(self, bands: list[str] | None) -> None:
        """Unknown band names raise instead of silently matching nothing.

        Before 2026-08-15 a ``bands=`` filter naming a band the preset
        doesn't have (e.g. the continuum-era ``["working","instant"]``
        under the flat default) returned an empty result set with no
        error — indistinguishable from "nothing relevant stored". Caller
        must already hold self._lock with the CMS initialised.
        """
        if not bands:
            return
        assert self._cms is not None
        valid = {b.name for b in self._cms.bands}
        unknown = [b for b in bands if b not in valid]
        if unknown:
            raise ValueError(
                f"unknown band name(s) {unknown!r} — this preset has "
                f"{sorted(valid)!r}")

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
            self._validate_band_filter(bands)
            query = (query or "").strip()
            if not query:
                return {
                    "query": "", "count": 0, "entries": [], "trace": None,
                }
            embedding = self._embedder.encode_query(query)
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
            # ``seq`` tie-breaks entries whose wall-clock timestamps collide
            # within one tick — same-tick stores must still list newest-first.
            all_entries.sort(key=lambda e: (e.timestamp, e.seq), reverse=True)
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
                emb = self._embedder.encode_query(old_text)
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

            # Retract traversal: what the dream derived from the memories
            # just corrected. Reported, never cascaded — the caller decides
            # whether a derivation still holds. The same facts also carry
            # ``re_verify`` on later reads, but that is a BEST-EFFORT read of
            # still-present evidence and stops the moment the superseded
            # entry is evicted (see ``_annotate_evidence_supersession``).
            # This list, named once at the moment of correction, is the
            # durable half of the affordance — which is why it is capped
            # rather than truncated silently.
            derived_total = self._derived_from_entries_locked(
                [e.db_id for e in superseded_entries])
            derived = derived_total[:DERIVED_FLAGGED_CAP]

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
                "derived_flagged": derived,
                "derived_flagged_total": len(derived_total),
                "derived_flagged_truncated": len(derived) < len(derived_total),
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
                # Retrieval log liveness: nothing else reads the table, and
                # both write paths swallow their errors, so this is the only
                # place a silently-dead log becomes visible. Guarded: a
                # feature whose premise is "logging must never break the
                # caller" must not break memory_stats either (stats() is on
                # the session-start path).
                try:
                    log = self._storage.retrieval_log_health()
                except Exception:  # noqa: BLE001
                    logger.warning("retrieval-log health read failed",
                                   exc_info=True)
                    log = {"events": None, "uses": None,
                           "last_event_at": None, "unavailable": True}
                log["enabled"] = bool(self.config.memory.retrieval_log.enabled)
                log["write_errors"] = self._retrieval_log_errors
                result["retrieval_log"] = log
                # Read audit (schema v33): never-read fractions + the
                # read/write balance. Same guard rationale as the retrieval
                # log — stats() is on the session-start path and must not
                # break on a telemetry read.
                try:
                    audit = self._storage.read_audit()
                except Exception:  # noqa: BLE001
                    logger.warning("read-audit computation failed",
                                   exc_info=True)
                    audit = {"unavailable": True}
                # Graduation candidates: entries the retrieval layer
                # re-serves in most sessions are static-context
                # ("promote to CLAUDE.md") candidates — paying per
                # query for what could be standing context. Empty
                # until the event log holds enough distinct sessions.
                # Guarded SEPARATELY from read_audit: an advisory report
                # must not destroy the audit it rides in (the same rule
                # that keeps both out of the search path).
                try:
                    audit["graduation_candidates"] = (
                        self._storage.graduation_report())
                except Exception:  # noqa: BLE001
                    logger.warning("graduation report failed",
                                   exc_info=True)
                    audit["graduation_candidates"] = []
                audit["slot_tracking_enabled"] = bool(getattr(
                    self.config.memory.cortex, "read_tracking", True))
                audit["slot_read_write_errors"] = self._slot_read_errors
                result["read_audit"] = audit
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
            embedding = self._embedder.encode_query(query)
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
        # Per-part durations, warned on a slow save: every persist runs
        # under the service lock, so a slow part IS a daemon pause — and
        # the 2026-08-31 probe caught a ~1.5s autosave-correlated stall
        # that none of the offline-measurable parts explains (weights
        # ~5ms, access counts ~10-30ms at live size). This breakdown makes
        # the live daemon name the part next time instead of leaving it to
        # correlation.
        t_start = time.perf_counter()
        timings: dict[str, float] = {}

        def _timed(part, fn):
            t0 = time.perf_counter()
            out = fn()
            timings[part] = round(time.perf_counter() - t0, 3)
            return out

        def _finish(result):
            # Wall time, not sum-of-parts: same meaning as the sweep
            # ledger's total, and it still counts a part that raised.
            timings["total"] = round(time.perf_counter() - t_start, 3)
            result["timings"] = timings
            if timings["total"] >= SLOW_LOCK_SECONDS:
                logger.warning("%s save held the service lock %.2fs: %s",
                               kind, timings["total"], timings)
            return result

        if self._storage is not None:
            _timed("weights",
                   lambda: self._cms.save_weights(self.config.memory.save_dir))
            pairs = [
                (e.db_id, e.access_count)
                for band in self._cms.bands
                for e in band.entries
                if e.db_id is not None
            ]
            try:
                _timed("access_counts",
                       lambda: self._storage.update_access_counts(pairs))
            except Exception as exc:  # noqa: BLE001
                logger.warning("access-count sync failed: %s", exc)
            if kind in ("explicit", "flush"):
                # Full resync on the rare explicit/exit saves — belt and
                # braces against any dirty-mark gap in the per-slot path.
                from pseudolife_memory.storage import sync as _sync
                _timed("cortex",
                       lambda: _sync.snapshot_cortex(self._cortex, self._storage))
                if self._world is not None:
                    _timed("world", lambda: _sync.snapshot_world_cortex(
                        self._world, self._storage))
                if self._lessons is not None:
                    _timed("lessons", lambda: _sync.snapshot_lessons(
                        self._lessons, self._storage))
            else:
                _timed("cortex", self._save_cortex)
                _timed("world", self._save_world)
                _timed("lessons", self._save_lessons)
            return _finish({"saved_to": self.config.memory.save_dir,
                            "mode": "postgres+weights", "kind": kind})
        _timed("bank", lambda: self._cms.save(self.config.memory.save_dir))
        _timed("cortex", self._save_cortex)
        return _finish({"saved_to": self.config.memory.save_dir, "kind": kind})

    def _entity_kind_map(self) -> dict[str, str]:
        """entity_norm -> kind, cached. Order 1k rows and read on every fact
        write, so it is loaded once -- and it stays cached for the life of
        this process. The backfill (``evals/apply_entity_kinds.py``) runs
        out-of-process, so it cannot reach this cache: a running daemon keeps
        serving whatever map it loaded (typically empty) after a backfill,
        silently resolving every write to ``evergreen`` until the daemon is
        restarted."""
        if self._entity_kind_cache is None:
            self._entity_kind_cache = (
                self._storage.load_entity_kinds() if self._storage else {})
        return self._entity_kind_cache

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
        episode: str | None = None,
        freshness_class: str = "auto",
        force_contend: bool = False,
        stance: str | None = None,
    ) -> dict[str, Any]:
        """Write / confirm / supersede a canonical fact at the
        ``(entity, attribute)`` slot. The claim is embedded through the same
        pipeline as memories so cortex search shares the embedding space.

        ``force_contend`` (internal; consolidation quarantine): park the
        value as a contender regardless of tier — including at an empty
        slot. Not exposed on the MCP surface.

        ``support`` records who asserted the fact — ``"user"`` (the human stated
        it), ``"action"`` (a tool/agent action confirmed it), or ``"agent"`` (the
        agent merely said it). It accumulates on the record (``origin`` = the
        strongest tier seen), so corroboration is first-class.

        ``episode`` (identity tier 2): an open episode id or unambiguous
        prefix (>=8 chars). Facts have no separate attribution field, so a
        valid handle IS the identity for this call — it becomes the
        ``session_id`` stamp, unless a header session (tier 1) is present, in
        which case the header wins outright. An unknown/closed/ambiguous
        handle degrades silently: the write still proceeds, and
        ``"episode_warning"`` is added to the result.

        Returns ``{"action": "inserted"|"confirmed"|"superseded"|"contested",
        ...record fields}``. On ``"contested"`` the returned record is the parked
        *contender* and a ``"current"`` key carries the canonical value that won,
        so the caller sees both sides of the conflict.
        """
        with self._lock:
            self._ensure_init()
            if freshness_class == "auto":
                from pseudolife_memory.memory.cortex import _norm_key
                freshness_class = freshness.resolve_class(
                    self._entity_kind_map().get(_norm_key(entity)), _norm_key(attribute))
            assert self._embedder is not None and self._cortex is not None
            claim = f"{entity} {attribute} {value}".strip()
            emb = self._embedder.encode_single(claim)
            slot_emb = self._embedder.encode_single(f"{entity} {attribute}".strip())
            writer_id, session_id = self._resolve_writer()
            resolved = self._resolve_episode_handle(episode)
            episode_warning = bool(episode) and resolved is None
            if resolved is not None:
                _, header_session, _ = resolve_writer_detailed(self._writer_id)
                if not header_session:
                    session_id = resolved[1]
            try:
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
                    freshness_class=freshness_class,
                    force_contend=force_contend,
                    stance=stance,
                )
            except ValueError as exc:
                if "holds a set" in str(exc):
                    # Tool-boundary mapping (Task 4): the store's message names
                    # its own API (add_member/remove_member); memory_fact_set
                    # callers need the MCP tool names instead.
                    raise ValueError(
                        "slot holds a set; use memory_set_add / memory_set_remove"
                    ) from exc
                raise
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
            out = {"action": res.action,
                   **_cortex_record_to_dict(
                       res.record, stale_policy=self._stale_policy)}
            if res.action == "contested":
                cur = self._cortex.lookup(entity, attribute)
                out["current"] = (_cortex_record_to_dict(
                    cur, stale_policy=self._stale_policy)
                    if cur is not None else None)
            if episode_warning:
                out["episode_warning"] = "unknown or closed episode handle"
            return out

    def set_add(
        self,
        entity: str,
        attribute: str,
        member: str,
        provenance: list[str] | None = None,
        origin: str | None = None,
        confidence: float = 0.7,
    ) -> dict[str, Any]:
        """Add (or confirm) a member of the set-valued ``(entity, attribute)``
        slot. A scalar already occupying the slot converts to a set one-way
        — UNLESS that scalar is a number-led aggregate value ("32",
        "$1,500"), in which case it is protected instead: the add is parked
        as a contender (or confirms the scalar, if the member equals it) so
        the stated total survives (see
        :meth:`pseudolife_memory.memory.cortex.CortexStore.add_member`).

        The member text is embedded through the same composition scalar
        writes use (``"{entity} {attribute} {member}"``) so set membership
        shares the cortex embedding space with everything else. Persists via
        the same per-slot write-through path ``cortex_write`` uses.

        Returns ``{"action", "entity", "attribute", "member", "members_count"}``
        — ``action`` is one of ``"member_added"``, ``"member_confirmed"``,
        ``"member_capped"``, ``"member_invalid"``, ``"contested"`` (the
        aggregate-conversion guard parked the add as a contender), or
        ``"confirmed"`` (the member equalled the protected scalar) — see
        ``add_member``.
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cortex is not None
            claim = f"{entity} {attribute} {member}".strip()
            emb = self._embedder.encode_single(claim)
            writer_id, session_id = self._resolve_writer()
            res = self._cortex.add_member(
                Slot(entity, attribute, member),
                emb,
                confidence=confidence,
                provenance=provenance or (),
                support=origin,
                hlc=self._hlc.tick(),
                writer_id=writer_id,
                session_id=session_id,
            )
            self._ensure_subject_entity(entity)
            self._save_cortex()
            return {
                "action": res.action,
                "entity": entity,
                "attribute": attribute,
                "member": member,
                "members_count": len(self._cortex.members(entity, attribute)),
            }

    def set_remove(self, entity: str, attribute: str, member: str) -> dict[str, Any]:
        """Retract one current member of a set-valued slot (audit row kept,
        ``status`` -> ``"removed"``). Persists via the same per-slot
        write-through path ``cortex_write`` uses.

        Returns ``{"action", "entity", "attribute", "member", "members_count"}``
        — ``action`` is ``"member_removed"`` or ``"member_not_found"``.
        """
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            res = self._cortex.remove_member(entity, attribute, member)
            self._save_cortex()
            return {
                "action": res.action,
                "entity": entity,
                "attribute": attribute,
                "member": member,
                "members_count": len(self._cortex.members(entity, attribute)),
            }

    @property
    def _stale_policy(self) -> str:
        """The serving-side staleness policy for record render sites
        (``memory.search.stale_policy``; "annotate" = today's behavior)."""
        return getattr(self.config.memory.search, "stale_policy", "annotate")

    def _track_slot_reads(self, slots: list[tuple[str, str]]) -> None:
        """Slot read telemetry (schema v33): count each slot SERVED as an
        answer. Never raises — telemetry must not break the read it rides
        on (the retrieval-log contract). Called under ``self._lock``; the
        storage connection is shared, so an unlocked call would interleave
        transaction blocks (the 2026-08-21 wedge class)."""
        if (self._storage is None or not slots
                or not getattr(self.config.memory.cortex,
                               "read_tracking", True)):
            return
        try:
            self._storage.bump_slot_reads(slots)
        except Exception:  # noqa: BLE001
            self._slot_read_errors += 1
            logger.warning("slot-read telemetry failed", exc_info=True)

    def _alias_retry_names(self, entity: str, node: dict) -> list[str]:
        """Names to retry a slot miss under after ``entity`` itself: the
        node's canonical, then its aliases in ``find_entity``'s order —
        minus any alias that is some OTHER entity's canonical.
        ``find_entity`` resolves canonical-first, so such an alias row never
        resolves to this node and a record under that name belongs to the
        other entity (``graph_alias`` never checks the entities table, and
        a lesson about an alias used to mint one). Dropping it is the same
        drop ``graph.alias_canonical_map`` makes for the graph read
        surfaces, so lookup and attachment agree. Dedup is on the cortex
        key (``_norm_key``), the space the retry actually probes — a
        canonical whose graph norm equals the query's but whose cortex key
        differs (``host:port`` vs ``host-port``) is still worth one probe.
        Cost: one indexed query, only when the node has aliases. Caller
        holds the lock."""
        from pseudolife_memory.memory.cortex import _norm_key
        aliases = [a for a in (node.get("aliases") or ()) if a]
        shadowed = (self._storage.canonical_names_among(aliases)
                    if aliases else set())
        seen = {_norm_key(entity)}
        out: list[str] = []
        for c in (node.get("canonical"), *aliases):
            if not c or c in shadowed or _norm_key(c) in seen:
                continue
            seen.add(_norm_key(c))
            out.append(c)
        return out

    def cortex_lookup(self, entity: str, attribute: str,
                      track: bool = True) -> dict[str, Any] | None:
        """Exact slot lookup — the one ``current`` fact, or ``None``.

        ``track=False`` skips the slot-read telemetry bump — for internal
        callers whose lookup is verification, not serving (the dream
        rollback's post-revert check).

        Alias-aware in both directions: on a direct slot miss, the entity
        name is resolved through the graph's ``entity_aliases`` (Postgres)
        and the canonical name is retried, then each of the node's aliases.
        So a fact stored under ``dev-box`` surfaces when the caller queries
        the alias ``4090``, and a fact the dream wrote under ``pr-235``
        surfaces under ``PR #235`` once a merge has folded that name into
        the node — the same attachment ``graph_neighborhood`` makes, so the
        name recall shows a fact under is a name that fetches it. Order is
        direct hit, canonical, aliases (``find_entity``'s alias order), so a
        canonical-keyed record wins over an alias-keyed one at the same
        attribute; the served record keeps its OWN slot key, which is where
        its traces, contenders and ``correct_with`` live. An alias that is
        also another entity's canonical is skipped — ``find_entity`` would
        resolve it to that entity, so a record under it is theirs (see
        ``_alias_retry_names``). One ``find_entity`` call per miss plus,
        only when the node has aliases, one indexed shadow probe — never a
        per-alias storage query. Aliases are graph norms
        (``graph.norm_name``), so a record whose entity the cortex normalizer
        keeps distinct (``host:port`` vs ``host-port``) is not reached this
        way — the limit the canonical retry has always had.

        A set-valued slot has no single ``current`` record to return, so it
        takes a different shape: ``{"kind": "set", "entity", "attribute",
        "members": [...], "removed": [...]}`` (current members / removed
        audit rows). Checked only on a scalar miss, over the same name list
        (query, canonical, aliases), so a set slot under an alias is still
        found."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            rec = self._cortex.lookup(entity, attribute)
            # Retry list on a miss: the canonical, then the node's aliases
            # that find_entity would actually resolve here (see
            # _alias_retry_names) — one find_entity call plus, only when
            # the node has aliases, one indexed shadow probe.
            names = [entity]
            if rec is None and self._storage is not None:
                from pseudolife_memory.graph import norm_name
                node = self._storage.find_entity(norm_name(entity))
                if node is not None:
                    names.extend(self._alias_retry_names(entity, node))
                    for name in names[1:]:
                        rec = self._cortex.lookup(name, attribute)
                        if rec is not None:
                            break
            if rec is None:
                ra = self.config.time.relative_age
                for name in names:
                    if name and self._cortex.slot_kind(name, attribute) == "set":
                        members = self._cortex.members(name, attribute)
                        removed = [
                            r for r in self._cortex.members(
                                name, attribute, include_removed=True)
                            if r.status == "removed"
                        ]
                        sp = self._stale_policy
                        # Gate on live members: a fully-emptied set slot is
                        # routed down the miss path by callers, so serving
                        # it is not an answer and must not count.
                        if track and members:
                            from pseudolife_memory.memory.cortex import _norm_key
                            self._track_slot_reads(
                                [(_norm_key(name), _norm_key(attribute))])
                        out = {
                            "kind": "set", "entity": name, "attribute": attribute,
                            "members": [_cortex_record_to_dict(
                                r, relative_age=ra, stale_policy=sp)
                                for r in members],
                            "removed": [_cortex_record_to_dict(
                                r, relative_age=ra, stale_policy=sp)
                                for r in removed],
                        }
                        if self._storage is not None and track and members:
                            self._annotate_set_slot_evidence(
                                out, name, attribute, members)
                        return out
                return None
            d = _cortex_record_to_dict(rec, relative_age=self.config.time.relative_age,
                                       stale_policy=self._stale_policy)
            if self._storage is not None:
                from pseudolife_memory.memory.cortex import _norm_key
                d["source_entries"] = self._storage.traces_for_slot(
                    _norm_key(rec.entity), _norm_key(rec.attribute))
                # Serving only. ``track=False`` already declares the lookup a
                # verification rather than an answer — the dream rollback
                # (``service_dream._rewrite_prev``) makes one per journal row
                # and reads only ``value``, so annotating there buys a PG
                # query per row for a flag nobody reads.
                if track:
                    self._annotate_evidence_supersession([d])
            if track:
                from pseudolife_memory.memory.cortex import _norm_key
                self._track_slot_reads(
                    [(_norm_key(rec.entity), _norm_key(rec.attribute))])
            return d

    # ── retract-direction traversal (arXiv 2608.10502) ───────────────────
    # The engram cross-index has only ever been read forwards (slot -> the
    # entries that formed it). Correcting a source memory therefore left
    # every fact the dream derived from it standing as current, with nothing
    # on the served fact saying its evidence had moved. The paper's fix is
    # to traverse the provenance graph DOWNSTREAM and scope the repair —
    # not to delete the derivations (which loses benign state) and not to
    # reset the store. Here that scoping is a FLAG: cascading a correction
    # through derived facts is a review judgment, which is the same
    # two-man rule the consolidation quarantine already encodes.

    def _superseded_evidence(self, ids: set[int]) -> dict[int, float]:
        """``{entry_id: superseded_at}`` for the superseded ones among these
        source entries, scoped to the ids actually being served.

        ``entries.superseded_at`` is the single authority. An earlier draft
        also scanned the live band entries, because :meth:`consolidate`
        stamped its marks in RAM and never wrote them through. PR #239
        closed that: all three entry-level supersession sites —
        :meth:`supersede`, ``cms.store``'s contradiction decay, and
        :meth:`consolidate` — now write through inside the same locked call
        that sets the mark, so normal operation opens no window in which
        RAM and the column disagree. The scan was therefore paying an
        O(bank) pass on every annotated read to cover a state no live path
        produces, and it went. The consequence is recorded, not hidden:
        anything that leaves a mark in RAM only is a known miss for this
        flag — a future site that forgets the write-through, all three
        existing loops' ``if e.db_id is not None`` guard if an entry could
        ever reach them unpersisted, and a write-through that raises after
        the marks are set (loud, but the RAM marks are not rolled back).
        Pinned by ``test_a_mark_that_never_reached_postgres_does_not_flag``.

        No cached index — the same no-stored-state rule as
        :meth:`_cortex_change_index`. Caller holds the lock."""
        if not ids or self._storage is None:
            return {}
        return self._storage.superseded_evidence(ids)

    def _annotate_evidence_supersession(self, rows: list[dict]) -> list[dict]:
        """Read-time flag: a served fact standing on evidence that was
        corrected AFTER the fact was last confirmed gets ``re_verify`` +
        ``re_verify_reason`` — the SAME shape lessons already carry for
        "subject facts changed since"
        (:meth:`_annotate_lesson_staleness`), not a parallel one.

        The ``last_confirmed`` comparison is what makes the flag mean
        something and what makes it CLEARABLE. The cross-index is keyed on
        the slot, so ``source_entries`` lists every entry that ever formed
        it across the slot's whole supersession history, and trace rows are
        never deleted. A bare "any source superseded" test would therefore
        latch on forever — including on slots whose current value was
        asserted long after the retracted contributor, which on a mature
        bank is a large fraction of the cortex. Worse, it would latch while
        routing into ``correct_with``, whose served note tells the reader to
        write a correction NOW: an unclearable flag there is a standing
        instruction to rewrite a quarter of the cortex, every session.
        Keying on ``last_confirmed`` means the documented remedy —
        re-assert the slot, which confirms it and moves the clock — is
        exactly what silences it.

        The keys are ABSENT on unaffected facts, keeping their payloads
        byte-identical (the ``stance`` precedent in
        ``_cortex_record_to_dict``).

        BEST-EFFORT, and deliberately so. The flag is derived at read time
        from evidence that still exists, so LOSING the evidence loses the
        flag: ``memory_traces.entry_id`` is ``ON DELETE CASCADE``, a
        true-drop capacity eviction hard-deletes the entry row (every
        eviction under the default flat preset), and a superseded entry is
        the top eviction candidate because contradiction decay multiplies
        its surprise by 0.3. So a flag can appear and later vanish with no
        re-verification having happened, and ``memory_delete`` — the
        strongest retraction of all — raises no flag at any point. Making
        the caution outlive its evidence needs durable per-slot state, i.e.
        a schema change, which is out of scope here; both behaviours are
        pinned by tests so the limit is a recorded contract rather than a
        surprise. Caller holds the lock."""
        if not self.config.memory.traces.enabled:
            return rows
        cited = {i for r in rows for i in (r.get("source_entries") or [])}
        if not cited:
            return rows                 # nothing to traverse — skip the scan
        dead = self._superseded_evidence(cited)
        if not dead:
            return rows
        for row in rows:
            # Fall back to asserted_at only when the record carries no
            # confirmation stamp at all (legacy rows); never to 0.0, which
            # would re-open the latch this comparison exists to close.
            seen = row.get("last_confirmed") or row.get("asserted_at")
            if not seen:
                continue
            hit = [i for i in (row.get("source_entries") or [])
                   if (ts := dead.get(i)) is not None and ts > seen]
            if not hit:
                continue
            row["re_verify"] = True
            row["re_verify_reason"] = (
                f"derived from {len(hit)} source "
                f"{'memory' if len(hit) == 1 else 'memories'} "
                "corrected since this fact was last confirmed")
        return rows

    def _annotate_set_slot_evidence(self, out: dict, entity: str,
                                    attribute: str, members: list) -> None:
        """Carry the flag onto a set-valued slot's payload.

        A set slot is served as ONE grouped dict with no scalar record
        behind it, so this lookup payload carried neither the
        ``source_entries`` the scalar path fetches nor a confirmation stamp
        (``cortex_search``'s grouped entry has carried ``last_confirmed``
        since the Task-6 review; it lacked only the traces). Either way no
        set slot could carry ``re_verify``, while ``slots_for_entries``
        (kind-agnostic) named set slots in ``derived_flagged`` and
        ``memory_fact_get`` promised the flag without qualification.

        The cross-index is keyed on the SLOT, not the member, so one
        ``traces_for_slot`` answers for the whole set. The slot's
        confirmation stamp is the newest member's, which makes the flag
        clearable the way the scalar path's ``last_confirmed`` does — but
        bluntly: ADDING a member also stamps the slot, so an unrelated add
        silences the caution for members nobody re-checked. Accepted rather
        than keyed to confirmations only, because a set slot is a single
        served answer and a per-member flag on a grouped payload has
        nowhere to render. ``_annotate_recalled_facts``, where members ARE
        served individually, matches per member instead. Only the flag keys
        are merged out, so the set payload does not otherwise change shape.
        Gated on ``traces.enabled`` before the query, not after: unlike the
        scalar path — which SERVES its traces as ``source_entries`` and so
        fetches them either way — this lookup is purely feeding the
        annotation and discards the result, so with the cross-index off the
        query is pure waste. ``test_flag_off_when_the_cross_index_is_disabled``
        states the rule it would break: "the read surface must not pay for
        one". Caller holds the lock."""
        if not self.config.memory.traces.enabled:
            return
        from pseudolife_memory.memory.cortex import _norm_key
        probe = {
            "source_entries": self._storage.traces_for_slot(
                _norm_key(entity), _norm_key(attribute)),
            # ``or m.asserted_at`` is the same legacy fallback the scalar
            # path applies, taken per member so one unstamped member cannot
            # drag the slot's clock back to nothing.
            "last_confirmed": max(
                (s for m in members
                 if (s := m.last_confirmed or m.asserted_at)),
                default=None),
        }
        self._annotate_evidence_supersession([probe])
        if probe.get("re_verify"):
            out["re_verify"] = True
            out["re_verify_reason"] = probe["re_verify_reason"]

    def _derived_from_entries_locked(self, entry_ids) -> list[dict]:
        """Slots these source entries helped form, in display vocabulary.
        Caller holds the lock; :meth:`derived_from_entries` is the public,
        self-locking form.

        CURRENT records supply the display naming and the rest of the store
        only fills gaps. ``self._cortex.records`` holds every version of
        every slot, so a ``setdefault`` over it alone names each slot after
        whichever version happens to sit oldest — a slot written as
        "Payments DB / Host" and re-asserted as "payments-db / host" comes
        back under the name the reader can no longer look up.

        And a trace row outlives the fact it formed (the cross-index is
        slot-keyed, rows are never deleted, and a slot can end up with no
        current value at all), so slots are MARKED with
        ``has_current_value`` rather than filtered: a dead slot is still
        blast radius worth seeing, it is just not a fact to go re-check.

        Ordered live slots first, then by normalized key. The order is what
        ``supersede``'s cap slices, and alphabetical order alone would let a
        correction touching many dead slots spend the whole cap on rows
        nobody can act on while truncating away the live ones. Note the
        rows are SORTED on the normalized key but RENDERED with display
        names, so the result can look unsorted to a reader."""
        ids = [int(i) for i in (entry_ids or []) if i is not None]
        if (not ids or self._storage is None
                or not self.config.memory.traces.enabled):
            return []
        from pseudolife_memory.memory.cortex import _norm_key
        by_slot: dict[tuple[str, str], list[int]] = {}
        for r in self._storage.slots_for_entries(ids):
            by_slot.setdefault(
                (r["entity_norm"], r["attribute_norm"]), []).append(r["entry_id"])
        disp: dict[tuple[str, str], tuple[str, str]] = {}
        live: set[tuple[str, str]] = set()
        if self._cortex is not None:
            for rec in self._cortex.current_records():
                key = (_norm_key(rec.entity), _norm_key(rec.attribute))
                disp.setdefault(key, (rec.entity, rec.attribute))
                live.add(key)
            for rec in self._cortex.records:
                disp.setdefault(
                    (_norm_key(rec.entity), _norm_key(rec.attribute)),
                    (rec.entity, rec.attribute))
        return [{"entity": disp.get(k, k)[0], "attribute": disp.get(k, k)[1],
                 "has_current_value": k in live,
                 "source_entry_ids": sorted(set(src))}
                for k, src in sorted(by_slot.items(),
                                     key=lambda kv: (kv[0] not in live, kv[0]))]

    def derived_from_entries(self, entry_ids) -> dict[str, Any]:
        """What the dream derived FROM these source memories — the retract
        read of the engram cross-index. The blast radius of correcting or
        removing an entry, reported so a human can scope the repair; nothing
        here mutates anything."""
        with self._lock:
            self._ensure_init()
            facts = self._derived_from_entries_locked(entry_ids)
        return {"count": len(facts), "facts": facts}

    def cortex_contenders(self, entity: str, attribute: str) -> dict[str, Any]:
        """Active contenders parked at a slot — a conflicting lower-tier / below-
        margin value that did NOT supersede the current fact (0 or 1)."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            recs = self._cortex.contenders_for(entity, attribute)
            return {
                "entity": entity, "attribute": attribute,
                "contenders": [_cortex_record_to_dict(
                    r, stale_policy=self._stale_policy) for r in recs],
            }

    def cortex_candidates(self, entity: str, attribute: str,
                          top_k: int = 5) -> list[dict]:
        """Ranked nearby slots for an empty-slot lookup (see
        ``CortexStore.candidates_for``). Alias-aware: same-entity candidates
        are collected for both the queried name and its canonical graph
        alias. Degrades to same-entity-only when the embedder is absent."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            names = [entity]
            if self._storage is not None:
                from pseudolife_memory.graph import norm_name
                node = self._storage.find_entity(norm_name(entity))
                if node is not None:
                    canon = node.get("canonical")
                    if canon and norm_name(canon) != norm_name(entity):
                        names.append(canon)
            emb = None
            if self._embedder is not None:
                emb = self._embedder.encode_single(f"{entity} {attribute}")
            out: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for name in names:
                for c in self._cortex.candidates_for(
                        name, attribute, emb, top_k=top_k):
                    k = (c["entity"].lower(), c["attribute"].lower())
                    if k not in seen:
                        seen.add(k)
                        out.append(c)
            out.sort(key=lambda c: (c["why"] != "same_entity",
                                    -(c["score"] or 1.0)))
            return out[:top_k]

    def cortex_resolve(self, entity: str, attribute: str, accept: bool,
                       support: str = "user") -> dict[str, Any]:
        """Promote (accept) or retire (reject) the active contender at a slot.
        Persists. Returns ``{"resolved": False, "reason": "no_contender"}`` when
        there is nothing parked to resolve, and ``{"resolved": False,
        "reason": "slot_holds_set"}`` when the slot was converted to a set
        (``memory_set_add``) after the contender was parked against the
        scalar it used to hold — resolve it via ``memory_set_add`` /
        ``memory_set_remove`` instead.

        ``support`` (internal): the tier stamped on an accepted contender —
        "user" for the MCP tool path; the consolidation quarantine passes
        "agent" so an automated promotion never reads as a human act."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            # A fresh HLC tick on accept: promotion is an ordering event, and
            # the promoted fact must outrank any pre-promotion stamp (same
            # failure class as the 2026-07-02 _promote_slots fix). Rejection
            # retires without competing, so it needs no tick.
            res = self._cortex.resolve(entity, attribute, accept,
                                       support=support,
                                       hlc=self._hlc.tick() if accept else None)
            if res is None:
                return {"resolved": False, "reason": "no_contender",
                        "entity": entity, "attribute": attribute}
            if res.action == "refused":
                return {"resolved": False, "reason": "slot_holds_set",
                        "entity": entity, "attribute": attribute}
            self._save_cortex()
            cur = self._cortex.lookup(entity, attribute)
            return {
                "resolved": True,
                "accepted": bool(accept),
                "action": res.action,
                "current": (_cortex_record_to_dict(
                    cur, stale_policy=self._stale_policy)
                    if cur is not None else None),
                "record": _cortex_record_to_dict(
                    res.record, stale_policy=self._stale_policy),
            }

    def cortex_search(
        self, query: str, top_k: int = 5, min_score: float = 0.0,
        bm25: bool | None = None,
    ) -> dict[str, Any]:
        """Fuzzy search over ``current`` canonical facts only. Each entry is
        flagged ``"contested": bool`` and, when true, carries
        ``"contender_value"`` / ``"contender_origin"`` for the parked rival, so a
        discrepancy is visible during normal recall.

        ``bm25`` is the same tri-state override as ``memory_search``, but
        facts read their own default (``memory.bm25.cortex_enabled``,
        ships **off** — the 2026-07-30 pre-registered A/B measured no
        end-to-end benefit): a lexical pool over the
        composed fact text (``entity — attribute: value``) is fused with
        the dense cosine hits exactly as the turn pool does it —
        ``score + weight * normalized_bm25`` for facts in both pools,
        ``weight * normalized_bm25`` for lexical-only facts. Lexical hits
        are gated by the *normalised* ``bm25.min_score``, deliberately NOT
        by the caller's dense ``min_score`` floor: a fact the embedder
        scores below the floor must still be servable when the query names
        it exactly (the dense-only channel starved identifier-style
        queries — the 2026-07-30 ceiling-e2e diagnosis).
        """
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._cortex is not None
            emb = self._embedder.encode_query(query)
            hits = self._cortex.search(emb, top_k=top_k, min_score=min_score)
            bm25_cfg = getattr(self.config.memory, "bm25", None)
            # Facts read their own switch (`cortex_enabled`, ships False —
            # the 2026-07-30 A/B showed no end-to-end benefit), NOT the
            # turn pool's `enabled`.
            bm25_enabled = (
                bm25 if bm25 is not None
                else bool(bm25_cfg
                          and getattr(bm25_cfg, "cortex_enabled", False)))
            if bm25_enabled and bm25_cfg is not None and query:
                hits = self._cortex_bm25_fuse(query, hits, bm25_cfg, top_k)
            # Set-valued slots (Task 6): every member of a set is a distinct
            # ``current`` record, so it ranks in ``hits`` on its own — left
            # alone, a slot with N members would surface as N separate
            # entries. Group AFTER fusion (fusion stays per-record, exactly
            # as it ran above) by folding each member hit into its slot,
            # keyed on first occurrence: ``hits`` is already score-descending
            # (both ``search`` and ``_cortex_bm25_fuse`` sort before
            # returning), so the first member of a slot encountered here
            # already carries that slot's max score, and the group's
            # position in the final ordering is exactly where that top
            # member would have sorted.
            groups: dict[tuple[str, str], dict[str, Any]] = {}
            order: list[tuple[str, Any]] = []
            for r, s in hits:
                if r.kind == "member":
                    key = r.key
                    grp = groups.get(key)
                    if grp is None:
                        grp = {"entity": r.entity, "attribute": r.attribute,
                               "ranked": []}
                        groups[key] = grp
                        order.append(("set", key))
                    grp["ranked"].append((r, s))
                else:
                    order.append(("scalar", (r, s)))
            entries = []
            for tag, payload in order:
                if tag == "scalar":
                    r, s = payload
                    d = {**_cortex_record_to_dict(
                        r, stale_policy=self._stale_policy),
                        "score": round(float(s), 4)}
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
                else:
                    from pseudolife_memory.memory.cortex import compose_set_value
                    key = payload
                    grp = groups[key]
                    entity, attribute = grp["entity"], grp["attribute"]
                    all_members = self._cortex.members(entity, attribute)
                    id_to_idx = {id(m): i for i, m in enumerate(all_members)}
                    ranked_pairs = [(id_to_idx[id(r)], s) for r, s in grp["ranked"]
                                     if id(r) in id_to_idx]
                    value, score = compose_set_value(
                        [m.value for m in all_members], ranked_pairs)
                    # F5/re-review (Task 6 review): a set entry carried no
                    # timestamp at all, so mcp_server.py's cortex-first
                    # block (`_iso_seconds(f.get(...))` / `f.get("age")`)
                    # always rendered blank for it. One shared "anchor" —
                    # the most recent per-member activity, same
                    # tx_time-preferred-over-asserted_at priority
                    # `_cortex_record_to_dict` uses for a scalar's "age" —
                    # backs both "asserted_at" (raw float, exactly how
                    # `_cortex_record_to_dict` renders a scalar's
                    # "asserted_at": no ISO formatting at this layer, that
                    # happens downstream in mcp_server._iso_seconds) and
                    # "age" (the human string, via the same _relative_time
                    # helper `_cortex_record_to_dict` uses).
                    anchor = max(
                        ((m.tx_time or m.asserted_at) for m in all_members),
                        default=None)
                    # The cross-index is slot-keyed, so one lookup answers for
                    # the whole set — the same per-row cost the scalar branch
                    # above already pays, and without it the grouped entry is
                    # the one served fact shape that can never carry
                    # ``re_verify`` (the annotation below reads this key).
                    # Ungated on ``traces.enabled`` to match that branch:
                    # ``source_entries`` is served provenance in its own
                    # right, and the knob gates the FLAG, not the display.
                    set_traces = []
                    if self._storage is not None:
                        from pseudolife_memory.memory.cortex import _norm_key
                        set_traces = self._storage.traces_for_slot(
                            _norm_key(entity), _norm_key(attribute))
                    entries.append({
                        "kind": "set",
                        "entity": entity,
                        "attribute": attribute,
                        "value": value,
                        "source_entries": set_traces,
                        "members": [_cortex_record_to_dict(
                            m, stale_policy=self._stale_policy)
                            for m in all_members],
                        "score": round(float(score), 4) if score is not None else 0.0,
                        "contested": False,
                        "last_confirmed": max(
                            (m.last_confirmed for m in all_members), default=None),
                        "asserted_at": anchor,
                        "age": _relative_time(anchor) if anchor else None,
                    })
            _demote_stale(entries, self._stale_policy)
            self._annotate_evidence_supersession(entries)
            from pseudolife_memory.memory.cortex import _norm_key
            self._track_slot_reads(sorted({
                (_norm_key(e["entity"]), _norm_key(e["attribute"]))
                for e in entries}))
            return {"count": len(entries), "entries": entries}

    def _cortex_bm25_fuse(self, query, hits, cfg, top_k):
        """Fuse dense cortex hits with a lexical pool over composed fact
        text. Mirrors the turn pool's fusion (memory/bm25.py): boost facts
        in both pools, inject lexical-only facts at ``weight * norm``.
        Called under ``self._lock`` with the store initialised.

        Keyed by ``id(record)``, NOT ``record.key`` (the slot identity):
        a set-valued slot can have many current members sharing one slot
        key, and fusion must stay genuinely per-record (Task 6 review
        finding F1) — keying by slot key collapsed every member of a slot
        onto whichever one the lexical-score dict comprehension happened
        to keep last, so the rest silently inherited its score and could
        never be lexical-only-injected on their own. Set-level grouping
        happens strictly AFTER this call, in ``cortex_search``."""
        from types import SimpleNamespace

        from pseudolife_memory.memory.bm25 import BM25Index, normalize_scores

        docs = [
            SimpleNamespace(
                text=f"{r.entity} — {r.attribute}: {r.value}", record=r)
            for r in self._cortex.current_records()
        ]
        if not docs:
            return hits
        idx = BM25Index(docs, k1=cfg.k1, b=cfg.b)
        norm_hits = normalize_scores(idx.score(query, top_k=cfg.top_n))
        lex = {id(d.record): (d.record, s) for d, s in norm_hits
               if s >= cfg.min_score}
        fused = []
        seen = set()
        for r, s in hits:
            boost = lex.get(id(r))
            if boost is not None:
                s = s + cfg.weight * boost[1]
            fused.append((r, s))
            seen.add(id(r))
        for rid, (r, s) in lex.items():
            if rid not in seen:
                fused.append((r, cfg.weight * s))
        fused.sort(key=lambda rs: rs[1], reverse=True)
        return fused[: max(0, int(top_k))]

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
            return {"action": action, **_world_record_to_dict(
                rec, stale_policy=self._stale_policy)}

    def world_lookup(self, entity: str, attribute: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_init()
            assert self._world is not None
            rec = self._world.lookup(entity, attribute)
            return (_world_record_to_dict(rec, stale_policy=self._stale_policy)
                    if rec is not None else None)

    def world_search(self, query: str, top_k: int = 5, min_score: float = 0.0) -> dict[str, Any]:
        """Fuzzy search over current world facts; entries carry decayed
        effective_confidence + stale flag + citation."""
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._world is not None
            emb = self._embedder.encode_query(query)
            hits = self._world.search(emb, top_k=top_k, min_score=min_score)
            entries = [{**_world_record_to_dict(
                            r, stale_policy=self._stale_policy),
                        "score": round(float(s), 4)}
                       for r, s in hits]
            _demote_stale(entries, self._stale_policy)
            return {"count": len(entries), "entries": entries}

    def world_dump(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            assert self._world is not None
            rows = [_world_record_to_dict(r, stale_policy=self._stale_policy)
                    for r in self._world.current_records()]
            rows.sort(key=lambda d: (d["entity"].lower(), d["attribute"].lower()))
            _demote_stale(rows, self._stale_policy)
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
                       origin: str = "action",
                       episode: str | None = None) -> dict[str, Any]:
        """Record a cheap in-session outcome signal (success | failure |
        correction). Single-writer: this never writes a lesson — the dream
        synthesises lessons from the accumulated signals.

        ``episode`` (identity tier 2): an open episode id or unambiguous
        prefix (>=8 chars) — attributes this signal to that episode instead
        of the global current one. An unknown/closed/ambiguous handle
        degrades silently: the signal is still recorded, and
        ``"episode_warning"`` is added to the result."""
        # Refuse — never coerce — an unknown outcome: silently mapping e.g.
        # "failed" to "success" would invert a dead-end into a do-this lesson.
        if outcome not in ("success", "failure", "correction"):
            return {"recorded": False, "reason": "unknown_outcome",
                    "outcomes": ["success", "failure", "correction"]}
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"recorded": False, "reason": "signals require Postgres storage"}
            if not self.config.memory.lessons.enabled:
                return {"recorded": False, "reason": "lessons disabled"}
            resolved = self._resolve_episode_handle(episode)
            episode_warning = bool(episode) and resolved is None
            episode_id = resolved[0] if resolved is not None else self._current_episode_id()
            sid = self._storage.add_signal(
                task=task, outcome=outcome, about=about, detail=detail,
                polarity=polarity, origin=origin,
                episode_id=episode_id)
            out = {"recorded": True, "signal_id": sid, "task": task, "outcome": outcome}
            if episode_warning:
                out["episode_warning"] = "unknown or closed episode handle"
            return out

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
        tn = norm_name(task)
        if not tn:
            return
        # Resolve through the alias table before upserting by canonical:
        # ensure_entity alone minted a fresh canonical for a name that was
        # already an alias (a lesson about "pr-235" after that node was
        # folded into "PR #235"), and that shadow then out-resolved the
        # alias everywhere find_entity is consulted — the graph detached
        # the surviving node's alias-keyed facts while cortex_lookup's
        # retry, before it learned to skip shadowed aliases, kept serving
        # them. Same alias-aware find-then-create every other write uses.
        tid = self._resolve_or_create_entity(task, etype="task-type")["id"]
        if not about:
            return
        an = norm_name(about)
        if not an or an == tn:
            return
        oid = self._resolve_or_create_entity(about)["id"]
        relation = "avoids" if polarity == "-" else "prefers"
        self._graph.upsert_edge(tid, relation, oid, confidence=0.7, origin="action")

    def retype_quarantined_links(self, extractor, limit: int = 3) -> dict[str, Any]:
        """Second pass over quarantined UNTYPED edges: re-ask the extractor for
        a typed relation, using only the notes where both entities co-occur.

        The quarantine holds ``related-to`` co-mention edges at 0.45. A triage
        of 32 of them found 0 worth writing as-is, but ~44% named a REAL
        relationship that merely got the wrong label (``publishes-to``,
        ``implements``, ``operates-on``) — value that otherwise just
        accumulates. Focused evidence plus the current prompt (which demands
        the most specific relation and forbids ``related-to`` for co-mentions)
        makes this a genuinely different question from the one that produced
        the quarantined edge.

        A typed answer files a REVIEWABLE ``dream-retyped`` proposal — never a
        live edge, because a retype is a second guess on already-suspect
        material — and settles the untyped original as rejected. No typed
        answer settles the original too: that is the co-mention noise the
        quarantine was built to catch, and leaving it pending only regrows the
        queue. Extractor failure settles NOTHING and never raises (the pass is
        best-effort, like lessons). Returns
        ``{"considered", "retyped", "settled"}``."""
        import time as _t
        from pseudolife_memory.memory import graph_consolidation as gc
        from pseudolife_memory.memory.relation_quality import edge_confidence
        from pseudolife_memory import graph as G

        rel_fn = getattr(extractor, "extract_relations", None)
        cap = max(0, int(limit))
        if rel_fn is None or not cap:
            return {"considered": 0, "retyped": 0, "settled": 0}
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"considered": 0, "retyped": 0, "settled": 0}
            pending = [p for p in self._storage.pending_proposals()
                       if p.get("source") == "dream-low-confidence"][:cap]
            if not pending:
                return {"considered": 0, "retyped": 0, "settled": 0}
            entries = self._storage.load_entries()
            known = [(r["name"], r["description"])
                     for r in self._graph.load_relations()
                     if r["name"] not in ("prefers", "avoids")]
            names = [r["name"] for r in self._graph.load_relations()
                     if r["name"] not in ("prefers", "avoids")]
        retyped = settled = 0
        for p in pending:
            texts = gc.shared_mention_entries(entries, p["src"], p["dst"])
            if not texts:
                continue                     # no evidence: leave it pending
            try:                             # unlocked: extractor call
                found = rel_fn(texts, known)
            except Exception as exc:  # noqa: BLE001 — best-effort, never break
                logger.warning("retype pass halted (%s); %d settled so far",
                               exc, settled)
                break
            want = {G.norm_name(p["src"]), G.norm_name(p["dst"])}
            typed = None
            for r in found or []:
                if {G.norm_name(str(r.get("src", ""))),
                        G.norm_name(str(r.get("dst", "")))} != want:
                    continue
                resolved, _ = G.resolve_relation(names, str(r.get("relation", "")))
                if resolved and resolved != "related-to":
                    typed = (str(r.get("src")), resolved, str(r.get("dst")))
                    break
            with self._lock:
                if typed:
                    src_e = self._resolve_or_create_entity(typed[0])
                    dst_e = self._resolve_or_create_entity(typed[2])
                    conf = edge_confidence(typed[0], typed[1], typed[2])
                    self._storage.insert_proposal(
                        src_e["id"], typed[1], dst_e["id"], conf, None,
                        f"retyped from related-to on {len(texts)} shared note(s)",
                        "dream-retyped", _t.time())
                    retyped += 1
                self._storage.set_proposal_status(p["id"], "rejected")
            settled += 1
        return {"considered": len(pending), "retyped": retyped, "settled": settled}

    def lesson_search(self, query: str, top_k: int | None = None,
                      min_score: float = 0.0) -> dict[str, Any]:
        """Embedding-on-query retrieval over current lessons (mirrors world_search).
        Returns lessons with polarity/outcome so a caller can surface dead-ends."""
        with self._lock:
            self._ensure_init()
            assert self._embedder is not None and self._lessons is not None
            k = int(top_k if top_k is not None else self.config.memory.lessons.top_k)
            floor = max(float(min_score), float(self.config.memory.lessons.min_confidence))
            emb = self._embedder.encode_query(query)
            hits = self._lessons.search(emb, top_k=k, min_score=floor)
            entries = [{**_lesson_record_to_dict(r), "score": round(float(s), 4)}
                       for r, s in hits]
            self._annotate_lesson_staleness(entries)
            return {"count": len(entries), "entries": entries}

    def lessons_dump(self, limit: int = 120) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            assert self._lessons is not None
            rows = [_lesson_record_to_dict(r) for r in self._lessons.current_records()]
            rows.sort(key=lambda d: (d["task"].lower(), d["aspect"].lower()))
            rows = rows[: max(0, int(limit))]
            self._annotate_lesson_staleness(rows)
            return {"count": len(rows), "entries": rows}

    def loop_health(self, window_days: int = 7,
                    now: float | None = None) -> dict[str, Any]:
        """Is the memory loop actually being exercised? Windowed activity
        counts + per-session rates for the Console tile. Needs Postgres —
        ``{"available": False}`` without (never raises)."""
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"available": False}
            h = self._storage.loop_health(
                window_s=float(window_days) * 86400.0, now=now)
        sessions = h.get("sessions") or 0

        def _rate(n: int) -> float | None:
            return round(n / sessions, 2) if sessions else None

        return {"available": True, "window_days": int(window_days), **h,
                "stores_per_session": _rate(h["stores"]["current"]),
                "outcomes_per_session": _rate(h["outcomes"]["current"])}

    def _cortex_change_index(self) -> dict[str, float]:
        """norm-entity → latest cortex change ts (assertions + supersessions,
        over ALL records incl. superseded) — the churn signal behind lesson
        ``re_verify``. Caller holds the lock."""
        from pseudolife_memory.memory.cortex import _norm_key
        idx: dict[str, float] = {}
        if self._cortex is None:
            return idx
        for r in self._cortex.records:
            ts = max(r.asserted_at, r.superseded_at or 0.0)
            k = _norm_key(r.entity)
            if ts > idx.get(k, 0.0):
                idx[k] = ts
        return idx

    def _annotate_lesson_staleness(self, rows: list[dict]) -> list[dict]:
        """Read-time staleness: a lesson whose ``about`` (fallback: task)
        resolves to a cortex entity whose facts changed AFTER the lesson was
        asserted/confirmed gets ``re_verify: True`` + ``re_verify_reason``.
        No stored state — re-confirming the lesson clears the flag. Any
        resolution failure leaves the row unflagged. Caller holds the lock."""
        if not rows:
            return rows
        from pseudolife_memory.memory.cortex import _norm_key
        idx = self._cortex_change_index()
        if not idx:
            return rows
        for row in rows:
            name = (row.get("about") or row.get("task") or "").strip()
            if not name:
                continue
            k = _norm_key(name)
            if k not in idx and self._storage is not None:
                from pseudolife_memory.graph import norm_name
                node = self._storage.find_entity(norm_name(name))
                if node is not None:
                    k = _norm_key(node.get("canonical") or "")
            changed = idx.get(k)
            seen = max(row.get("asserted_at") or 0.0,
                       row.get("last_confirmed") or 0.0)
            if changed and seen and changed > seen:
                row["re_verify"] = True
                row["re_verify_reason"] = (
                    f"facts about {name} changed since this lesson")
        return rows

    def lesson_forget(self, task: str, aspect: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_init()
            assert self._lessons is not None
            removed = self._lessons.forget(task, aspect)
            if removed:
                self._save_lessons()
            return {"removed": removed, "task": task, "aspect": aspect}

    def _synthesized_lesson_duplicate(self, task: str, aspect: str,
                                      lesson: str, polarity: str,
                                      threshold: float) -> bool:
        """Cross-key near-duplicate gate for SYNTHESIZED lessons only —
        explicit ``lesson_write`` callers are never gated. True when an
        existing CURRENT lesson at a DIFFERENT ``(task, aspect)`` key with
        the SAME polarity sits at/above ``threshold`` cosine (the store's
        own search metric). Same-key hits pass through: supersession is the
        store's job. Opposite-polarity hits pass through: an "avoid"
        inversion of a "do" lesson is new information, never a duplicate."""
        from pseudolife_memory.memory.cortex import _norm_key
        with self._lock:
            self._ensure_init()
            if self._lessons is None or self._embedder is None:
                return False
            key = (_norm_key(task), _norm_key(aspect))
            emb = self._embedder.encode_single(
                f"{task} {aspect} {lesson}".strip())
            for rec, _score in self._lessons.search(emb, top_k=3,
                                                    min_score=threshold):
                if rec.key != key and rec.polarity == polarity:
                    return True
        return False

    def cortex_dump(self) -> dict[str, Any]:
        """All current canonical facts (entity, attribute, value, origin, …) for
        introspection / cleanup. Sorted by (entity, attribute)."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            ra = self.config.time.relative_age
            rows = [_cortex_record_to_dict(r, relative_age=ra,
                                           stale_policy=self._stale_policy)
                    for r in self._cortex.current_records()]
            rows.sort(key=lambda d: (d["entity"].lower(), d["attribute"].lower()))
            _demote_stale(rows, self._stale_policy)
            if self._storage is not None:
                from pseudolife_memory.graph import norm_name
                from pseudolife_memory.memory.cortex import _norm_key
                emap = self._storage.entity_id_map()
                for d in rows:
                    d["entity_id"] = emap.get(norm_name(d["entity"]))
                    d["source_entries"] = self._storage.traces_for_slot(
                        _norm_key(d["entity"]), _norm_key(d["attribute"]))
                self._annotate_evidence_supersession(rows)
            return {"count": len(rows), "entries": rows}

    def compact_superseded(self) -> dict[str, Any]:
        """Purge old superseded/retired versions from the three canonical
        stores (spec 2026-07-14): per slot keep the newest
        ``memory.compaction.keep_per_slot`` non-live records, purge the rest
        once older than ``min_age_days``. current/contested are never
        touched; the per-slot sync deletes the purged rows from PG. Runs on
        the dream sweep tick; safe to call any time."""
        from pseudolife_memory.memory.compaction import compact_store

        cfg = self.config.memory.compaction
        if not cfg.enabled:
            return {"facts": 0, "world_facts": 0, "lessons": 0, "total": 0,
                    "skipped": "disabled"}
        with self._lock:
            self._ensure_init()
            kw = dict(keep_per_slot=cfg.keep_per_slot,
                      min_age_days=cfg.min_age_days)
            out = {"facts": 0, "world_facts": 0, "lessons": 0}
            if self._cortex is not None:
                out["facts"] = compact_store(self._cortex, **kw)
                if out["facts"]:
                    self._save_cortex()
            if getattr(self, "_world", None) is not None:
                out["world_facts"] = compact_store(self._world, **kw)
                if out["world_facts"]:
                    self._save_world()
            if getattr(self, "_lessons", None) is not None:
                out["lessons"] = compact_store(self._lessons, **kw)
                if out["lessons"]:
                    self._save_lessons()
            out["total"] = sum(out.values())
            if out["total"]:
                logger.info("compaction purged %s", out)
            return out

    @staticmethod
    def _parse_as_of(as_of: str | float | None) -> float | None:
        """ISO datetime or epoch seconds → epoch seconds (None passes
        through). A naive ISO string is read in local time."""
        if as_of is None:
            return None
        try:
            return float(as_of)
        except (TypeError, ValueError):
            pass
        from datetime import datetime
        return datetime.fromisoformat(str(as_of)).timestamp()

    def history(self, entity: str, attribute: str,
                as_of: str | float | None = None) -> dict[str, Any]:
        """The version timeline at a ``(entity, attribute)`` slot — current +
        superseded records, oldest→newest by tx_time, each attributed
        (writer_id / session_id) with its temporal stamp. The agent's "how did
        this fact change, and who changed it?" view (v0.4 T8).

        ``as_of`` (ISO or epoch) filters to versions whose transaction time
        is at or before that instant — a per-slot point-in-time read. Honest
        limitation: superseded-row compaction keeps only the newest
        ``memory.compaction.keep_per_slot`` non-live versions past
        ``min_age_days``, so an ``as_of`` older than that window may return
        an incomplete chain.

        A set-valued slot has no single supersession chain — each member has
        its own add/remove lifecycle — so it takes a flatter shape instead:
        ``{"kind": "set", "entity", "attribute", "versions": [{"value",
        "event": "added"|"removed", "at"}, ...]}``, time-ordered across all
        members. A still-current member contributes one ``"added"`` event; a
        removed member contributes both its original ``"added"`` and the
        later ``"removed"``."""
        cutoff = self._parse_as_of(as_of)
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            if self._cortex.slot_kind(entity, attribute) == "set":
                # The list is built member-by-member, so only the sort
                # restores chronology. Timestamps can tie (2026-08-06
                # full-suite flake: a removal sorted ahead of a later
                # member's add), so break ties adds-before-removes, then
                # by member insertion order — the only deterministic
                # order the store can still attest to at equal clocks.
                versions: list[dict[str, Any]] = []
                for idx, r in enumerate(
                        self._cortex.members(entity, attribute,
                                             include_removed=True)):
                    versions.append(
                        {"value": r.value, "event": "added",
                         "at": r.asserted_at, "_tie": (0, idx)})
                    if r.status == "removed" and r.superseded_at is not None:
                        versions.append(
                            {"value": r.value, "event": "removed",
                             "at": r.superseded_at, "_tie": (1, idx)})
                if cutoff is not None:
                    versions = [v for v in versions if (v["at"] or 0) <= cutoff]
                versions.sort(key=lambda v: ((v["at"] or 0), v["_tie"]))
                for v in versions:
                    del v["_tie"]
                out = {
                    "kind": "set", "entity": entity, "attribute": attribute,
                    "count": len(versions), "versions": versions,
                }
                if cutoff is not None:
                    out["as_of"] = cutoff
                return out
            ra = self.config.time.relative_age
            recs = self._cortex.records_for(entity, attribute)
            if cutoff is not None:
                recs = [r for r in recs
                        if (r.tx_time or r.asserted_at) <= cutoff]
            recs = sorted(recs, key=lambda r: (r.tx_time or r.asserted_at))
            out = {
                "entity": entity, "attribute": attribute, "count": len(recs),
                # Deliberately NOT policy-rendered (stale_policy stays at its
                # "annotate" default here): the version chain is the audit
                # surface — and the recovery path when a current value is
                # quarantined — so it must always show what was actually
                # stored.
                "versions": [_cortex_record_to_dict(r, relative_age=ra)
                             for r in recs],
            }
            if cutoff is not None:
                out["as_of"] = cutoff
            return out

    def chain(self, entity: str, limit: int = 20) -> dict[str, Any]:
        """Causal chain — "what led to X": dated events about an entity,
        merged from four streams (canonical fact assertions + supersessions,
        source entries, graph edges, lessons) and sorted oldest→newest.
        Alias-aware in both directions — the fact and lesson streams key on
        the queried name, its canonical AND the node's aliases, so slot
        history written under a name a later merge folded into the node is
        the node's history too. Streams degrade independently (no graph
        node → facts + lessons only). Returns ``{found, entity, count,
        events}`` with events ``{t, kind:
        fact_set|superseded|entry|edge|lesson, summary, refs}``."""
        from pseudolife_memory.memory.cortex import _norm_key
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None
            name = (entity or "").strip()
            if not name:
                return {"found": False, "entity": entity, "count": 0, "events": []}
            keys = {_norm_key(name)}
            display = name
            node = None
            if self._storage is not None:
                from pseudolife_memory.graph import norm_name
                node = self._storage.find_entity(norm_name(name))
                if node is not None:
                    display = node["display"]
                    # Canonical plus the aliases find_entity resolves to
                    # this node (a merge folds the absorbed canonical into
                    # them without rewriting the records written under it);
                    # an alias shadowed by another entity's canonical is
                    # that entity's history, not this one's.
                    keys.update(_norm_key(n) for n in
                                self._alias_retry_names(name, node))
            events: list[dict] = []
            # 1. canonical fact history — assertions and supersessions.
            for r in self._cortex.records:
                if _norm_key(r.entity) not in keys:
                    continue
                events.append({
                    "t": r.asserted_at, "kind": "fact_set",
                    "summary": f"{r.attribute} = {r.value}",
                    "refs": {"attribute": r.attribute}})
                if r.superseded_at:
                    by = (f" by {r.superseded_by_value}"
                          if r.superseded_by_value else "")
                    events.append({
                        "t": r.superseded_at, "kind": "superseded",
                        "summary": f"{r.attribute}: {r.value} superseded{by}",
                        "refs": {"attribute": r.attribute}})
            # 2 + 3. source entries and edges need the graph node.
            if node is not None and self._storage is not None:
                for en in self._storage.entries_for_entity(
                        node["id"], limit=limit):
                    refs = {"entry_id": en.get("id")}
                    if en.get("episode_title"):
                        refs["episode_title"] = en["episode_title"]
                    events.append({
                        "t": en["ts"], "kind": "entry",
                        "summary": (en.get("text") or "")[:160],
                        "refs": refs})
                g = self._storage.load_graph()
                disp = {e["id"]: e["display"] for e in g["entities"]}
                for e in g["edges"]:
                    if node["id"] not in (e["src_id"], e["dst_id"]):
                        continue
                    if not e.get("asserted_at"):
                        continue
                    events.append({
                        "t": e["asserted_at"], "kind": "edge",
                        "summary": (f"{disp.get(e['src_id'])} —{e['relation']}→ "
                                    f"{disp.get(e['dst_id'])}"),
                        "refs": {"relation": e["relation"]}})
            # 4. lessons whose about/task names the entity.
            if self._lessons is not None:
                for r in self._lessons.current_records():
                    named = {_norm_key(r.entity)}
                    if r.about:
                        named.add(_norm_key(r.about))
                    if named & keys:
                        events.append({
                            "t": r.asserted_at, "kind": "lesson",
                            "summary": r.value,
                            "refs": {"task": r.entity, "aspect": r.attribute}})
            if not events:
                return {"found": False, "entity": entity, "count": 0, "events": []}
            events.sort(key=lambda ev: ev["t"])
            events = events[-max(1, int(limit)):]
            return {"found": True, "entity": display,
                    "count": len(events), "events": events}

    def _log_retrieval_event(self, query: str,
                             entries_out: list[dict],
                             components: list[dict | None] | None = None,
                             params: dict | None = None) -> int | None:
        """Append a ``retrieval_events`` row (schema v31/v32): the (query,
        served) half of the learned-reranker training tuple. Rank = list
        position in the served output. Entries without a storage id
        (pre-persist) are dropped — they can't be joined to a later use.
        A zero-result search still writes its (empty-served) row — misses
        are training signal too, and ``graduation_report`` documents the
        session-count consequence.

        ``components`` (aligned with ``entries_out``) are the fusion inputs
        already computed at ranking time — bi-encoder score, cross-encoder
        score, BM25 boost, recency, the multipliers — and ``params`` is the
        knob snapshot. Both are logged rather than re-derived because
        neither survives to training time: config is mutable at runtime,
        and band recency / supersession flags / access counts mutate on
        every serve, so replaying this query against tomorrow's bank
        reproduces neither the scores nor the pool they came from.

        Never raises: a logging failure must not break the search it
        rides on. Failures bump ``_retrieval_log_errors``, which
        ``stats()`` reports — the log is otherwise silent when broken."""
        cfg = self.config.memory.retrieval_log
        if self._storage is None or not cfg.enabled:
            return None
        try:
            # Length-guarded: a misaligned components list must not
            # truncate the served list (zip stops at the shorter one).
            comps = (components
                     if components is not None
                     and len(components) == len(entries_out)
                     else [None] * len(entries_out))
            served = []
            for rank, (d, comp) in enumerate(zip(entries_out, comps)):
                if d.get("id") is None:
                    continue
                row = {"entry_id": int(d["id"]), "score": d.get("score"),
                       "rank": rank, "via": d.get("via"),
                       "bank": d.get("bank")}
                if comp is not None:
                    row["components"] = comp
                served.append(row)
            _, session_id = self._resolve_writer()
            return self._storage.add_retrieval_event(
                query, served, origin="search", session_id=session_id,
                episode_id=self._current_episode_id(), params=params)
        except Exception:  # noqa: BLE001
            self._retrieval_log_errors += 1
            logger.warning("retrieval-event log failed", exc_info=True)
            return None

    def attach_served_facts(self, event_id: int,
                            facts: list[dict]) -> None:
        """Record the cortex slots a search's cortex-first block served
        (schema v34) on the exact event row that search wrote — the fact
        half of the (query, served, used) training tuple. Called by the
        MCP search handler after it builds the cortex block, with the id
        ``search(return_event_id=True)`` returned. Never raises: training
        plumbing must not break the search response it rides on."""
        cfg = self.config.memory.retrieval_log
        if (self._storage is None or not cfg.enabled or not facts
                or event_id is None):
            return
        try:
            from pseudolife_memory.memory.cortex import _norm_key
            payload = [
                {"entity_norm": _norm_key(f.get("entity", "")),
                 "attribute_norm": _norm_key(f.get("attribute", "")),
                 "rank": rank,
                 "score": f.get("score"),
                 "kind": f.get("kind", "scalar"),
                 "contested": bool(f.get("contested", False))}
                for rank, f in enumerate(facts)
            ]
            with self._lock:
                updated = self._storage.attach_served_facts(
                    int(event_id), payload)
            if not updated:
                self._retrieval_log_errors += 1
                logger.warning(
                    "served-facts attach matched no event row (id=%s)",
                    event_id)
        except Exception:  # noqa: BLE001
            self._retrieval_log_errors += 1
            logger.warning("served-facts attach failed", exc_info=True)

    def _record_retrieval_use(self, entry_id: int, used_via: str) -> None:
        """Implicit relevance label (schema v31): the most recent search in
        this session that served ``entry_id`` gains a ``retrieval_uses``
        row. Never raises: a label failure must not break the fetch it
        rides on."""
        cfg = self.config.memory.retrieval_log
        if self._storage is None or not cfg.enabled:
            return
        try:
            _, session_id = self._resolve_writer()
            self._storage.record_retrieval_use(
                int(entry_id), session_id, used_via,
                float(cfg.use_window_seconds))
        except Exception:  # noqa: BLE001
            self._retrieval_log_errors += 1
            logger.warning("retrieval-use label failed", exc_info=True)

    def prune_retrieval_log(self) -> int:
        """Drop retrieval events older than the configured retention (their
        use labels CASCADE). Rides the dream-sweep tick, like the other
        append-only logs. The lock is load-bearing: the sweep thread calls
        this concurrently with lock-holding writers, and an unlocked storage
        call interleaves psycopg transaction blocks on the shared connection,
        wedging it in-transaction (2026-08-21 daemon incident)."""
        if self._storage is None:
            return 0
        cfg = self.config.memory.retrieval_log
        cutoff = time.time() - cfg.retention_days * 86400
        with self._lock:
            return self._storage.prune_retrieval_events(cutoff)

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
            self._record_retrieval_use(int(entry_id), "get")
        return {"found": True, "entry_id": row["id"], "text": row["text"],
                "source": row.get("source"),
                "reinforcements": row.get("reinforcements", 0),
                "explicit_reinforcements": row.get("explicit_reinforcements", 0),
                "access_count": row.get("access_count", 0) + 1,  # +1 for the bump just applied
                "consolidated_into": facts}

    def reinforce(self, entry_id: int) -> dict[str, Any]:
        """The 'this episode was useful' signal — bump reinforcements (Phase-2
        retention reads it). No-op on a faded episode."""
        with self._lock:
            self._ensure_init()
            if self._storage is None or self._storage.get_entry(int(entry_id)) is None:
                return {"reinforced": False, "faded": True}
            # v33 split: explicit=True moves the explicit counter ONLY here,
            # atomically with the shared one — the dream's trace path bumps
            # the shared counter alone, so usefulness stays separable from
            # consolidation yield.
            self._storage.bump_reinforcements(int(entry_id), 1, explicit=True)
            if self._cms is not None:
                self._cms.bump_entry_reinforcements(int(entry_id), 1)
            self._record_retrieval_use(int(entry_id), "reinforce")
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
                # Drop the slot's read counters too — an orphaned counter
                # would let slot coverage exceed 100% and leak a stale
                # count into a later re-created slot. Guarded: counter
                # hygiene must not fail the forget that already happened.
                if self._storage is not None:
                    from pseudolife_memory.memory.cortex import _norm_key
                    try:
                        self._storage.delete_slot_reads(
                            _norm_key(entity),
                            None if attribute is None else _norm_key(attribute))
                    except Exception:  # noqa: BLE001
                        self._slot_read_errors += 1
                        logger.warning("slot-read cleanup failed",
                                       exc_info=True)
            return {"removed": removed, "entity": entity, "attribute": attribute}

    # ------------------------------------------------------------------
    # Cortex maintenance + startup warmup (dream pass: service_dream.py)
    # ------------------------------------------------------------------

    def cortex_dedup(self, threshold: float = 0.90, dry_run: bool = True) -> dict[str, Any]:
        """One-time, reviewed cleanup of paraphrase sibling slots left by past
        regex auto-promotes. Dry-run by default (reports, writes nothing). Reuses
        the value-free slot embedding, backfilling any SCALAR current record
        missing one. Ops-only (see ``ops/dedup_cortex.py``) — back up the bank
        before applying.

        ``kind == "member"`` records are skipped by the backfill (bank-
        corrupting fix): they never carry their own ``slot_embedding``, and
        giving every member of a slot the identical value-free
        ``f"{entity} {attribute}"`` embedding would make ``dedup_siblings``
        treat them as paraphrase siblings of EACH OTHER and supersede all
        but one, destroying the set. ``dedup_siblings`` also excludes
        members outright as a second, independent guard.

        Returns ``{"dry_run", "threshold", "clusters", "merged"}`` where
        ``clusters`` is a list of ``{"canonical", "retired"}`` and ``merged`` is
        the number of sibling slots retired."""
        with self._lock:
            self._ensure_init()
            assert self._cortex is not None and self._embedder is not None
            for r in self._cortex.current_records():
                if r.kind == "member":
                    continue
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
        episode: str | None = None,
    ) -> dict[str, Any]:
        """Open a NESTED sub-episode under the CALLER's open session episode;
        the parent stays open. ``episode`` (spec 2026-08-25) anchors the nest
        to that handle's session root explicitly — the multi-session-safe
        path, since concurrent sessions share both the transport connection
        and the active-session pointer. Without a handle the caller's session
        resolves as before (``X-PL-Session`` header, then the pointer);
        without either it nests under the global current leaf. An unknown/
        closed/keyless handle degrades to the no-handle path and adds
        ``episode_warning``, mirroring ``store``."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            episode = episode or None      # "" from clients means "no handle"
            # Keyless roots are rejected inside the resolver itself now, so
            # every handle consumer shares one contract.
            resolved = self._resolve_episode_handle(episode)
            episode_warning = bool(episode) and resolved is None
            parent = None
            if resolved is not None:
                root_id, session_id = resolved
                em = self._cms.episodes
                # Pin the parent inside the HANDLE's subtree: two open roots
                # can share a session_key (handle-resume beside a newer
                # root), and the key-based leaf lookup could cross trees.
                parent = em.open_subtree_leaf(root_id, session_id) or em.get(root_id)
            else:
                _, session_id = self._resolve_writer()
                # A sub-episode opened before the first store must still nest
                # under a session root — lazily open one exactly like store does.
                self._ensure_session_episode(session_id)
            ep = self._cms.episodes.start_nested(
                title=title, hint=hint, session_key=session_id, parent=parent)
            self._persist_episodes()
            out = self._episode_to_dict(ep)
            if episode_warning:
                out["episode_warning"] = "unknown or closed episode handle"
            return out

    def episode_end(self, episode: str | None = None) -> dict[str, Any]:
        """Close the caller's currently-open leaf episode and pop to its parent.
        ``{}`` when nothing is open for the caller.

        ``episode`` (spec 2026-08-25): with a handle, ownership is the
        handle's subtree — strictly narrower than the shared connection key.
        Only an open sub-episode BELOW that root is popped; the session root
        itself belongs to the hook lifecycle (SessionEnd / idle reaper) and
        is never closed here. A subtree with no open sub-episode is a plain
        no-op ``{}``; an unknown/closed handle is refused.

        Ownership guard (spec 2026-07-18, handle-less path): with no resolved
        session identity, ``Episodes.end_leaf`` falls back to whichever
        episode is globally "current" — which may belong to a different,
        still-active session. Before closing, the candidate leaf's
        ``session_key`` is compared against the resolved identity; a mismatch
        (including "something is open but I have no identity") is refused as
        a no-op rather than popping a foreign session's episode."""
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            em = self._cms.episodes
            episode = episode or None      # "" from clients means "no handle"
            if episode is not None:
                # resume=False: an end on a reaped/closed session must refuse,
                # not resurrect the root (write paths keep the resume).
                resolved = self._resolve_episode_handle(episode, resume=False)
                if resolved is None:
                    return {"closed": None,
                            "reason": "unknown or closed episode handle"}
                root_id, skey = resolved
                leaf = em.open_subtree_leaf(root_id, skey)
                if leaf is None:
                    return {}
                # Close exactly the subtree leaf — end_leaf(session_key)
                # could pick a foreign leaf when two open roots share a key.
                closed = em.end_episode(leaf)
                self._persist_episodes()
                return self._episode_to_dict(closed)
            _, session_id = self._resolve_writer()
            ep = (em.open_leaf_for(session_id) if session_id is not None
                  else em.open_episode())
            if ep is None:
                return {}
            if ep.session_key != session_id:
                return {"closed": None, "reason": "no owned open session"}
            closed = em.end_leaf(session_key=session_id)
            self._persist_episodes()
            return self._episode_to_dict(closed) if closed is not None else {}

    def episode_start_session(
        self, session_key: str | None, title: str, hint: str | None = None,
    ) -> dict[str, Any]:
        """Idempotent open for a shim-driven session episode.

        If an episode is already open with the same ``session_key`` (a
        resume/compact re-fire), return it unchanged. If the idle reaper
        recently closed it (within the resume window), reopen that same root
        rather than forking a new one (finding 5, 2026-07-19). Otherwise open
        a new root — WITHOUT closing any other session's open episode, so
        concurrent sessions (different projects) coexist cleanly.
        """
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            existing = (self._cms.episodes.open_leaf_for(session_key)
                        if session_key is not None else None)
            if existing is not None:
                return self._episode_to_dict(existing)
            resumed = self._resume_closed_session_locked(session_key)
            if resumed is not None:
                return self._episode_to_dict(resumed)
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

        An explicit non-``None`` ``session_key`` is an explicit target (the
        shim/hook/REST path, unchanged): no match still returns ``{}``.
        ``session_key=None`` (a direct ``POST /api/episode/end`` with no
        ``session_key`` in the body) used to force-close ANY open root — a
        blind pop that could close another workstream's session. It now
        resolves the caller's own identity via ``_resolve_writer`` instead and
        closes only THAT identity's root; if the identity is unresolved or
        owns no open root, nothing is closed and ``{"closed": None, "reason":
        "no owned open session"}`` is returned (ownership guard, spec
        2026-07-18)."""
        ownership_guard = session_key is None
        if session_key is None:
            _, session_key = self._resolve_writer()
        if session_key is None:
            return {"closed": None, "reason": "no owned open session"}
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            result, fire, found = self._close_session_locked(session_key, run_dream)
        if ownership_guard and not found:
            return {"closed": None, "reason": "no owned open session"}
        if fire:
            self._fire_and_forget_dream()
        return result

    def _close_session_locked(
        self, session_key: str | None, run_dream: bool,
        prune_empty: bool = True,
    ) -> tuple[dict[str, Any], bool, bool]:
        """Cascade-close the session root for ``session_key`` and prune the
        subtree if it captured zero entries. Caller MUST hold the lock and have
        ensured init (so both ``episode_end_session`` and the idle reaper can
        reuse it without re-entering the non-reentrant lock). Returns
        ``(result_dict, should_fire_dream, found)``; ``result_dict`` is ``{}``
        when nothing matched OR the subtree was pruned empty — ``found``
        disambiguates the two (``False`` only when no root matched
        ``session_key`` at all).

        ``prune_empty=False`` (the idle reaper) closes an empty subtree but
        KEEPS it: deleting it here orphaned the session's briefing handle
        mid-break (2026-09-02) — the reaper's sweep deletes it with a
        tombstone once past the resume window instead. An explicit end
        (shim exit, ``episode_end``) keeps the immediate prune: the session
        affirmatively finished, so no handle can legitimately return. An
        empty close never fires a dream and is never auto-titled, deferred
        or not."""
        assert self._cms is not None
        em = self._cms.episodes
        closed = em.end_session(session_key)
        found = closed is not None
        result = self._episode_to_dict(closed) if closed is not None else {}
        pruned = False
        empty = False
        if closed is not None:
            subtree = {closed.id} | {
                e.id for e in em.episodes.values()
                if em._descends_from(e, closed.id)
            }
            counts = self._episode_entry_counts()
            if sum(counts.get(i, 0) for i in subtree) == 0:
                empty = True
                if prune_empty:
                    for i in subtree:
                        em.remove(i)
                        self._delete_episode_row(i)
                    pruned = True
                else:
                    self._deferred_empty_roots[closed.id] = float(
                        closed.ended_at or 0.0)
                    self._persist_deferred_empty()
            else:
                self._auto_title_locked(closed, subtree)
                result = self._episode_to_dict(closed)
        if not pruned:
            self._persist_episodes()
        fire = bool(run_dream and result and not pruned and not empty)
        return ({} if pruned else result), fire, found

    def reap_idle_sessions(
        self, idle_seconds: float, now: float | None = None,
    ) -> dict[str, Any]:
        """Close session episodes with no activity for ``idle_seconds``.

        In the direct-HTTP transport there is no session-end signal, so a
        session episode is closed here once idle: non-empty ones are closed
        (firing one end-of-session dream so outcome signals become lessons),
        and empty ones are closed but KEPT until past the resume window —
        the session may only be on a break, and its briefing handle must
        stay resumable (2026-09-02) — then swept with a tombstone. A later
        store from the same client lazily opens a fresh episode. ``now`` is
        injectable for tests. Returns
        ``{"reaped": int, "session_keys": [...], "swept": int}``."""
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
                activity = max(last_ts.get(root.id, root.started_at),
                               self._episode_touches.get(root.id, 0.0))
                for e in em.episodes.values():
                    if (e.id != root.id and e.id in last_ts
                            and em._descends_from(e, root.id)):
                        activity = max(activity, last_ts[e.id])
                if now - activity >= idle_seconds:
                    targets.append(root.session_key)
            for sk in targets:
                _result, fire, _found = self._close_session_locked(
                    sk, run_dream=True, prune_empty=False)
                reaped.append(sk)
                fired_any = fired_any or fire
            swept = self._sweep_stale_empty_roots_locked(now)
        if fired_any:
            self._fire_and_forget_dream()
        return {"reaped": len(reaped), "session_keys": reaped, "swept": swept}

    def _sweep_stale_empty_roots_locked(self, now: float) -> int:
        """Delete session roots the reaper closed EMPTY (deferred prune)
        once ``ended_at`` is past the session resume window, leaving a
        tombstone so the always-pass briefing handle can still be honored
        by recreating the episode (:meth:`_resolve_episode_handle`).
        Deferred from the reaper's close because an immediate prune made
        the handle unknown rather than resumable — the 2026-09-02
        incident: a session that only read before a long break lost its
        attribution.

        Candidates come ONLY from ``_deferred_empty_roots`` — never from a
        scan of the episode log, where "zero live band entries" also
        matches real history whose memories were evicted (the flat
        preset's capacity eviction is a true drop) or forgotten; sweeping
        those would silently delete session records (review finding,
        2026-09-02). A deferred root that was resumed, explicitly pruned,
        or gained entries since is dropped from the map, not swept.
        Caller holds the lock. Returns the number of roots swept."""
        assert self._cms is not None
        em = self._cms.episodes
        retention = float(os.environ.get(
            "PSEUDOLIFE_SESSION_RESUME_SECONDS", "21600"))
        counts: dict[str, int] | None = None
        swept = 0
        map_changed = False
        for rid in list(self._deferred_empty_roots):
            root = em.episodes.get(rid)
            if root is None or root.ended_at is None:
                # explicitly pruned, or resumed — no longer deferred
                del self._deferred_empty_roots[rid]
                map_changed = True
                continue
            if now - root.ended_at <= retention:
                continue
            subtree = {rid} | {
                e.id for e in em.episodes.values()
                if em._descends_from(e, rid)}
            if counts is None:
                counts = self._episode_entry_counts()
            if sum(counts.get(i, 0) for i in subtree) != 0:
                # gained entries after a resume: real history now
                del self._deferred_empty_roots[rid]
                map_changed = True
                continue
            if root.session_key:
                self._episode_tombstones[rid] = (root.session_key,
                                                 float(root.ended_at),
                                                 root.title or "")
            for i in subtree:
                em.remove(i)
                self._delete_episode_row(i)
            del self._deferred_empty_roots[rid]
            map_changed = True
            swept += 1
        if swept:
            self._expire_tombstones_locked(now)
            self._persist_tombstones()
        if map_changed:
            self._persist_deferred_empty()
        return swept

    def _expire_tombstones_locked(self, now: float) -> None:
        """Drop tombstones past the handle-resume window, and cap the map so
        a bank reaping many empty sessions can't grow meta without bound.
        Caller holds the lock; caller persists."""
        window = self._handle_resume_window()
        if window <= 0:
            self._episode_tombstones.clear()
            return
        for tid in [t for t, meta in self._episode_tombstones.items()
                    if now - meta[1] > window]:
            del self._episode_tombstones[tid]
        cap = 200
        if len(self._episode_tombstones) > cap:
            newest = sorted(self._episode_tombstones.items(),
                            key=lambda kv: kv[1][1], reverse=True)[:cap]
            self._episode_tombstones = dict(newest)

    def _persist_tombstones(self) -> None:
        """Write-through the tombstone map (small; best-effort like the
        active-session pointer). No-op in file mode."""
        if self._storage is None:
            return
        try:
            self._storage.set_meta(
                self._EPISODE_TOMBSTONES_META_KEY,
                {tid: {"session_key": key, "ended_at": ended, "title": title}
                 for tid, (key, ended, title)
                 in self._episode_tombstones.items()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("episode tombstone write-through failed: %s", exc)

    def _persist_deferred_empty(self) -> None:
        """Write-through the deferred-empty-root map (small; best-effort
        like the tombstones). No-op in file mode."""
        if self._storage is None:
            return
        try:
            self._storage.set_meta(self._DEFERRED_EMPTY_META_KEY,
                                   dict(self._deferred_empty_roots))
        except Exception as exc:  # noqa: BLE001
            logger.warning("deferred-empty write-through failed: %s", exc)

    @staticmethod
    def _handle_resume_window() -> float:
        """The explicit-handle resume window (seconds). Separate from — and
        much longer than — the session-key window, because a presented
        handle is a daemon-minted id only that session's briefing carried:
        an explicit identity claim, not an inference. Default 30 days so a
        session deferred for lack of GPU time (or just parked) still
        attributes on return (2026-09-02); ``0`` disables closed-handle
        resume and tombstone recreation. Parses defensively: this sits on
        the handle-resolution path, which promises never to raise (a stale
        handle must not lose a memory), so a typo'd env var falls back to
        the default instead of failing every handle-carrying write."""
        try:
            return float(os.environ.get(
                "PSEUDOLIFE_HANDLE_RESUME_SECONDS", "2592000"))
        except ValueError:
            logger.warning("PSEUDOLIFE_HANDLE_RESUME_SECONDS is not a "
                           "number; using the 30-day default")
            return 2592000.0

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

    def _resolve_episode_handle(
            self, handle: str | None, *,
            resume: bool = True) -> tuple[str, str | None] | None:
        """Identity tier 2 (spec 2026-07-18): match ``handle`` (a daemon-minted
        episode id or an unambiguous prefix, >=8 chars) against an OPEN root
        episode — or a reaped one (within
        ``PSEUDOLIFE_HANDLE_RESUME_SECONDS``, its own window, far longer
        than the session-key one), which is reopened, or a swept-empty one,
        which is recreated from its tombstone: the briefing advertises the
        handle as always-pass, so neither the idle reaper closing the root
        mid-session (observed live 2026-08-10) nor the sweep deleting an
        empty root during a multi-day break (2026-09-02) may defeat it.
        Unlike a session-key resume, a handle resume never moves
        ``current_id`` — the writer may be a different session. Caller MUST
        hold the lock. Returns ``(episode_id, session_key)`` on exactly one
        match; ``None`` on any miss — callers warn-and-degrade, never raise
        (a stale handle must not lose a memory)."""
        if not handle or len(handle) < 8 or self._cms is None:
            return None
        matches = [e for e in self._cms.episodes.episodes.values()
                   if e.parent_id is None and e.ended_at is None
                   and e.id.startswith(handle)]
        if len(matches) == 1:
            if matches[0].session_key is None:
                # Keyless roots (global/embedded fallbacks) are not session
                # roots — resolving one would give callers a (id, None) pair
                # each call site patches differently (review, 2026-08-25).
                # Both branches now require a key; callers warn-and-degrade.
                return None
            self._episode_touches[matches[0].id] = time.time()
            return (matches[0].id, matches[0].session_key)
        if len(matches) > 1:
            return None
        # The reopen side effect belongs to WRITE paths (a store must never
        # be lost to a reaped root); lifecycle calls pass resume=False so an
        # end on a closed session refuses instead of resurrecting it.
        if not resume:
            return None
        resume_window = self._handle_resume_window()
        if resume_window <= 0:
            return None
        now = time.time()
        closed = [e for e in self._cms.episodes.episodes.values()
                  if e.parent_id is None and e.ended_at is not None
                  and e.session_key and e.id.startswith(handle)]
        if len(closed) > 1:
            return None
        if len(closed) == 1:
            ep = closed[0]
            if now - ep.ended_at > resume_window:
                return None
            ep.ended_at = None
            ep.closed_by_new_start = False
            self._episode_touches[ep.id] = now
            self._persist_episodes()
            logger.info("resumed session episode %s via handle", ep.id)
            return (ep.id, ep.session_key)
        # Tombstone recreation: the sweep deleted this root as an empty husk
        # while its session was on a long break. The handle proves the
        # session is back — recreate the episode under its original id so
        # attribution lands where the briefing promised.
        tomb = [(tid, meta) for tid, meta in self._episode_tombstones.items()
                if tid.startswith(handle)]
        if len(tomb) != 1:
            return None
        tid, (skey, ended_at, title) = tomb[0]
        if now - ended_at > resume_window:
            return None
        from pseudolife_memory.memory.episodes import Episode
        ep = Episode(id=tid,
                     title=title or time.strftime("session - %Y-%m-%d %H:%M"),
                     started_at=now, session_key=skey)
        self._cms.episodes.episodes[tid] = ep    # never moves current_id
        del self._episode_tombstones[tid]
        self._persist_tombstones()
        self._episode_touches[tid] = now
        self._persist_episodes()
        logger.info("recreated swept session episode %s via handle", tid)
        return (tid, skey)

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
        resumed = self._resume_closed_session_locked(session_key)
        if resumed is not None:
            return resumed.id
        title = time.strftime("session - %Y-%m-%d %H:%M")
        ep = em.start_session(title=title, session_key=session_key)
        self._persist_episodes()
        logger.info("opened session episode %s (session_key=%s)", ep.id, session_key)
        return ep.id

    def _resume_closed_session_locked(self, session_key: str | None):
        """Reopen a recently-reaped root for ``session_key`` so a return
        continues the same episode instead of forking a new one. The idle
        reaper closes a session episode during a long pause, but the same
        resolved identity is by construction the same client session. Returns
        the reopened Episode, or None when there's no key, no closed root, or
        the newest closed root is past
        ``PSEUDOLIFE_SESSION_RESUME_SECONDS`` (default 6 h; ``0`` disables so a
        days-idle session starts fresh). Caller holds the lock; persists on a
        resume.

        Shared by the store path (:meth:`_ensure_session_episode`) and the
        hook path (:meth:`episode_start_session`) — before this was split, a
        SessionStart re-fire (resume/compact) after a reap forked a second
        root while a store resumed, fragmenting one session (finding 5,
        2026-07-19)."""
        if not session_key or self._cms is None:
            return None
        resume = float(os.environ.get(
            "PSEUDOLIFE_SESSION_RESUME_SECONDS", "21600"))
        if resume <= 0:
            return None
        em = self._cms.episodes
        closed = [e for e in em.episodes.values()
                  if e.session_key == session_key
                  and e.parent_id is None and e.ended_at is not None]
        if not closed:
            return None
        last = max(closed, key=lambda e: e.ended_at)
        if time.time() - last.ended_at > resume:
            return None
        last.ended_at = None
        last.closed_by_new_start = False
        em.current_id = last.id
        # The reaper proxies activity by band-entry timestamps; a resumed
        # session that then makes only non-band-entry writes (outcomes,
        # cortex writes) would be re-reaped on the next sweep without this.
        self._episode_touches[last.id] = time.time()
        self._persist_episodes()
        logger.info("resumed session episode %s (session_key=%s)",
                    last.id, session_key)
        return last

    def set_session_title(self, title: str,
                          episode: str | None = None) -> dict[str, Any]:
        """Rename THIS request's session episode (the root keyed by the caller's
        session id, or the ``episode`` handle's root when one is passed — the
        handle is the only identity a hook-registered direct-HTTP client has
        now that the transport fallback is retired). The daemon can't see the
        client's project directory, so session titles default to a generic
        ``session - <date> <time>``; an agent that knows its project calls
        this to name the session. Opens a session episode if none is open yet
        (so it can be called up front).
        Returns ``{"ok": bool, "id": str, "title": str}`` or
        ``{"ok": False, "reason": ...}``."""
        title = (title or "").strip()
        if not title:
            return {"ok": False, "reason": "empty title"}
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            resolved = self._resolve_episode_handle(episode or None)
            if resolved is not None:
                root = self._cms.episodes.get(resolved[0])
                root.title = title
                self._persist_episodes()
                return {"ok": True, "id": root.id, "title": title}
            if episode:
                return {"ok": False,
                        "reason": "unknown or closed episode handle"}
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
                self._retitle_locked(root, title)
            self._persist_episodes()
            return {"ok": True, "id": root.id, "title": title}

    def _session_root_locked(self, session_key: str | None):
        """The OPEN root episode for ``session_key``, or None. Caller holds
        the lock and has ensured init."""
        if not session_key or self._cms is None:
            return None
        for e in self._cms.episodes.episodes.values():
            if (e.session_key == session_key and e.parent_id is None
                    and e.ended_at is None):
                return e
        return None

    def _retitle_locked(self, ep, title: str) -> None:
        """Set an episode's title and rewrite the denormalised
        ``episode_title`` stamp on its band entries (in-memory + DB rows).
        Caller holds the lock and persists the episode log afterwards."""
        assert self._cms is not None
        ep.title = title
        for band in self._cms.bands:
            changed = False
            for e in band.entries:
                if e.episode_id == ep.id and e.episode_title != title:
                    e.episode_title = title
                    changed = True
                    if self._storage is not None and e.db_id is not None:
                        try:
                            self._storage.update_entry(
                                e.db_id, episode_title=title)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("entry retitle failed: %s", exc)
            if changed:
                band._dirty = True

    def _auto_title_locked(self, root, subtree_ids: set[str]) -> None:
        """Derive a content title for a session root that still carries the
        generic lazy-open title at close time (dominant source + first-entry
        snippet — see :func:`derive_session_title`). Agent/shim-named episodes
        never match the generic pattern and are untouched. Caller holds the
        lock."""
        assert self._cms is not None
        if not GENERIC_TITLE_RE.match(root.title or ""):
            return
        entries: list[tuple[float, str, str]] = []
        for band in self._cms.bands:
            for e in band.entries:
                if e.episode_id in subtree_ids:
                    entries.append((e.timestamp, e.source, e.text))
        title = derive_session_title(root.started_at, entries)
        if title:
            self._retitle_locked(root, title)

    def episode_rename(self, id: str, title: str) -> dict[str, Any]:
        """Rename any episode by id (admin surface for the console / REST —
        an agent naming its OWN session uses ``set_session_title``). Rewrites
        the denormalised title stamp on the episode's entries too."""
        title = (title or "").strip()
        if not title:
            return {"ok": False, "reason": "empty title"}
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            ep = self._cms.episodes.get(id)
            if ep is None:
                return {"ok": False, "reason": f"unknown episode {id}"}
            self._retitle_locked(ep, title)
            self._persist_episodes()
            return {"ok": True, "id": ep.id, "title": ep.title}

    def episode_merge(
        self,
        source_ids: list[str],
        into: str | None = None,
        title: str | None = None,
        hint: str | None = None,
    ) -> dict[str, Any]:
        """Merge closed episodes into one (the fragmentation repair tool).

        Target is ``into`` (an existing episode) or a fresh closed rollup
        titled ``title``. Every source's entries and outcome signals are
        re-stamped to the target, child episodes are re-parented, the target's
        span widens to cover the sources, and the source episodes are deleted.
        Open sources are skipped (they are someone's live session) and
        reported in ``skipped_open``."""
        from pseudolife_memory.memory.episodes import Episode
        import uuid as _uuid
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            em = self._cms.episodes
            sources, skipped_open, missing = [], [], []
            for sid in dict.fromkeys(source_ids or []):
                if into is not None and sid == into:
                    continue
                ep = em.get(sid)
                if ep is None:
                    missing.append(sid)
                elif ep.ended_at is None:
                    skipped_open.append(sid)
                else:
                    sources.append(ep)
            base = {"skipped_open": skipped_open, "missing": missing}
            if into is not None:
                target = em.get(into)
                if target is None:
                    return {"ok": False, **base,
                            "reason": f"unknown target episode {into}"}
            elif not (title or "").strip():
                return {"ok": False, **base,
                        "reason": "a new target needs a title"}
            else:
                target = None
            if not sources:
                return {"ok": False, **base,
                        "reason": "no closed source episodes to merge"}
            if target is None:
                target = Episode(
                    id=_uuid.uuid4().hex,
                    title=title.strip(),
                    started_at=min(s.started_at for s in sources),
                    ended_at=max(s.ended_at for s in sources),
                    hint=hint,
                )
                em.episodes[target.id] = target
            src_ids = {s.id for s in sources}
            moved = 0
            for band in self._cms.bands:
                changed = False
                for e in band.entries:
                    if e.episode_id in src_ids:
                        e.episode_id = target.id
                        e.episode_title = target.title
                        moved += 1
                        changed = True
                        if self._storage is not None and e.db_id is not None:
                            try:
                                self._storage.update_entry(
                                    e.db_id, episode_id=target.id,
                                    episode_title=target.title)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "entry retarget failed: %s", exc)
                if changed:
                    band._dirty = True
            if self._storage is not None:
                # Belt-and-braces for rows the bands no longer hold (evicted
                # entries) + the outcome-signals log.
                try:
                    self._storage.retarget_episode_refs(
                        sorted(src_ids), target.id, target.title)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("episode retarget failed: %s", exc)
            for e in em.episodes.values():
                if e.parent_id in src_ids:
                    e.parent_id = target.id
            target.started_at = min(
                [target.started_at] + [s.started_at for s in sources])
            if target.ended_at is not None:
                target.ended_at = max(
                    [target.ended_at]
                    + [s.ended_at for s in sources if s.ended_at])
            for s in sources:
                em.remove(s.id)
                self._delete_episode_row(s.id)
            self._persist_episodes()
            return {"ok": True, "id": target.id, "title": target.title,
                    "merged": sorted(src_ids), "entries_moved": moved,
                    **base}

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
                embedding = self._embedder.encode_query(query)
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
            superseded_entries: list[MemoryEntry] = []
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
                            superseded_entries.append(entry)
                            marked_this_round = True
                if marked_this_round:
                    continue
                # Embedding fallback for paraphrases.
                emb = self._embedder.encode_query(old_text)
                result = self._cms.retrieve(emb, top_k=1, query_text=old_text)
                if result.entries:
                    target = result.entries[0]
                    if target.superseded_at is None:
                        target.superseded_at = now
                        target.superseded_by_text = new_text
                        superseded.append(target.text)
                        superseded_entries.append(target)

            # Write-through the supersession marks, exactly as ``supersede``
            # does. Postgres is the source of truth across a restart and
            # ``_persist_all`` syncs only ``access_count`` for entries — a
            # mark left in RAM is lost at the next ``hydrate_cms`` and the
            # consolidated-away entry comes back looking current.
            if self._storage is not None:
                for e in superseded_entries:
                    if e.db_id is not None:
                        self._storage.update_entry(
                            e.db_id,
                            superseded_at=e.superseded_at,
                            superseded_by_text=e.superseded_by_text,
                        )

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

    # Confidence floor for an edge a reviewer accepted: above graph_review's
    # _DUBIOUS_CONF (0.6) so a settled verdict leaves the queue, below the
    # human bless tier (0.8).
    _REVIEWED_EDGE_MIN_CONF = 0.7

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

    def _resolve_or_create_entity(self, name: str, etype: str | None = None,
                                  *, propose_dupes: bool = False) -> dict:
        """Alias-aware find; auto-create on miss. Caller holds the lock.
        ``propose_dupes`` (dream paths only) files write-dedup merge
        proposals when the created name near-duplicates an existing one."""
        from pseudolife_memory.graph import norm_name
        st = self._storage
        n = norm_name(name)
        found = st.find_entity(n)
        if found is not None:
            if etype and not found.get("etype"):
                st.ensure_entity(found["canonical"], etype=etype)
                found["etype"] = etype
            found["created"] = False
            return found
        # Slot-key fold (2026-07-11): a dreamed name that IS an existing fact
        # slot key ("entity.attribute") resolves to the slot's owner entity
        # instead of minting a node named after the whole key. Exact-match
        # only; recursion terminates because the owner's norm differs from n
        # and cannot itself be a slot key (keys concat two non-empty norms).
        slot_owner = st.find_fact_slot_entity(n)
        if slot_owner is not None and norm_name(slot_owner) != n:
            logger.debug("entity folded to slot owner (slot-key): %r -> %r",
                         name, slot_owner)
            return self._resolve_or_create_entity(slot_owner, etype=etype)
        eid = st.ensure_entity(n, display=name.strip(), etype=etype)
        if propose_dupes:
            self._propose_write_dedup(eid, name)
        return {"id": eid, "canonical": n, "display": name.strip(),
                "etype": etype, "aliases": [], "created": True}

    def _propose_write_dedup(self, entity_id: int, name: str) -> None:
        """Advisory write-time dedup: when the dream mints a new entity whose
        name near-duplicates an existing canonical/display/alias, file an
        entity_proposals merge row for review (dismissed pairs suppressed;
        the merge unique index dedupes re-files). Never blocks the write —
        any failure logs and continues. Caller holds the lock."""
        import time as _t
        try:
            thr = float(self.config.memory.dream.write_dedup_min_jaccard)
            if thr <= 0:
                return
            from pseudolife_memory.graph import degree_counts
            from pseudolife_memory.memory.graph_review import (merge_veto,
                                                               near_duplicate_names)
            g = self._storage.load_graph()
            existing = [{**e, "aliases": g["aliases"].get(e["id"], [])}
                        for e in g["entities"] if e["id"] != entity_id]
            matches = near_duplicate_names(
                name, existing, min_jaccard=thr,
                dismissed=frozenset(self._storage.dismissed_pairs()))
            # Name-shape vetoes (event-slug, numeric-substitution): Jaccard
            # alone filed the project-vs-event and sibling-id classes that
            # dominated the 2026-08-11 triage's 101 rejections. Known limit:
            # the matcher scores across aliases too, so a pair matched via
            # an alias is vetoed against the display name it will present as.
            matches = [m for m in matches
                       if merge_veto(name, m["display"]) is None]
            # Junk-first routing: an entity with a pending junk proposal is
            # the junk queue's to settle — filing a merge against it double-
            # handles the same node (delete-vs-fold) across two queues.
            junk_owned = {p["entity_id"]
                          for p in self._storage.pending_entity_proposals()
                          if p.get("kind") == "junk"}
            matches = [m for m in matches
                       if m["entity_id"] not in junk_owned
                       and entity_id not in junk_owned]
            if not matches:
                return
            deg = degree_counts(g["edges"])
            facts = self._storage.entity_fact_counts()
            now = _t.time()

            def _evidence(eid: int) -> int:
                return deg.get(eid, 0) + facts.get(eid, 0)

            for m in matches[:3]:
                # Present the fold thin -> evidence-bearing. Degree alone let a
                # contentless node be the target and absorb a fact-rich one
                # (2026-07-26); facts count as evidence too.
                a, b = entity_id, m["entity_id"]
                if _evidence(a) > _evidence(b):
                    a, b = b, a
                self._storage.insert_entity_proposal(
                    "merge", a, b, m["score"],
                    f"write-dedup: {name!r} ~ {m['display']!r}", now)
        except Exception as exc:  # noqa: BLE001
            logger.debug("write-dedup scan skipped (%s): %r", exc, name)

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

    def graph_bless_edge(self, src: str, relation: str, dst: str) -> dict[str, Any]:
        """Human 'Keep' on a dubious-edge finding: raise the live edge to
        >=0.8 / origin='user' so it leaves the review queue. Confirms only —
        a missing or superseded edge is not created or revived."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            registry = [r["name"] for r in self._graph.load_relations()]
            resolved, suggestions = G.resolve_relation(registry, relation)
            if resolved is None:
                return {"error": "unknown_relation", "relation": relation,
                        "suggestions": suggestions}
            src_e = self._storage.find_entity(G.norm_name(src))
            dst_e = self._storage.find_entity(G.norm_name(dst))
            if src_e is None or dst_e is None:
                missing = src if src_e is None else dst
                return {"blessed": False, "reason": "unknown_entity",
                        "entity": missing}
            ok = self._graph.bless_edge(src_e["id"], resolved, dst_e["id"])
            if not ok:
                return {"blessed": False, "reason": "edge_not_found",
                        "src": src_e["display"], "relation": resolved,
                        "dst": dst_e["display"]}
            return {"blessed": True, "src": src_e["display"],
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
            dismissed = self._storage.dismissed_pairs()
            lesson_ids = self._storage.lesson_entity_ids()
            fact_counts = self._storage.entity_fact_counts()
        # Merge rows present in the direction an accept will APPLY — the same
        # current-evidence rule graph_accept_entity_merge uses. Without this,
        # the Atlas/console/wiki Merge buttons (which render this payload)
        # could show A → B while the accept folds B → A. Evidence is computed
        # over the FULL graph even for scoped views.
        from pseudolife_memory.graph import degree_counts as _dc
        _deg = _dc(g["edges"])

        def _ev(eid):
            return _deg.get(eid, 0) + fact_counts.get(eid, 0)

        entity_proposals = [
            {**p, "entity_id": p["into_id"], "into_id": p["entity_id"],
             "entity": p["into"], "into": p["entity"]}
            if p.get("kind") == "merge"
            and self._fold_direction(p["entity_id"], p["into_id"], _ev)
            != (p["entity_id"], p["into_id"])
            else p
            for p in entity_proposals]
        entities, edges = g["entities"], g["edges"]
        if scope and scope != "all":
            keep = {eid for eid, ss in src_map.items() if scope in ss}
            entities = [e for e in entities if e["id"] in keep]
            edges = [e for e in edges if e["src_id"] in keep and e["dst_id"] in keep]
        out = gr.review(edges, entities, src_map, proposals=proposals,
                        entity_proposals=entity_proposals,
                        dismissed_pairs=dismissed,
                        lesson_entity_ids=lesson_ids)
        with self._lock:
            out["recent_merges"] = self._storage.recent_entity_decisions()
            out["merge_decision_stats"] = self._storage.merge_decision_stats()
        return out

    def graph_dismiss_duplicate(self, a: str, b: str) -> dict[str, Any]:
        """Human verdict on a duplicate finding: these two names are genuinely
        distinct. Persisted by the ENTITY's stored canonical when the name
        resolves to a live entity (falling back to ``norm_name``): the
        analyzer filters on stored canonicals, and an entity minted from a
        bare name later display-enriched has a canonical ``norm_name`` of the
        display never reproduces — 'GND (Enshrouded server)' (canonical
        ``gnd``) re-listed after every dismissal because the two key spaces
        never met (live bank, 2026-08-16)."""
        from pseudolife_memory import graph as G
        an, bn = G.norm_name(a), G.norm_name(b)
        if not an or not bn or an == bn:
            return {"dismissed": False, "reason": "bad_pair", "a": a, "b": b}
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            canon_by_norm: dict[str, str] = {}
            for e in self._storage.load_graph()["entities"]:
                canon_by_norm.setdefault(e["canonical"], e["canonical"])
                canon_by_norm.setdefault(G.norm_name(e["display"]),
                                         e["canonical"])
            an = canon_by_norm.get(an, an)
            bn = canon_by_norm.get(bn, bn)
            if an == bn:        # both names resolve to one entity: not a pair
                return {"dismissed": False, "reason": "bad_pair",
                        "a": a, "b": b}
            new = self._storage.dismiss_pair(an, bn)
        return {"dismissed": True, "new": new, "a": a, "b": b}

    def curation_dismiss_duplicate(self, store: str, a_entity: str, a_attribute: str,
                                   b_entity: str, b_attribute: str) -> dict[str, Any]:
        """Human verdict on a lesson/world duplicate listing: the two slots are
        genuinely distinct. Persisted in dismissed_pairs under the store's
        namespace prefix (see _CURATION_STORES) so the deep dream never
        re-lists the pair."""
        from pseudolife_memory.memory.cortex import _norm_key
        if store not in _CURATION_STORES:
            return {"dismissed": False, "reason": "bad_store", "store": store}
        an, aa = _norm_key(a_entity), _norm_key(a_attribute)
        bn, ba = _norm_key(b_entity), _norm_key(b_attribute)
        if not (an and aa and bn and ba) or (an, aa) == (bn, ba):
            return {"dismissed": False, "reason": "bad_pair", "store": store}
        a_key, b_key = _slot_key(an, aa), _slot_key(bn, ba)
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            new = self._storage.dismiss_pair(f"{store}:{a_key}", f"{store}:{b_key}")
        return {"dismissed": True, "new": new, "store": store,
                "a_key": a_key, "b_key": b_key}

    def curation_duplicates(self) -> dict[str, Any]:
        """Standing listing of the deep dream's lesson/world cross-key
        near-duplicate pairs — the same candidates deep_dream reports, minus
        the graph-wide pass, so the Console review drawer can load them on
        demand and dismissals take effect immediately."""
        cfg = self.config.memory.deep_dream
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            dismissed = self._storage.dismissed_pairs()
            lesson_recs = self._curation_records("lesson", cfg.snippet_max_chars)
            world_recs = self._curation_records("world", cfg.snippet_max_chars)
        lesson_dups, world_dups = self._slot_duplicate_listings(
            lesson_recs, world_recs, dismissed)
        return {"lesson_duplicates": lesson_dups,
                "world_duplicates": world_dups}

    def _slot_duplicate_listings(self, lesson_recs, world_recs,
                                 dismissed) -> tuple[list[dict], list[dict]]:
        """The lesson/world cross-key duplicate listings from pre-read record
        snapshots — shared by deep_dream and curation_duplicates so the two
        can never drift on threshold/top-k/dismissal wiring."""
        from pseudolife_memory.memory import graph_consolidation as gc
        cfg = self.config.memory.deep_dream
        def one(recs, store):
            return gc.slot_duplicate_candidates(
                recs, min_similarity=cfg.curation_min_similarity,
                top_k=cfg.curation_top_k,
                dismissed=_store_dismissed(dismissed, store))
        return one(lesson_recs, "lesson"), one(world_recs, "world")

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

    def wiki_page(self, entity: str, *, mentions_limit: int = 20,
                  timeline_limit: int = 30) -> dict[str, Any]:
        """Everything the console's wiki page needs for one entity, in one
        call: identity + attribution, canonical facts, cited world facts,
        relations (in/out, derived marked), provenance mentions, a merged
        newest-first chronology, and open review flags. Read-only; never
        creates entities and never runs the full review scan.

        The ``facts`` here deliberately carry NO ``re_verify``: the dossier
        is a Console review view over one entity, reached beside the
        ``mentions`` list and the review queues that show the same
        correction directly, and every fact it lists is also reachable
        through ``memory_search``/``memory_fact_get``, which do carry the
        flag. ``recall`` is annotated because it ANSWERS with facts; this
        page describes an entity."""
        from pseudolife_memory import graph as G
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            st = self._storage
            e = st.find_entity(G.norm_name(entity))
            if e is None:
                return {"found": False, "entity": entity}
            eid = e["id"]
            projects = st.sources_for_entity(eid)
            community = st.load_communities()["assignment"].get(eid)
            g = st.load_graph()
            mentions = st.entries_for_entity(eid, limit=mentions_limit)
            entity_props = [p for p in st.pending_entity_proposals()
                            if eid in (p.get("entity_id"), p.get("into_id"))]
            edge_props = [p for p in st.pending_proposals()
                          if eid in (p.get("src_id"), p.get("dst_id"))]
            # Facts attach by resolving the record's entity through the
            # alias table (find_entity precedence), not by exact canonical
            # match: a record written under a name a later merge folded
            # into this node is this node's fact. One map per call, from
            # the graph already loaded above.
            amap = G.alias_canonical_map(g["entities"], g["aliases"])
            facts = []
            if self._cortex is not None:
                for rec in self._cortex.current_records():
                    n = G.norm_name(rec.entity)
                    if amap.get(n, n) != e["canonical"]:
                        continue
                    facts.append({
                        "attribute": rec.attribute, "value": rec.value,
                        "confidence": round(float(rec.confidence), 4),
                        "origin": rec.origin, "asserted_at": rec.asserted_at,
                        "history_available": rec.supersedes_value is not None,
                    })
            world_facts = []
            if self._world is not None:
                for rec in self._world.current_records():
                    n = G.norm_name(rec.entity)
                    if amap.get(n, n) != e["canonical"]:
                        continue
                    world_facts.append({
                        "attribute": rec.attribute, "value": rec.value,
                        "confidence": round(float(rec.confidence), 4),
                        "source_url": rec.source_url,
                        "retrieved_at": rec.retrieved_at,
                    })
        facts.sort(key=lambda f: f["attribute"])
        world_facts.sort(key=lambda f: f["attribute"])

        # Relations via the existing depth-1 neighborhood (derived edges marked,
        # provenance tags included). Outside the lock — it locks itself.
        nb = self.graph_neighborhood(entity, depth=1, include_facts=False)
        rel_out, rel_in = [], []
        for ed in nb.get("edges", []):
            row: dict[str, Any] = {"relation": ed["relation"],
                                   "derived": bool(ed.get("derived"))}
            if row["derived"]:
                row["via"] = ed.get("via")
            else:
                row["confidence"] = ed.get("confidence")
                row["tag"] = ed.get("tag")
            if ed["src"] == e["display"]:
                rel_out.append({**row, "target": ed["dst"]})
            elif ed["dst"] == e["display"]:
                rel_in.append({**row, "source": ed["src"]})

        disp = {en["id"]: en["display"] for en in g["entities"]}
        timeline = [{"ts": float(e["created_at"]), "kind": "entity-created",
                     "text": f"“{e['display']}” first seen"}]
        for ed in g["edges"]:
            if eid in (ed["src_id"], ed["dst_id"]):
                timeline.append({
                    "ts": float(ed["asserted_at"]), "kind": "edge-asserted",
                    "text": (f"{disp.get(ed['src_id'], '?')} {ed['relation']} "
                             f"{disp.get(ed['dst_id'], '?')}")})
        for f in facts:
            timeline.append({"ts": float(f["asserted_at"] or 0.0),
                             "kind": "fact-stamped",
                             "text": f"{f['attribute']} = {f['value']}"})
        for m in mentions:
            timeline.append({"ts": float(m["ts"] or 0.0), "kind": "mention",
                             "text": (m["text"] or "")[:120]})
        timeline.sort(key=lambda t: t["ts"], reverse=True)
        timeline = timeline[:timeline_limit]

        flags: list[dict[str, Any]] = []
        for p in entity_props:
            flags.append({"kind": p["kind"], "id": p["id"],
                          "entity": disp.get(p.get("entity_id")),
                          "into": disp.get(p.get("into_id")),
                          "reason": p.get("reason"), "score": p.get("score")})
        for p in edge_props:
            flags.append({"kind": "proposed_link", "id": p["id"],
                          "src": disp.get(p.get("src_id")),
                          "relation": p.get("relation"),
                          "dst": disp.get(p.get("dst_id")),
                          "confidence": p.get("confidence")})
        if not projects:
            flags.append({"kind": "unattributed"})

        return {"found": True, "entity": e["display"],
                "canonical": e["canonical"], "etype": e["etype"],
                "aliases": e["aliases"], "projects": projects,
                "community": community, "first_seen": float(e["created_at"]),
                "facts": facts, "world_facts": world_facts,
                "relations": {"out": rel_out, "in": rel_in},
                "mentions": mentions, "timeline": timeline, "flags": flags}

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
        """Return all project sources with their entity counts. Rollup-aware:
        a source mapped to an umbrella in ``memory.scopes.rollup`` carries
        ``parent`` so consumers can nest the family instead of rendering
        umbrella and children as flat peers.

        Returns ``{"projects": [{"source": str, "entities": int,
        "parent"?: str}, ...]}``.
        """
        roll = {str(k).strip().lower(): str(v).strip().lower()
                for k, v in self.config.memory.scopes.rollup.items()}
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"projects": []}
            projects = self._storage.project_source_counts()
        for p in projects:
            umb = roll.get(p["source"])
            if umb and umb != p["source"]:
                p["parent"] = umb
        return {"projects": projects}

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
                # Keyed by the record entity RESOLVED through the alias table
                # (graph.alias_canonical_map) so the per-canonical lookup
                # below also finds facts written under an alias of the node.
                amap = G.alias_canonical_map(g["entities"], g["aliases"])
                for rec in self._cortex.current_records():
                    n = G.norm_name(rec.entity)
                    facts_by_norm.setdefault(amap.get(n, n), []).append({
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
                    "community": comm.get(e["id"]), "sources": src_map.get(e["id"], []),
                    "created_at": float(e["created_at"])}
            if include_facts:
                node["facts"] = facts_by_norm.get(e["canonical"], [])
            nodes.append(node)
        from pseudolife_memory.memory.graph_review import classify_edge as _classify_edge
        edges = [
            {"src": by_id[e["src_id"]], "relation": e["relation"],
             "dst": by_id[e["dst_id"]], "derived": False,
             "confidence": round(float(e["confidence"]), 4),
             "origin": e.get("origin"),
             "asserted_at": float(e["asserted_at"]),
             "tag": _classify_edge(e)}
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
        shortest path when ``to`` names a second entity. A node's facts are
        the cortex records whose entity resolves to it through the alias
        table (``find_entity`` precedence), not only exact canonical
        matches.

        When ``entity`` is ``None`` (or falsy), returns the whole graph
        filtered to ``scope`` (a source name; ``None`` / ``"all"`` = no
        filter) via :meth:`_whole_graph`, capped to ``max_nodes`` hubs."""
        if not entity:
            return self._whole_graph(scope=scope, include_facts=include_facts,
                                     max_nodes=max_nodes)
        from pseudolife_memory import graph as G
        from pseudolife_memory.memory.graph_review import classify_edge as _classify_edge
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
                # Keyed by the record entity RESOLVED through the alias table
                # (graph.alias_canonical_map) so a fact written under a name
                # a merge later folded into a node lands on that node. The
                # map comes from the graph the subgraph read already loaded
                # (no extra query) and current_records() order is kept, so
                # alias-keyed facts interleave by write order.
                amap = G.alias_canonical_map(by_id.values(), aliases)
                for rec in self._cortex.current_records():
                    n = G.norm_name(rec.entity)
                    facts_by_norm.setdefault(amap.get(n, n), []).append({
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
                    row["tag"] = _classify_edge(e)
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

    def graph_backfill_sources(self) -> dict[str, Any]:
        """Refresh entity->project attribution from fact provenance. Cheap,
        idempotent, manual overrides preserved. Applies memory.scopes policy
        (meta-source exclusions + umbrella rollups, case-folded keys). Takes
        the lock itself, so callers must NOT hold it (mirrors graph_backfill
        in dream_run, which runs after the lock is released)."""
        import time as _time
        scopes = self.config.memory.scopes
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return {"attributed": 0}
            n = self._storage.backfill_entity_sources(
                _time.time(), rollup=scopes.rollup,
                exclude=frozenset(scopes.exclude))
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
                # Session digest (spec 2026-08-24, decision 7): attach the
                # narrative body when the dream pass has digested this
                # session; absent, the recap stays the bare title/count.
                summary = self._episode_digest_body(e.get("id"))
                if summary:
                    recap["summary"] = summary
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

    def _episode_digest_body(self, episode_id: str | None) -> str | None:
        """The narrative body (header line stripped) of ``episode_id``'s
        digest entry, or None. Takes the lock itself — callers (the
        briefing orchestrator) must NOT hold it. Band scan only, same cost
        profile as ``_episode_entry_counts``."""
        if not episode_id:
            return None
        with self._lock:
            self._ensure_init()
            assert self._cms is not None
            for band in self._cms.bands:
                for en in band.entries:
                    if (en.source == "digest"
                            and en.episode_id == episode_id
                            and en.superseded_at is None):
                        split = en.text.split("\n", 1)
                        return (split[1] if len(split) > 1
                                else split[0]).strip() or None
        return None

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

    def _curation_records(self, store: str, value_cap: int) -> list[dict]:
        """Label + embedding snapshot of a slot store's CURRENT records, shaped
        for graph_consolidation.slot_duplicate_candidates. Caller holds the
        lock. Values are truncated to ``value_cap`` chars (listing evidence,
        like candidate snippets); records without embeddings are passed
        through and skipped by the pure function."""
        src = self._lessons if store == "lesson" else self._world
        out: list[dict] = []
        for r in (src.current_records() if src is not None else []):
            d = {"key": _slot_key(*r.key), "entity": r.entity,
                 "attribute": r.attribute,
                 "value": r.value[:value_cap] if value_cap else r.value,
                 "embedding": (r.embedding.detach().cpu().numpy()
                               if r.embedding is not None else None)}
            if store == "lesson":
                d.update(polarity=r.polarity, outcome=r.outcome, about=r.about)
            else:
                d.update(source_url=r.source_url)
            out.append(d)
        return out

    @staticmethod
    def _fold_direction(frm: int, into: int, evidence) -> tuple[int, int]:
        """The fold direction a merge proposal SHOULD apply with, re-derived
        from current evidence (degree + fact count, the insert-time rule):
        swap when the stored from-side now outweighs the target; ties keep
        the stored direction. Shared by the review payloads
        (``_enrich_merge_proposals``, ``graph_review``) and
        ``graph_accept_entity_merge`` so both derive direction from the same
        rule. The guarantee is same-rule, not same-instant: accept re-reads
        evidence at click time, so a batch of accepts can legitimately flip
        a later pending row that was displayed before the batch began —
        current evidence is the truth being tracked."""
        return (into, frm) if evidence(frm) > evidence(into) else (frm, into)

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
            # A reviewed edge is no longer dubious: floor its confidence above
            # the dubious_edges threshold AND store it as a confirming action —
            # origin "agent" would be recaptured by the next apply's
            # rescore_edges (pure name-based recompute, e.g. related-to back
            # to 0.45) and re-flagged, undoing the verdict.
            conf = max(float(prop["confidence"] or 0.0), self._REVIEWED_EDGE_MIN_CONF)
            self._graph.upsert_edge(prop["src_id"], prop["relation"], prop["dst_id"],
                                    confidence=conf, origin="action")
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

    def graph_accept_entity_merge(self, proposal_id: int, *,
                                  decided_by: str = "human") -> dict[str, Any]:
        import time as _t
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            prop = self._storage.get_entity_proposal(proposal_id)
            if prop is None or prop["status"] != "pending" or prop["kind"] != "merge":
                return {"accepted": False, "reason": "not_pending", "id": proposal_id}
            from pseudolife_memory.graph import degree_counts
            g = self._storage.load_graph()
            disp = {e["id"]: e["display"] for e in g["entities"]}
            deg = degree_counts(g["edges"])
            facts = self._storage.entity_fact_counts()
            # Same current-evidence rule the enrich payload presented with —
            # the stored direction can be stale (see _enrich_merge_proposals).
            frm, into = self._fold_direction(
                prop["entity_id"], prop["into_id"],
                lambda eid: deg.get(eid, 0) + facts.get(eid, 0))
            now = _t.time()
            # Audit BEFORE the merge: the accepted proposal row CASCADEs away
            # with the folded entity, so merge_decisions is the durable record.
            self._storage.record_merge_decision(
                proposal_id, disp.get(frm, "?"),
                disp.get(into, "?"), "accepted", prop.get("score"),
                prop.get("reason"), decided_by, now)
            ok = self._storage.merge_entity(frm, into)
            self._storage.set_entity_proposal_status(
                proposal_id, "accepted", decided_by=decided_by, decided_at=now)
        return {"accepted": ok, "from": disp.get(frm),
                "into": disp.get(into)}

    def graph_accept_entity_junk(self, proposal_id: int, *,
                                 decided_by: str = "human") -> dict[str, Any]:
        import time as _t
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            prop = self._storage.get_entity_proposal(proposal_id)
            if prop is None or prop["status"] != "pending" or prop["kind"] != "junk":
                return {"accepted": False, "reason": "not_pending", "id": proposal_id}
            disp = {e["id"]: e["display"] for e in self._storage.load_graph()["entities"]}
            # Audit BEFORE the delete, like the merge path: the proposal row
            # CASCADEs away with the entity, so the merge_decisions row
            # (into_display NULL = junk) is the only durable record — and the
            # TOMBSTONE that lets the deep dream auto-suppress a re-mint of
            # the same name instead of re-queueing it for a second verdict.
            self._storage.record_merge_decision(
                proposal_id, disp.get(prop["entity_id"], "?"), None,
                "accepted", prop.get("score"),
                f"junk: {prop.get('reason')}", decided_by, _t.time())
            ok = self._storage.delete_entity(prop["entity_id"])
            self._storage.set_entity_proposal_status(
                proposal_id, "accepted", decided_by=decided_by,
                decided_at=_t.time())
        return {"accepted": ok, "entity": disp.get(prop["entity_id"])}

    def graph_reject_entity_proposal(self, proposal_id: int, *,
                                     decided_by: str = "human") -> dict[str, Any]:
        import time as _t
        with self._lock:
            self._ensure_init()
            if self._storage is None:
                return dict(self._GRAPH_UNAVAILABLE)
            now = _t.time()
            prop = self._storage.get_entity_proposal(proposal_id)
            ok = self._storage.set_entity_proposal_status(
                proposal_id, "rejected", decided_by=decided_by, decided_at=now)
            if ok and prop is not None and prop.get("kind") == "merge":
                disp = {e["id"]: e["display"]
                        for e in self._storage.load_graph()["entities"]}
                self._storage.record_merge_decision(
                    proposal_id, disp.get(prop["entity_id"], "?"),
                    disp.get(prop["into_id"], "?"), "rejected",
                    prop.get("score"), prop.get("reason"), decided_by, now)
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
            LLMController, MechanicalController, recall_state_to_dict,
            run_recall, simple_complete, _hub_threshold,
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
                    "low_confidence": True, "entity_hop": {}, "edge_hop": [],
                    "seed_text_count": 0}
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
        self._annotate_recalled_facts(state.entity_facts)
        return recall_state_to_dict(state, query, hops)

    def _annotate_recalled_facts(self, entity_facts) -> None:
        """Carry ``re_verify`` onto the facts ``recall`` serves, in place.

        ``recall`` reaches canonical facts through the graph projection, not
        the cortex block, so without this the SAME fact reads as cautioned
        via ``memory_search`` and clean via ``memory_recall`` — an
        inconsistency between adjacent read paths that is worse than either
        behaviour alone.

        Annotated HERE rather than in ``graph_neighborhood``, which recall
        composes: that method also backs the Console's Atlas and the whole-
        graph view, where the facts are a label on a node rather than an
        answer being acted on, and where the node set is capped in the
        hundreds. Scoping the work to recall keeps it to one batched trace
        query plus one evidence query per call, whatever the graph's size.

        Facts are matched back to their slot on ``(entity, attribute,
        VALUE)``. The entity side is the record's entity RESOLVED through
        the alias table — the same name ``graph_neighborhood`` attached the
        fact under. The node's entity is the canonical and the record's may
        be an alias of it (a merge folds the absorbed canonical into the
        survivor's aliases without rewriting the record), and ``norm_name``
        of the two differ, so keying on the raw record entity would miss
        exactly the alias-attached facts and silently drop their caution.
        The recall side of the key is a node's DISPLAY name (``run_recall``
        keys ``entity_facts`` by ``node["entity"]``), which a later display
        enrichment can leave normalizing to neither canonical nor alias
        (``GND (Enshrouded server)`` over ``gnd``), so that side resolves
        canonical → alias → display; the record side keeps ``find_entity``
        semantics, since a record's entity is an arbitrary string rather
        than a node label. The value is what makes the match sound. The
        graph normalizes
        names with ``graph.norm_name`` and the cross-index with
        ``cortex._norm_key``, and the two fold DIFFERENT separator classes —
        ``norm_name`` folds ``:``, ``_norm_key`` folds ``-`` and leaves
        ``:`` alone — so ``host:port`` and ``host-port`` are two distinct
        cortex slots hanging off ONE graph node, and
        ``graph_neighborhood`` puts both slots' facts on it. Matching on
        entity and attribute alone therefore annotates both against
        whichever slot won the tie: a missed correction on one, a
        correction from the wrong slot's evidence on the other. The value
        separates them, and where it does not (two records identical in all
        three) the slots are interchangeable for this purpose anyway. Alias
        resolution widens that tie a little: two slots that differ only by
        the alias they were written under (``pr-235`` and ``pr-#235``, same
        attribute and value) now share a key, and post-merge those are the
        same fact written twice, so the same tie-break applies. A set
        slot's members each match on their own value, so a member is
        cleared by ITS own re-confirmation rather than the slot's newest.

        Only the flag keys are written onto the served fact; the
        ``source_entries`` the traversal needs stay on a probe, since a
        recalled fact is a label and would double in size carrying them.

        Cost: one graph load for the alias table (the same load
        ``graph_neighborhood`` makes per hop), one batched trace query and
        one evidence query per call — and one pass over
        ``current_records()`` with two normalizations per record, which is
        the same sweep ``graph_neighborhood`` has already made to assemble
        these facts. ``recall`` holds no lock of its own, so this takes
        it."""
        if not entity_facts or not self.config.memory.traces.enabled:
            return
        from pseudolife_memory import graph as G
        from pseudolife_memory.memory.cortex import _norm_key
        with self._lock:
            self._ensure_init()
            if self._storage is None or self._cortex is None:
                return
            g = self._storage.load_graph()
            amap = G.alias_canonical_map(g["entities"], g["aliases"])
            # The recall side of the key is a node's DISPLAY name (run_recall
            # keys entity_facts by node["entity"]), and a display enriched
            # after minting no longer normalizes to its canonical ("GND
            # (Enshrouded server)" over "gnd", the shape
            # graph_dismiss_duplicate records). That side resolves canonical
            # -> alias -> display, the precedence its canon_by_norm applies;
            # the record side stays find_entity's, since a record's entity
            # is an arbitrary string, not a node label.
            node_of: dict[str, str] = {e["canonical"]: e["canonical"]
                                       for e in g["entities"]}
            for alias, canonical in amap.items():
                node_of.setdefault(alias, canonical)
            for e in g["entities"]:
                node_of.setdefault(G.norm_name(e["display"]), e["canonical"])
            # (alias-resolved entity, attribute, value) -> slot key +
            # confirmation stamp. ``max`` on the stamp only settles records
            # identical in all three. The slot key stays the record's OWN
            # norms — that is where the dream wrote its trace.
            resolved: dict[tuple[str, str, str], tuple[tuple[str, str], float]] = {}
            for rec in self._cortex.current_records():
                n = G.norm_name(rec.entity)
                node_key = (amap.get(n, n), rec.attribute, rec.value)
                seen = rec.last_confirmed or rec.asserted_at
                prev = resolved.get(node_key)
                if prev is None or (seen or 0) > (prev[1] or 0):
                    resolved[node_key] = (
                        (_norm_key(rec.entity), _norm_key(rec.attribute)), seen)
            targets: list[tuple[dict, tuple[str, str], float]] = []
            for entity, facts in entity_facts.items():
                n = G.norm_name(entity)
                for fact in facts or []:
                    hit = resolved.get((node_of.get(n, n),
                                        fact.get("attribute"),
                                        fact.get("value")))
                    if hit is not None:
                        targets.append((fact, hit[0], hit[1]))
            if not targets:
                return
            traces = self._storage.traces_for_slots(
                sorted({slot for _f, slot, _s in targets}))
            probes = [{"source_entries": traces.get(slot, []),
                       "last_confirmed": stamp}
                      for _f, slot, stamp in targets]
            self._annotate_evidence_supersession(probes)
            for probe, (fact, _slot, _stamp) in zip(probes, targets):
                if probe.get("re_verify"):
                    fact["re_verify"] = True
                    fact["re_verify_reason"] = probe["re_verify_reason"]

