"""MCP tool surface — exposes the PseudoLife memory tools to Claude Code.

Built on the FastMCP decorator API from the official ``mcp`` Python SDK.
Each ``@_tool()`` becomes a JSON-RPC tool. The surface (consolidated
2026-07-02, 55 → 32 tools) spans the associative stream (``memory_store`` /
``memory_search`` / ``memory_recent``), the canonical-fact cortex
(``memory_fact_*`` / ``memory_history``), the world cortex
(``memory_world_*``), procedural lessons (``memory_outcome`` /
``memory_lesson_search``), the knowledge graph (``memory_graph*`` /
``memory_recall`` / ``memory_alias``), episodes, verb-dispatched lifecycle
tools (``memory_dream`` / ``memory_forget`` / ``memory_graph_review``), and
the reference bank (``document_*``). Dump/introspection views live in the
Cortex Console (REST), not here — the manifest is agent context every
session, so it stays lean.

Transport (current architecture)
--------------------------------
This module is the shared tool layer for **two** entry points:

* **HTTP daemon** (the shipped path — :mod:`pseudolife_memory.daemon`): one
  long-lived process owns the bank; every session connects over streamable
  HTTP. ``/health`` is open; all other routes require
  ``Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>`` when a token is set, and a
  non-loopback bind without a token is refused. Single-writer by construction.
* **Embedded stdio** (:func:`_run_embedded_stdio`, also the shim's escape
  hatch): the v0.1-style in-process server over stdin/stdout. No auto dream
  sweep here — the daemon owns that cadence.

Configuration
-------------
* ``PSEUDOLIFE_MCP_DATABASE_URL`` — Postgres DSN; when set, PG is the source of
  truth and the in-memory bands are a write-through cache. Unset →
  v0.1 file-only mode.
* ``PSEUDOLIFE_MCP_DATA_DIR`` — where weights + ChromaDB live. **Set this
  explicitly** so the data path is stable regardless of cwd.
* ``PSEUDOLIFE_MCP_CONFIG`` — path to a ``config.yaml`` (optional; sane
  defaults baked in by :class:`MemoryService`).
* ``PSEUDOLIFE_WRITER_ID`` — writer attribution; see :mod:`pseudolife_memory.daemon`.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import atexit
import signal
import threading
import time
from typing import Any, Literal

# Silence torch._dynamo's noisy fall-back warnings on systems without
# Triton (i.e. every Windows install). The embedder forward pass works
# fine in eager — dynamo just tries to compile and gives up loudly.
# Setting TORCHDYNAMO_DISABLE before any torch import is enough; don't
# also touch TORCH_LOGS (an empty value crashes torch's log initialiser).
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

from anyio import to_thread  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

from pseudolife_memory.service import MemoryService  # noqa: E402

# Log to stderr so MCP's JSON-RPC chatter on stdout stays clean.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)
# Dampen torch's INFO/WARNING spam — only surface our own logs by default.
for noisy in ("torch._dynamo", "torch._inductor", "sentence_transformers"):
    logging.getLogger(noisy).setLevel(logging.ERROR)
logger = logging.getLogger("pseudolife-mcp")


# ── Single-instance service ───────────────────────────────────────────────
# Constructed at import time so MCP's tool-list response is instant. The
# heavy work (embedder + CMS init) is deferred until the first tool call
# via ``MemoryService._ensure_init``.
_data_dir = os.environ.get("PSEUDOLIFE_MCP_DATA_DIR")
_config_path = os.environ.get("PSEUDOLIFE_MCP_CONFIG")
service = MemoryService(data_dir=_data_dir, config_path=_config_path)

mcp = FastMCP("PseudoLife Memory")

# Tool-surface tier gate (P1.5). Default "full" = every tool registers (no
# behaviour change). "core" registers only the core-tier set — a lean opt-in
# for weak-model / public / token-conscious deployments. The Cortex Console is
# unaffected (it calls service.* over REST, not MCP tools).
_TOOLSET = os.environ.get("PSEUDOLIFE_MCP_TOOLSET", "full").strip().lower()
_TOOL_TIERS: dict[str, bool] = {}


def _should_register(toolset: str, core: bool) -> bool:
    """Register a tool unless we're in core mode and it's not core-tier."""
    return toolset != "core" or core


def _async_offload(fn):
    """Register-time wrapper: run a sync tool body on a worker thread.

    The MCP SDK invokes sync tools inline on the uvicorn event loop, so one
    long tool call (a dream run, document_ingest, first-call model init) froze
    every other session, /health, and the console (2026-07-02 review, H1).
    ``functools.wraps`` preserves name/docstring/signature (via
    ``__wrapped__``) so FastMCP still derives the tool schema from the real
    parameter list, and AnyIO copies the calling context into the worker
    thread so the per-request writer/session contextvars still resolve.

    Also the surface's uniform failure contract: a service-level raise is
    mapped to the same ``{"error", "message"}`` shape the dispatch tools
    return, instead of leaking a raw exception string to the agent.
    """
    @functools.wraps(fn)
    async def _run(*args: Any, **kwargs: Any) -> Any:
        try:
            return await to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s failed", fn.__name__)
            return {"error": type(exc).__name__, "message": str(exc)}
    return _run


def _tool(*, core: bool = False):
    """Replacement for @_tool() that records the tool's tier and gates
    registration on PSEUDOLIFE_MCP_TOOLSET."""
    def deco(fn):
        _TOOL_TIERS[fn.__name__] = core
        if _should_register(_TOOLSET, core):
            mcp.tool()(_async_offload(fn))
        return fn  # module attr stays the plain sync fn (tests / Console)
    return deco


# ── Associative stream ────────────────────────────────────────────────────


@_tool(core=True)
def memory_store(
    text: str,
    source: str = "claude",
    tags: list[str] | None = None,
    origin: Literal["user", "action", "agent"] | None = None,
) -> dict[str, Any]:
    """Store one durable fact, decision, or observation in associative memory.

    Use proactively when you learn something worth keeping: a preference, a
    decision, an outcome, a correction. One claim per call. Near-duplicates
    are dropped by the surprise gate (``stored=False``,
    ``reason="below_surprise_threshold"`` — a feature, not an error). This
    feeds the associative stream; the background dream pass distils canonical
    facts from it later. For a fact you want canonical NOW, use
    ``memory_fact_set`` instead.

    Args:
        text: The claim to remember.
        source: Stable per-project/topic tag for later filtering.
        tags: Optional cross-cutting labels, e.g. ``["decision", "blocker"]``.
        origin: Who asserted it — ``"user"`` (the human said it), ``"action"``
            (a tool confirmed it), or ``"agent"`` (you concluded it).

    Returns: ``{stored, surprise, reason, cortex_promoted}``.
    """
    return service.store(text=text, source=source, tags=tags, origin=origin)


def _restates_fact(entry_text: str, value: str) -> bool:
    """True when an associative recall hit merely RESTATES a surfaced cortex
    value — so the cortex block already covers it and showing it again is noise.

    Tightened from a bare substring test (which dropped any hit that *mentioned*
    the value, e.g. losing "claude code is the client" to the value "claude").
    A hit is a restatement only when ALL of:
      * the value is at least 5 chars (shorter values are too ambiguous to dedup);
      * it appears bounded by non-alphanumeric edges (a whole token/phrase, so
        "postgres" does not match inside "postgresql"); and
      * it DOMINATES the hit (the value is >= half the normalised text), i.e. the
        hit adds little beyond the value itself. A hit that references the value
        while carrying real extra context is kept.
    """
    t = " ".join((entry_text or "").lower().split())
    v = " ".join((value or "").lower().split())
    if len(v) < 5 or not t:
        return False
    i = t.find(v)
    if i == -1:
        return False
    if (i > 0 and t[i - 1].isalnum()) or (
        i + len(v) < len(t) and t[i + len(v)].isalnum()
    ):
        return False  # substring inside a larger token, not a real mention
    return len(v) >= 0.5 * len(t)


# ── Compact transport shapes (2026-07-10) ────────────────────────────────
# Recall-path tools ship only the fields an agent acts on; bookkeeping
# metadata (timestamps, counters, band/episode attribution, provenance) is
# gated behind ``verbose=True``. Result payloads are per-session agent
# context on every retrieval, so the default stays lean — same rationale as
# the toolset gate. The Cortex Console is unaffected (REST calls service.*).


def _compact_entry(e: dict[str, Any]) -> dict[str, Any]:
    """{id, text, source, tags, score} plus the supersession signal when
    set — ``superseded_by_text`` changes answers, so it always survives."""
    out = {k: e[k] for k in ("id", "text", "source", "tags", "score") if k in e}
    if e.get("superseded"):
        out["superseded"] = True
    if e.get("superseded_by_text"):
        out["superseded_by_text"] = e["superseded_by_text"]
    return out


def _compact_entries(result: dict[str, Any]) -> dict[str, Any]:
    result["entries"] = [_compact_entry(e) for e in result.get("entries", [])]
    return result


@_tool(core=True)
def memory_search(
    query: str,
    top_k: int = 8,
    sources: list[str] | None = None,
    bands: list[str] | None = None,
    episodes: list[str] | None = None,
    tags: list[str] | None = None,
    min_score: float | None = None,
    disable_recency_boost: bool = False,
    rerank: bool | None = None,
    bm25: bool | None = None,
    explain: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """Retrieve memories relevant to a query — associative recall plus
    canonical facts.

    Call at the start of a task and whenever prior context may apply.
    Canonical cortex facts arrive under ``cortex`` AHEAD of the associative
    ``entries`` — they are the current, deduped answer (treat origin
    ``agent`` as revisable; ``contested: true`` means a conflict awaits
    ``memory_fact_resolve``). ``low_confidence=True`` means no confident
    match: prefer to abstain rather than answer from weak entries. On a
    superseded entry, prefer its ``superseded_by_text``.

    Args:
        query: Natural-language description; specific queries retrieve better.
        top_k: Max results (default 8).
        sources / bands / episodes / tags: Optional filters (AND across
            kinds, OR within a list). Band names, most→least recent:
            working, micro, instant, fast, medium, slow, archival, forever.
        min_score: Override the 0.25 relevance floor.
        disable_recency_boost: True for state-probe queries where recency
            bias is unwelcome.
        rerank / bm25: Tri-state config overrides. ``bm25=True`` helps
            exact-keyword queries (function names, versions, error codes);
            ``rerank=True`` cross-encodes the top candidates (~200ms).
        explain: Attach a ranking ``trace`` for debugging (implies verbose).
        verbose: Full per-entry metadata; default entries are compact
            ``{id, text, source, tags, score}`` + supersession when set.

    Returns: ``{query, count, entries, cortex, low_confidence}``.
    """
    result = service.search(
        query=query,
        top_k=top_k,
        sources=sources,
        bands=bands,
        episodes=episodes,
        tags=tags,
        min_score=min_score,
        disable_recency_boost=disable_recency_boost,
        rerank=rerank,
        bm25=bm25,
    )
    # Cortex-first: surface canonical facts above associative recall, and drop
    # any recall hit that merely restates a surfaced fact (currency, not noise).
    # ``cortex`` is part of the documented return shape, so it is always
    # present — an empty list on a miss, never a missing key.
    result.setdefault("cortex", [])
    cc = service.config.memory.cortex
    if cc.enabled and cc.search_first and (query or "").strip():
        facts = service.cortex_search(query, top_k=5, min_score=cc.guard_min_score).get("entries", [])
        if facts:
            result["cortex"] = [
                {
                    "entity": f["entity"], "attribute": f["attribute"],
                    "value": f["value"], "origin": f.get("origin", ""),
                    "confidence": f["confidence"], "score": f.get("score"),
                    "contested": f.get("contested", False),
                    **(
                        {"contender_value": f.get("contender_value"),
                         "contender_origin": f.get("contender_origin", "")}
                        if f.get("contested") else {}
                    ),
                }
                for f in facts
            ]
            fact_vals = [f.get("value", "") for f in facts]
            kept = [
                e for e in result.get("entries", [])
                if not any(_restates_fact(e.get("text", ""), v) for v in fact_vals)
            ]
            result["entries"] = kept
            result["count"] = len(kept)
    # A confident cortex answer must never be flagged low-confidence: the
    # cortex block IS the answer even when associative recall is weak/empty.
    result["low_confidence"] = result.get("low_confidence", False) and not result.get("cortex")
    if explain:
        trace_out = service.trace(
            query=query, top_k=top_k, sources=sources, bands=bands,
            episodes=episodes, tags=tags, rerank=rerank, bm25=bm25,
        )
        result["trace"] = trace_out.get("trace")
    # explain implies verbose: a ranking trace without the entry metadata it
    # scores against would be unreadable.
    if not (verbose or explain):
        result = _compact_entries(result)
    return result


@_tool()
def memory_recent(
    n: int = 10,
    sources: list[str] | None = None,
    episodes: list[str] | None = None,
    tags: list[str] | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """List the N most recently stored memories, newest first — timestamp
    order, not relevance. Useful for "what did I just store?" and for
    catching up at the start of a session. Optional ``sources`` /
    ``episodes`` / ``tags`` filters (AND-combined). ``verbose=True`` for
    full per-entry metadata (default entries are compact).
    """
    result = service.recent(
        n=n, sources=sources, episodes=episodes, tags=tags,
    )
    return result if verbose else _compact_entries(result)


@_tool()
def memory_supersede(old_text: str, new_text: str) -> dict[str, Any]:
    """Mark a stored memory obsolete and record its replacement. The old
    entry is kept but flagged superseded, so retrieval ranks the correction
    higher and shows both together. Matching is exact-text first, then
    nearest-embedding fallback — a close paraphrase of ``old_text`` works.

    Returns: ``{superseded_count, superseded_texts, new_memory_stored}``.
    """
    return service.supersede(old_text=old_text, new_text=new_text)


@_tool(core=True)
def memory_stats() -> dict[str, Any]:
    """Memory-bank vital signs: per-band sizes, capacities, hit rates, and
    totals. Use to gauge how much has been remembered or to diagnose why
    retrieval feels off.
    """
    return service.stats()


@_tool(core=True)  # core memory_fact_get returns source_entries ids —
# core mode must be able to dereference them.
def memory_get(entry_id: int) -> dict[str, Any]:
    """Dereference a memory id (from search results or a fact's
    ``source_entries``) to the full stored episode plus
    ``consolidated_into`` — the canonical facts it produced. Reading it
    gently reinforces it. Returns ``{found: false, faded: true}`` when the
    episode has since been forgotten.
    """
    return service.get_entry(entry_id)


@_tool()
def memory_reinforce(entry_id: int) -> dict[str, Any]:
    """Strengthen one memory after reading it via ``memory_get`` and finding
    it genuinely useful — a deliberate "this mattered" signal that helps it
    resist forgetting. Read first, then reinforce.
    """
    return service.reinforce(entry_id)


# ── Cortex — canonical facts ──────────────────────────────────────────────


@_tool(core=True)
def memory_fact_get(entity: str, attribute: str) -> dict[str, Any]:
    """Look up the one CURRENT canonical value at an ``(entity, attribute)``
    slot — the unambiguous "what is X now?" read. One current value per
    slot, no ranking, no stale duplicates; matching is
    case/separator-insensitive. A null record means the slot is empty, not
    that the topic is unknown — ``memory_search`` still finds associative
    context.

    Returns: ``{record | null, contenders}`` (+ ``entity_ref`` when the
    entity has a graph node). A non-empty ``contenders`` list is an
    unsettled conflict — see ``memory_fact_resolve``. On an empty slot,
    ``candidates`` lists nearby current slots (same entity first, then
    similar slots) — ranked leads, not the answer.
    """
    out = {
        "record": service.cortex_lookup(entity, attribute),
        "contenders": service.cortex_contenders(entity, attribute)["contenders"],
    }
    if out["record"] is None and not out["contenders"]:
        out["candidates"] = service.cortex_candidates(entity, attribute)
    # Graph join: when the subject has a graph node, surface its id +
    # aliases so callers can pivot into memory_graph.
    ref = service.entity_ref(entity)
    if ref is not None:
        out["entity_ref"] = {
            "entity_id": ref["id"], "canonical": ref["canonical"],
            "etype": ref["etype"], "aliases": ref["aliases"],
        }
    return out


@_tool(core=True)
def memory_fact_set(
    entity: str,
    attribute: str,
    value: str,
    origin: Literal["user", "action", "agent"] | None = None,
    confidence: float = 0.8,
) -> dict[str, Any]:
    """Assert a canonical fact NOW — insert, confirm, or correct a slot.

    Setting a new value at an existing ``(entity, attribute)`` slot
    supersedes the old one (kept as history). If the write conflicts with a
    higher-tier fact (e.g. user-stated), it is parked as a contender instead
    (``action="contested"``, with the winning value under ``current``) —
    check with the human, then settle via ``memory_fact_resolve``.

    Args:
        origin: ``"user"`` / ``"action"`` / ``"agent"`` (default agent) —
            who asserts it. Set ``"user"`` for things the human told you.
        confidence: 0..1, default 0.8.

    Returns: ``{action: inserted|confirmed|superseded|contested, ...record}``.
    """
    return service.cortex_write(
        entity, attribute, value,
        confidence=confidence, support=(origin or "agent"),
    )


@_tool(core=True)
def memory_fact_resolve(entity: str, attribute: str, accept: bool) -> dict[str, Any]:
    """Settle a CONTESTED fact slot after checking with the human.
    ``accept=true`` adopts the parked contender as the new current value
    (old value kept as history); ``accept=false`` discards the contender
    and keeps the current value.

    Returns: ``{resolved, accepted, action, current, record}`` or
    ``{resolved: false, reason: "no_contender"}``.
    """
    return service.cortex_resolve(entity, attribute, accept)


@_tool()
def memory_history(entity: str, attribute: str | None = None) -> dict[str, Any]:
    """With ``attribute``: change history of that canonical fact slot — every
    version, oldest→newest, each with its writer/session, transaction time,
    valid time, and age ("what did this used to be? who set it?").

    Without ``attribute``: the entity's causal CHAIN — dated
    fact/entry/edge/lesson events merged oldest→newest ("what led to X?").

    Returns: ``{entity, attribute, count, versions}`` (slot mode) or
    ``{found, entity, count, events}`` (chain mode).
    """
    if attribute is None:
        return service.chain(entity)
    return service.history(entity, attribute)


# ── World cortex + lessons ────────────────────────────────────────────────


@_tool(core=True)
def memory_world_set(
    entity: str,
    attribute: str,
    value: str,
    source_url: str = "",
    source_quote: str = "",
    freshness_class: Literal["evergreen", "slow", "volatile"] = "volatile",
    confidence: float = 0.85,
    retrieved_at: float | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    """Assert a canonical WORLD fact — sourced EXTERNAL knowledge (versions,
    prices, who-holds-a-role, research findings), kept separate from
    user/project facts. Route verified web/docs findings here, with the
    citation. A newer source supersedes an older value at the same slot.

    Args:
        source_url: http(s) citation URL (any other scheme is rejected).
        source_quote: The 1–2 sentences the claim was extracted from.
        freshness_class: ``evergreen`` (never decays) | ``slow`` (months) |
            ``volatile`` (weeks, default) — sets trust decay at read time.
        confidence: 0..1 source confidence (default 0.85).
        retrieved_at / content_hash: Optional fetch time + source hash.

    Returns: ``{action: inserted|confirmed|superseded|rejected, ...record}``.
    """
    return service.world_write(
        entity, attribute, value, confidence=confidence, source_url=source_url,
        source_quote=source_quote, freshness_class=freshness_class,
        retrieved_at=retrieved_at, content_hash=content_hash,
    )


@_tool(core=True)
def memory_world_search(query: str, top_k: int = 5,
                        verbose: bool = False) -> dict[str, Any]:
    """Search current WORLD facts (sourced external knowledge) by
    similarity. Use when a task turns on an external fact your training
    data may have stale. Entries carry ``effective_confidence``
    (age-decayed), a ``stale`` flag (re-verify before relying on it), and
    their ``source_url`` / ``source_quote`` for citation. ``verbose=True``
    for full provenance metadata (default entries are compact).

    Returns: ``{count, entries}``.
    """
    result = service.world_search(query, top_k=top_k, min_score=0.0)
    if not verbose:
        result["entries"] = [_compact_world(e) for e in result.get("entries", [])]
    return result


def _compact_world(e: dict[str, Any]) -> dict[str, Any]:
    out = {k: e[k] for k in ("entity", "attribute", "value",
                             "effective_confidence", "stale", "score")
           if k in e}
    if e.get("source_url"):
        out["source_url"] = e["source_url"]
    if e.get("source_quote"):
        out["source_quote"] = e["source_quote"]
    return out


@_tool(core=True)
def memory_outcome(
    task: str,
    outcome: Literal["success", "failure", "correction"],
    about: str | None = None,
    detail: str | None = None,
    polarity: str | None = None,
) -> dict[str, Any]:
    """Record a procedural outcome signal — what worked, what failed, or
    what the user corrected — the moment it lands. The dream synthesises
    accumulated signals into durable lessons surfaced at future session
    starts; logging outcomes is how you stop repeating mistakes.

    Args:
        task: Kind of task, in stable wording ("deploy engine to host").
        outcome: ``success`` | ``failure`` | ``correction``.
        about: The tool/source/approach the outcome concerns (optional but
            makes the lesson traversable).
        detail: What worked / what the dead-end was.
        polarity: ``+`` do-this | ``-`` avoid; usually omit (inferred).

    Returns: ``{recorded, signal_id, task, outcome}``. Requires Postgres
    storage — file mode returns ``{recorded: false, reason}``.
    """
    return service.record_outcome(
        task, outcome, about=about, detail=detail, polarity=polarity)


@_tool(core=True)
def memory_lesson_search(query: str, top_k: int = 5,
                         verbose: bool = False) -> dict[str, Any]:
    """Search learned lessons (procedural memory) by similarity to the task
    at hand. Call at the START of a task: what worked, what to avoid, what
    the user corrected before. Heed polarity ``-`` entries — known dead-ends.
    ``verbose=True`` for full provenance metadata (default entries are
    compact).

    Returns: ``{count, entries: [{task, aspect, lesson, about, polarity,
    outcome, confidence, score}]}``.
    """
    result = service.lesson_search(query, top_k=top_k)
    if not verbose:
        keep = ("task", "aspect", "lesson", "about", "polarity", "outcome",
                "confidence", "score", "re_verify", "re_verify_reason")
        result["entries"] = [
            {k: e[k] for k in keep if k in e}
            for e in result.get("entries", [])
        ]
    return result


# ── Consolidated lifecycle verbs ──────────────────────────────────────────


@_tool()
def memory_forget(
    scope: Literal["memory", "fact", "world", "lesson"],
    entity: str | None = None,
    attribute: str | None = None,
    text: str | None = None,
    substring: str | None = None,
    source: str | None = None,
    episode: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Hard-delete from one memory store. Cleanup for junk/test data — no
    audit trail. For "this is now wrong, but keep the history" use
    ``memory_fact_set`` (facts) or ``memory_supersede`` (memories) instead.

    Scopes:
        ``memory``: associative entries matching ``text`` / ``substring`` /
            ``source`` / ``episode`` / ``tag`` (at least one filter
            required; filters OR-combine — ANY match deletes, unlike
            memory_search's AND).
        ``fact``: canonical fact slots — ``entity`` required; omit
            ``attribute`` to purge every slot under the entity.
        ``world``: world facts — ``entity`` (+ optional ``attribute``).
        ``lesson``: lessons — pass the task as ``entity``, the aspect as
            ``attribute``.

    Returns: ``{deleted_count, ...}`` for scope memory; ``{removed, ...}``
    otherwise; ``{error}`` on a bad scope or missing argument.
    """
    if scope == "memory":
        if not any((text, substring, source, episode, tag)):
            return {"error": "filter_required",
                    "filters": ["text", "substring", "source", "episode", "tag"]}
        return service.delete(
            text=text, substring=substring, source=source,
            episode=episode, tag=tag,
        )
    if scope in ("fact", "world", "lesson"):
        if not entity:
            return {"error": "entity_required", "scope": scope}
        if scope == "fact":
            return service.cortex_forget(entity, attribute)
        if scope == "world":
            return service.world_forget(entity, attribute)
        return service.lesson_forget(entity, attribute)
    return {"error": "unknown_scope",
            "scopes": ["memory", "fact", "world", "lesson"]}


@_tool()
def memory_dream(
    action: Literal["status", "pull", "commit", "run", "deep"],
    limit: int | None = None,
    cursor: float | None = None,
    apply: bool = False,
    snippets: bool = True,
) -> dict[str, Any]:
    """Drive the dream — consolidation of recent memories into canonical
    facts and graph structure.

    Actions:
        ``status``: backlog + whether a sweep would fire. Read-only.
        ``pull``: memories not yet consolidated (oldest-first, up to
            ``limit``) — read them, write slot-shaped facts via
            ``memory_fact_set``, then commit.
        ``commit``: advance the dream cursor to ``cursor`` (the newest
            timestamp from the pull).
        ``run``: one server-side dream with the configured extractor (up to
            ``limit``; loop until ``pulled=0`` to drain the backlog).
        ``deep``: full-corpus graph consolidation. Dry-run preview by
            default; ``apply=true`` snapshots the graph tables (undo file;
            refuses if it can't) then commits the provably-safe self-clean.
            Settle the returned link candidates via ``memory_graph_review``
            (propose / dismiss_pair) or the Console's Atlas queue;
            ``snippets=false`` omits candidate evidence.

    Returns: per-action dict; ``{error}`` on a bad action or missing cursor.
    """
    if action == "status":
        return service.dream_status()
    if action == "pull":
        return service.dream_pull(limit=limit or 40)
    if action == "commit":
        if cursor is None:
            return {"error": "cursor_required"}
        return service.dream_commit(cursor)
    if action == "run":
        from pseudolife_memory.memory.dream import build_extractor
        return service.dream_run(
            build_extractor(service.config.memory.dream), limit=limit,
        )
    if action == "deep":
        return service.deep_dream(apply=apply, include_snippets=snippets)
    return {"error": "unknown_action",
            "actions": ["status", "pull", "commit", "run", "deep"]}


@_tool()
def memory_graph_review(
    action: Literal["list", "propose", "dismiss_pair", "accept_link",
                    "reject_link", "accept_merge", "accept_junk",
                    "reject_entity"] = "list",
    proposal_id: int | None = None,
    proposals: list[dict] | None = None,
    scope: str | None = None,
    src: str | None = None,
    dst: str | None = None,
) -> dict[str, Any]:
    """Work the graph review queue — deep-dream proposals that need a
    verdict before they touch the real graph.

    Actions:
        ``list``: pending findings/proposals (optionally filtered by
            ``scope``).
        ``propose``: submit link proposals ``[{src, relation, dst,
            similarity?, rationale?}]`` — stored for review, never written
            to the graph directly.
        ``dismiss_pair``: record that ``src`` and ``dst`` (entity names) are
            genuinely distinct — the pair stops resurfacing as a duplicate
            finding or deep-dream candidate.
        ``accept_link`` / ``reject_link``: settle an edge proposal by
            ``proposal_id``.
        ``accept_merge``: fold a near-duplicate entity into its canonical
            twin.
        ``accept_junk``: delete an over-extraction artifact entity.
        ``reject_entity``: keep the entity; dismiss its merge/junk proposal.

    Returns: per-action dict; ``{error}`` on a bad action or missing input.
    """
    if action == "list":
        return service.graph_review(scope=scope)
    if action == "propose":
        if not proposals:
            return {"error": "proposals_required"}
        return service.graph_propose_links(proposals)
    if action == "dismiss_pair":
        if not src or not dst:
            return {"error": "src_dst_required"}
        return service.graph_dismiss_duplicate(src, dst)
    handlers = {
        "accept_link": service.graph_accept_proposal,
        "reject_link": service.graph_reject_proposal,
        "accept_merge": lambda pid: service.graph_accept_entity_merge(
            pid, decided_by="agent"),
        "accept_junk": service.graph_accept_entity_junk,
        "reject_entity": lambda pid: service.graph_reject_entity_proposal(
            pid, decided_by="agent"),
    }
    handler = handlers.get(action)
    if handler is None:
        return {"error": "unknown_action",
                "actions": ["list", "propose", "dismiss_pair",
                            "accept_link", "reject_link", "accept_merge",
                            "accept_junk", "reject_entity"]}
    if proposal_id is None:
        return {"error": "proposal_id_required", "action": action}
    return handler(proposal_id)


# ── Episodes + consolidation ──────────────────────────────────────────────


@_tool(core=True)  # the CLAUDE.md workflow opens sub-episodes for big tasks.
def memory_episode_start(
    title: str, hint: str | None = None,
) -> dict[str, Any]:
    """Open a named sub-episode for a substantial multi-step task. It nests
    under the auto-managed session episode; memories stored while it is open
    carry its id + title, enabling episode-scoped search and summaries
    later. ``memory_episode_end`` closes it and pops back to the session.

    Returns: ``{id, title, started_at, parent_id, ...}``.
    """
    return service.episode_start(title=title, hint=hint)


@_tool(core=True)  # pairs with memory_episode_start.
def memory_episode_end() -> dict[str, Any]:
    """Close the current open episode and pop back to its parent (the
    session). Returns the closed episode dict, or ``{}`` when nothing is
    open.
    """
    return service.episode_end()


@_tool(core=True)  # the recommended workflow names the session early.
def memory_session_title(title: str) -> dict[str, Any]:
    """Name THIS session's auto-opened episode (default titles are
    generic). Call once at the start of work — e.g. ``"PseudoLife-MCP"`` or
    ``"auth-refactor"`` — so session recaps read meaningfully. Idempotent;
    call again to rename.
    """
    return service.set_session_title(title=title)


@_tool()
def memory_episode_summary(id: str) -> dict[str, Any]:
    """Stats, tag/source distribution, and recent entries for one episode —
    "summarise what we worked on". Episode ids appear on search/recent
    results. Returns ``{found: false}`` for an unknown id.
    """
    return service.episode_summary(id=id)


@_tool()
def memory_consolidation_candidates(
    query: str | None = None,
    episode: str | None = None,
    sources: list[str] | None = None,
    tags: list[str] | None = None,
    top_k: int = 20,
    min_cohesion: float = 0.6,
    min_cluster_size: int = 2,
    max_clusters: int = 10,
) -> dict[str, Any]:
    """Find clusters of near-duplicate memories ripe for consolidation —
    the same thing phrased five ways across five sessions. Anchor with a
    ``query`` (topic-driven) or an ``episode`` id (session-driven); read
    the clusters, synthesise one canonical note, then commit it via
    ``memory_consolidate``.

    Args:
        min_cohesion: Minimum intra-cluster cosine (default 0.6) — raise to
            flag only near-duplicates.

    Returns: ``{count, clusters: [{cohesion, size, members}]}``.
    """
    return service.consolidation_candidates(
        query=query,
        episode=episode,
        sources=sources,
        tags=tags,
        top_k=top_k,
        min_cohesion=min_cohesion,
        min_cluster_size=min_cluster_size,
        max_clusters=max_clusters,
    )


@_tool()
def memory_consolidate(
    replaces: list[str],
    new_text: str,
    source: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Replace a cluster of near-duplicate memories with one canonical note.
    Every entry matching ``replaces`` (exact text or close paraphrase) is
    marked superseded by ``new_text``, which is stored fresh — the bank gets
    shorter without losing the audit trail.

    Returns: ``{superseded_count, superseded_texts, new_memory_stored}``.
    """
    return service.consolidate(
        replaces=replaces, new_text=new_text, source=source, tags=tags,
    )


# ── Knowledge graph ───────────────────────────────────────────────────────


@_tool(core=True)
def memory_graph_relate(
    src: str,
    relation: str,
    dst: str,
    origin: str | None = None,
    confidence: float = 0.8,
    src_type: str | None = None,
    dst_type: str | None = None,
) -> dict[str, Any]:
    """Assert a typed relation between two entities, e.g. ``("web-app",
    "runs-on", "host-1")``. Entities auto-create and resolve through
    aliases; re-asserting an edge bumps its confidence. Relations come from
    a closed registry (``depends-on``, ``part-of``, ``runs-on``, ``hosts``,
    ``uses``, ``configures``, ``stores-data-in``, ``related-to``);
    separator variants normalise, and an unknown name is rejected WITH the
    closest matches — pick one, fall back to ``related-to``, or grow the
    vocabulary deliberately via ``memory_relation_define``.

    Returns: ``{src, relation, dst, confidence, warnings}`` or
    ``{error: "unknown_relation", suggestions}``.
    """
    return service.graph_relate(
        src=src, relation=relation, dst=dst, origin=origin,
        confidence=confidence, src_type=src_type, dst_type=dst_type,
    )


@_tool()
def memory_graph_unrelate(src: str, relation: str, dst: str) -> dict[str, Any]:
    """Retract a relation — the edge is marked superseded (kept for audit)
    and leaves ``memory_graph`` results. Re-asserting the same triple later
    revives it.
    """
    return service.graph_unrelate(src=src, relation=relation, dst=dst)


@_tool()
def memory_alias(entity: str, alias: str) -> dict[str, Any]:
    """Bind an alternative name to an entity (e.g. ``pg`` → ``postgres``)
    so facts and graph lookups under either name land on the same node.
    Returns the entity's full alias list.
    """
    return service.graph_alias(entity=entity, alias=alias)


@_tool(core=True)
def memory_graph(
    entity: str,
    depth: int = 1,
    include_facts: bool = True,
    to: str | None = None,
    relation_filter: str | None = None,
) -> dict[str, Any]:
    """Read an entity's graph neighborhood: nodes, typed edges, and each
    node's canonical facts. Transitive/inverse edges arrive pre-derived
    (marked ``derived: true`` with rule provenance). Pass ``to`` for the
    shortest path between two entities; ``relation_filter`` keeps only
    edges whose relation contains the substring.

    Args:
        depth: Hops from the root (default 1, max 3).

    Returns: ``{found, entity, nodes, edges, paths}``.
    """
    out = service.graph_neighborhood(
        entity=entity, depth=depth, include_facts=include_facts, to=to,
    )
    if relation_filter and out.get("edges"):
        rf = relation_filter.lower()
        out = dict(out)
        out["edges"] = [e for e in out["edges"]
                        if rf in str(e.get("relation", "")).lower()]
    return out


@_tool(core=True)
def memory_recall(query: str, hops: int = 3, top_k: int = 5,
                  verbose: bool = False) -> dict[str, Any]:
    """Multi-hop retrieval over the knowledge graph, for RELATIONAL
    questions whose answer is reached by following links — "what does X
    ultimately run on?", "how does A reach C?" — which single-shot
    ``memory_search`` can't chain. Read-only. ``low_confidence: true``
    means no seed entity matched — fall back to ``memory_search``.

    Args:
        hops: Max graph hops (default 3, max 5).
        verbose: Full fact/edge provenance (origin, confidence, derivation).
            Default facts are ``{attribute, value}``, edges
            ``{src, relation, dst}``.

    Returns: ``{seeds, entities, edges, paths, texts, iterations}``.
    """
    out = service.recall(query, hops=hops, top_k=top_k)
    if not verbose:
        out["entities"] = [
            {"entity": n.get("entity"),
             "facts": [{"attribute": f.get("attribute"), "value": f.get("value")}
                       for f in n.get("facts", [])]}
            for n in out.get("entities", [])
        ]
        out["edges"] = [
            {"src": e.get("src"), "relation": e.get("relation"),
             "dst": e.get("dst")}
            for e in out.get("edges", [])
        ]
    return out


@_tool()
def memory_relation_define(
    name: str,
    description: str,
    transitive: bool = False,
    inverse_of: str | None = None,
    src_type: str | None = None,
    dst_type: str | None = None,
) -> dict[str, Any]:
    """Add a relation to the closed graph vocabulary — a deliberate, rare
    act. Prefer the builtins; define one only when a recurring connection
    genuinely fits none of them. Supports transitive closure
    (``transitive=true``) and inverse pairing (``inverse_of``, like
    ``runs-on`` ↔ ``hosts``); soft ``src_type``/``dst_type`` expectations
    warn on mismatch but never reject.
    """
    return service.relation_define(
        name=name, description=description, transitive=transitive,
        inverse_of=inverse_of, src_type=src_type, dst_type=dst_type,
    )


# ── Reference bank ────────────────────────────────────────────────────────


@_tool(core=True)
def document_ingest(path: str, source: str | None = None) -> dict[str, Any]:
    """Index a file (.txt / .md / .pdf) into the reference bank — a
    separate store for background documents (papers, manuals, codebases)
    retrieved by pure cosine similarity, kept apart from conversational
    memory. ``source`` defaults to the filename. ``path`` resolves on the
    SERVER's filesystem — with the Docker daemon, use a path visible inside
    the container (e.g. a mounted volume), not a host path.

    Returns: ``{source, chunks_stored, chunks_total}``.
    """
    return service.ingest_document(path=path, source=source)


@_tool(core=True)
def document_search(query: str, top_k: int = 5) -> dict[str, Any]:
    """Search the reference bank only — ingested documents, no
    conversational memories mixed in. For docs AND memories together, use
    ``memory_search``.
    """
    return service.search_documents(query=query, top_k=top_k)


def _flush_on_exit() -> None:
    # Gated, not unconditional: only persist if THIS process mutated state.
    # An idle/read-only subprocess exiting must never clobber a sibling
    # process's newer writes to the shared cms_state.pt.
    try:
        res = service.autosave_if_changed()
        if res:
            logger.info("durability: flushed changed CMS on exit -> %s", res.get("saved_to"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("exit flush failed: %s", exc)


def _autosave_loop(interval: float) -> None:
    while True:
        time.sleep(interval)
        try:
            res = service.autosave_if_changed()
            if res:
                logger.info("durability: autosaved CMS (state changed)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("autosave loop error: %s", exc)


def _warmup() -> None:
    t = time.time()
    logger.info("warmup: preloading embedder + reranker + NLI ...")
    service.warmup()
    logger.info("warmup: pipeline ready in %.1fs", time.time() - t)


_durability_started = False


def start_background_durability() -> None:
    """Idempotent: atexit flush + debounced autosave loop + model warmup.

    Shared by the embedded-stdio entry point and the HTTP daemon
    (:mod:`pseudolife_memory.daemon`).
    """
    global _durability_started
    if _durability_started:
        return
    _durability_started = True
    # Durability: flush unsaved state on clean exit. SIGKILL cannot be
    # caught — bounded loss is covered by the periodic autosave (and in
    # storage mode entries are transactional anyway; only weights ride
    # the cadence).
    atexit.register(_flush_on_exit)
    _interval = float(os.environ.get("PSEUDOLIFE_MCP_AUTOSAVE_SECONDS", "30"))
    threading.Thread(
        target=_autosave_loop, args=(_interval,), daemon=True, name="pl-autosave"
    ).start()
    # Cold-start mitigation: warm the model pipeline in the background so
    # the first real tool call does not pay init latency.
    threading.Thread(target=_warmup, daemon=True, name="pl-warmup").start()


def _dream_sweep_loop(interval: float) -> None:
    from pseudolife_memory.memory.dream import run_sweep_once
    while True:
        time.sleep(interval)
        try:
            run_sweep_once(service)
        except Exception as exc:  # noqa: BLE001 — a dream must never kill the daemon
            logger.warning("dream sweep error: %s", exc)


_dream_sweep_started = False


def start_dream_sweep() -> None:
    """Idempotent: start the headless dream sweep (Tier 0/2). Off when the
    bank is empty or unconfigured — ``run_sweep_once`` gates on backlog +
    quiescence each tick, so an idle bank does no LLM work. Daemon-only."""
    global _dream_sweep_started
    if _dream_sweep_started:
        return
    if not service.config.memory.dream.enabled:
        return
    from pseudolife_memory.memory.dream import build_extractor, NoOpExtractor
    if isinstance(build_extractor(service.config.memory.dream), NoOpExtractor):
        logger.warning(
            "dream enabled but no extractor LLM configured "
            "(PSEUDOLIFE_DREAM_BASE_URL/_MODEL unset): cortex auto-population is "
            "disabled; only memory_fact_set writes canonical facts. Configure the "
            "extractor sidecar to populate the cortex."
        )
    _dream_sweep_started = True
    interval = float(service.config.memory.dream.sweep_interval_seconds)
    threading.Thread(
        target=_dream_sweep_loop, args=(interval,), daemon=True, name="pl-dream",
    ).start()


def _session_reaper_loop(interval: float, idle_seconds: float) -> None:
    while True:
        time.sleep(interval)
        try:
            service.reap_idle_sessions(idle_seconds)
        except Exception as exc:  # noqa: BLE001 — reaper must never kill the daemon
            logger.warning("session reaper error: %s", exc)


_session_reaper_started = False


def start_session_reaper() -> None:
    """Idempotent: close session episodes idle past a threshold. The direct-HTTP
    transport gives no session-end signal, so this is how a session episode
    closes (fires the end-of-session dream / prunes if empty). Daemon-only."""
    global _session_reaper_started
    if _session_reaper_started:
        return
    _session_reaper_started = True
    idle = float(os.environ.get("PSEUDOLIFE_SESSION_IDLE_SECONDS", "1800"))  # 30 min
    interval = float(os.environ.get("PSEUDOLIFE_SESSION_REAP_SECONDS", "300"))  # 5 min
    threading.Thread(
        target=_session_reaper_loop, args=(interval, idle), daemon=True,
        name="pl-session-reaper",
    ).start()


def _run_embedded_stdio() -> None:
    """v0.1-style in-process stdio server (also the shim's escape hatch)."""
    logger.info(
        "PseudoLife-MCP embedded stdio starting (data_dir=%s, config=%s)",
        service.data_dir,
        _config_path or "<defaults>",
    )
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, lambda *_a: sys.exit(0))
        except Exception:  # noqa: BLE001
            pass
    start_background_durability()
    mcp.run()  # stdio transport.


def main() -> None:
    """Legacy entrypoint (``python -m pseudolife_memory.mcp_server``):
    the v0.1 embedded stdio server. The ``pseudolife-mcp`` console script
    dispatches via :mod:`pseudolife_memory.cli` instead (shim / serve /
    embedded) — the cli module stays torch-free so the shim starts fast."""
    _run_embedded_stdio()


if __name__ == "__main__":
    main()
