"""MCP tool surface — exposes the PseudoLife memory tools to Claude Code.

Built on the FastMCP decorator API from the official ``mcp`` Python SDK.
Each ``@_tool()`` becomes a JSON-RPC tool. The set spans memory
(``memory_store`` / ``memory_search`` / ``memory_recent`` / ``memory_stats``),
the canonical-fact cortex (``memory_fact_get`` / ``memory_fact_set`` /
``memory_history`` / ``memory_facts`` / ``memory_fact_resolve``), the world
cortex (``memory_world_*``), procedural lessons (``memory_outcome`` /
``memory_lesson_*``), the knowledge graph (``memory_graph*`` /
``memory_relation_define`` / ``memory_alias``), the dream pass
(``memory_dream_*``), and the reference bank (``document_*``).

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
  truth (schema v11) and the in-memory bands are a write-through cache. Unset →
  v0.1 file-only mode.
* ``PSEUDOLIFE_MCP_DATA_DIR`` — where weights + ChromaDB live. **Set this
  explicitly** so the data path is stable regardless of cwd.
* ``PSEUDOLIFE_MCP_CONFIG`` — path to a ``config.yaml`` (optional; sane
  defaults baked in by :class:`MemoryService`).
* ``PSEUDOLIFE_WRITER_ID`` — writer attribution; see :mod:`pseudolife_memory.daemon`.
"""

from __future__ import annotations

import logging
import os
import sys
import atexit
import signal
import threading
import time
from typing import Any

# Silence torch._dynamo's noisy fall-back warnings on systems without
# Triton (i.e. every Windows install). The embedder forward pass works
# fine in eager — dynamo just tries to compile and gives up loudly.
# Setting TORCHDYNAMO_DISABLE before any torch import is enough; don't
# also touch TORCH_LOGS (an empty value crashes torch's log initialiser).
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

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


def _tool(*, core: bool = False):
    """Replacement for @_tool() that records the tool's tier and gates
    registration on PSEUDOLIFE_MCP_TOOLSET."""
    def deco(fn):
        _TOOL_TIERS[fn.__name__] = core
        if _should_register(_TOOLSET, core):
            return mcp.tool()(fn)
        return fn  # left callable (tests / Console-via-service); not exposed
    return deco


# ── Tools ─────────────────────────────────────────────────────────────────


@_tool(core=True)
def memory_store(
    text: str,
    source: str = "claude",
    tags: list[str] | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    """Store a fact, observation, or decision in neural memory.

    Use proactively when you learn something durable: a user preference,
    a project decision, an outcome, a correction. Don't store fleeting
    chatter — the surprise gate already drops near-duplicates, but
    treating ``memory_store`` as "log everything" wastes your context
    on retrieval.

    This writes to the associative stream. Canonical **cortex** facts are
    written by the background **dream** pass (an LLM extractor consolidating the
    recent stream) and by explicit ``memory_fact_set`` — a plain ``memory_store``
    does *not* itself promote to the cortex (single-writer cortex; the legacy
    regex auto-promote is opt-in via ``memory.cortex.auto_promote``, default off).
    So for a fact you want canonical *now*, use ``memory_fact_set``; for durable
    notes the dream will distil, ``memory_store`` is right. See ``memory_fact_get``
    / ``memory_fact_set``.

    Args:
        text: The fact to remember. One claim per call works best.
        source: Free-form single-value tag for filtering ("pseudolife",
            "general", "v0.7.6"). Default "claude". Pass a stable tag
            per project/topic so ``memory_search`` can scope its results.
        tags: Optional multi-valued labels alongside ``source``.
            Cross-cutting marks like ``["decision", "blocker"]`` or
            ``["consolidated"]``. Normalised at store time (lowercased,
            stripped, deduped). Filter by these with
            ``memory_search(..., tags=[...])``. While an episode is
            open (see ``memory_episode_start``), entries also carry the
            current episode's id + title automatically.
        origin: Who asserted this — ``"user"`` (the human stated it),
            ``"action"`` (a tool/observation confirmed it), or ``"agent"``
            (you concluded it). Recorded on any auto-promoted cortex fact so
            its trustworthiness is explicit. Omit to default from ``source``
            (conversation->user, claude->agent, tool->action). Set
            ``origin="user"`` when storing something the human told you.

    Returns:
        ``{"stored": bool, "surprise": float, "reason": str|None,
        "cortex_promoted": int}``. ``stored=False`` with
        ``reason="below_surprise_threshold"`` means the bank already knows
        this — a feature, not an error. ``reason="filtered_meta"`` means the
        text looked like a self-referential statement about the memory system.
        ``cortex_promoted`` is how many canonical facts were lifted out.
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
) -> dict[str, Any]:
    """Retrieve memories relevant to a query by associative similarity.

    Call this at the start of a task to load relevant context, or
    mid-task when you suspect prior knowledge applies. Results are
    ranked by similarity × recency × source-weight × supersession,
    with current-state facts ranked above their superseded predecessors.

    Args:
        query: A natural-language description of what you're looking
            for. Longer / more specific queries retrieve better.
        top_k: Max results. Default 8.
        sources: Only return entries whose ``source`` is in this list.
            Use the tags you set on store. None = no filter.
        bands: Only return entries from these MIRAS bands
            (e.g. ``["instant", "fast"]`` for recent stores only,
            ``["forever"]`` for deeply-consolidated facts only).
        min_score: Override the relevance threshold (default 0.25).
            Raise to drop weak hits; lower to widen recall when the
            bank is sparse.
        disable_recency_boost: When True, ignore the per-band recency
            uplift — useful for state-probe queries where popularity
            bias is unwelcome.
        rerank: Override the config's reranker flag. ``None`` (default)
            follows config; ``True`` forces cross-encoder reranking of
            the top-N candidates (~200ms extra after a one-time ~80MB
            model fetch); ``False`` disables reranking even when config
            enables it. Useful when you suspect the bi-encoder is
            mis-ordering near-duplicates.
        bm25: Override the config's BM25 flag. ``None`` follows config;
            ``True`` runs sparse lexical retrieval in parallel with
            dense and fuses the two — catches exact-keyword queries
            (function names like ``process_chunk_v2``, version strings,
            error codes) that dense embeddings can underweight;
            ``False`` disables it.
        episodes: When provided, only return entries whose
            ``episode_id`` is in this list. List the available
            episodes via ``memory_episode_list``. None = no filter.
        tags: When provided, only return entries whose ``tags`` share at
            least one element with this list (OR within the list,
            AND with the other filters). None = no filter.
        explain: When True, also run the ranking tracer and attach a
            ``trace`` dict (per-tier candidates, multipliers, drop reasons) —
            the debug view formerly exposed as ``memory_trace``.

    Returns:
        ``{"query": str, "count": int, "entries": [<entry>...], "cortex":
        [<fact>...]}``. ``entries`` are associative recall hits (text, source,
        bank, timestamp, score, episode, tags, ``superseded`` flag — when True
        prefer ``superseded_by_text``). ``cortex`` (when present) lists canonical
        facts for the query — ``{entity, attribute, value, origin, confidence,
        score}`` — surfaced AHEAD of recall because they're the current,
        deduped answer; ``origin`` says whether the user stated it, a tool
        confirmed it, or you concluded it (treat ``agent`` as revisable).
        ``low_confidence=True`` means no confident match — prefer to abstain
        ("I don't have that") rather than answer from the weak ``entries``.
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
    return result


@_tool(core=True)
def memory_fact_get(entity: str, attribute: str) -> dict[str, Any]:
    """Look up the one CURRENT canonical value at a slot, or null.

    The cortex keeps a single current value per ``(entity, attribute)`` slot
    (supersession-not-decay), so this is the unambiguous "what is X now?" query —
    no ranking, no stale duplicates. Most canonical facts are captured
    automatically from your stores; use this to read one back deterministically.

    Args:
        entity: The thing, e.g. ``"nebula-serpent"`` or ``"Jacque"``. Matched
            case/separator-insensitively (NEBULA-SERPENT == nebula_serpent).
        attribute: The property, e.g. ``"grid_size"`` / ``"type"``.

    Returns:
        ``{"record": {entity, attribute, value, origin, confidence, support,
        ...} | null, "contenders": [...]}``. ``origin`` is user / action / agent.
        ``contenders`` lists any parked rival value(s) that conflicted with this
        slot but were too weak to supersede (see ``memory_fact_resolve``) — a
        non-empty list means a discrepancy is waiting to be settled with the human.
    """
    out = {
        "record": service.cortex_lookup(entity, attribute),
        "contenders": service.cortex_contenders(entity, attribute)["contenders"],
    }
    # Graph join (Phase 2): when the subject has a graph node, surface its
    # id + aliases so callers can pivot into memory_graph.
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
    origin: str | None = None,
    confidence: float = 0.8,
) -> dict[str, Any]:
    """Assert a canonical fact deliberately (insert / confirm / supersede a slot).

    Use when a fact matters and you want it canonical immediately rather than
    relying on auto-capture — or to CORRECT one: setting a new value at an
    existing slot supersedes the old (kept for audit). A higher ``confidence``
    here out-ranks a low-confidence auto-promoted guess.

    If your value conflicts with a higher-tier fact (e.g. a user-stated one) it is
    NOT applied — it's parked as a contender and the response ``action`` is
    ``"contested"`` (with the winning value under ``"current"``). Check in with the
    human, then settle it via ``memory_fact_resolve``.

    Args:
        entity: The thing being described (e.g. ``"project"``).
        attribute: The property (e.g. ``"language"``).
        value: The current value (e.g. ``"rust"``).
        origin: ``"user"`` / ``"action"`` / ``"agent"`` — who asserts it.
            Defaults to ``"agent"`` (you). Set ``"user"`` for things the human
            told you; a later user-origin set promotes the fact's origin.
        confidence: 0..1, default 0.8 (deliberate assertion > 0.5 auto floor).

    Returns:
        ``{"action": "inserted"|"confirmed"|"superseded"|"contested", ...record}``;
        on ``"contested"`` also a ``"current"`` key with the value that won.
    """
    return service.cortex_write(
        entity, attribute, value,
        confidence=confidence, support=(origin or "agent"),
    )


@_tool()
def memory_fact_forget(entity: str, attribute: str | None = None) -> dict[str, Any]:
    """Hard-delete canonical fact(s) — a whole entity or one slot.

    Cleanup for test/garbage facts; leaves no audit trail (for "now wrong but
    keep history" use ``memory_fact_set`` with a new value instead). Does not
    touch associative memory — see ``memory_delete`` for that.

    Args:
        entity: The entity to purge.
        attribute: When given, delete only that one slot; otherwise every slot
            under the entity.

    Returns:
        ``{"removed": int, "entity": str, "attribute": str|null}``.
    """
    return service.cortex_forget(entity, attribute)


@_tool(core=True)
def memory_fact_resolve(entity: str, attribute: str, accept: bool) -> dict[str, Any]:
    """Resolve a CONTESTED canonical fact after checking in with the human.

    When your write conflicts with a higher-tier fact (e.g. a user-stated value),
    the cortex KEEPS the current value and parks yours as a *contender* — you'll
    see ``action="contested"`` on the write, ``contested: true`` in ``memory_search``,
    and the contender under ``memory_fact_get``. That's your cue to ask the human,
    then call this to settle it:

    - ``accept=true``  -> adopt the contender as the new current value (recorded as
      user-confirmed; the old value is kept as superseded history).
    - ``accept=false`` -> discard the contender (retired); the current value stays.

    Args:
        entity: The slot entity (case/separator-insensitive).
        attribute: The slot attribute.
        accept: REQUIRED. ``true`` adopts the contender, ``false`` discards it.

    Returns:
        ``{"resolved": bool, "accepted": bool, "action": str, "current": {...},
        "record": {...}}``, or ``{"resolved": false, "reason": "no_contender"}``
        when nothing is parked at the slot.
    """
    return service.cortex_resolve(entity, attribute, accept)


@_tool()
def memory_history(entity: str, attribute: str) -> dict[str, Any]:
    """The change history of a canonical fact — how it evolved and who changed it.

    Returns every version at the ``(entity, attribute)`` slot (the current value
    plus superseded ones), oldest→newest, each with its temporal/provenance
    stamp: ``writer_id`` + ``session_id`` (which session/agent wrote it),
    ``tx_time`` (when), ``valid_time`` (when it became true), and a human
    ``age``. Use it to answer "what did this used to be?" or "who set this?".

    Args:
        entity: The slot entity (case/separator-insensitive).
        attribute: The slot attribute.

    Returns:
        ``{"entity", "attribute", "count": int, "versions": [{value, status,
        writer_id, session_id, tx_time, valid_time, age, ...}, ...]}``.
    """
    return service.history(entity, attribute)


@_tool()
def memory_facts(limit: int = 120) -> dict[str, Any]:
    """List all CURRENT canonical facts (introspection / audit of the cortex).

    Args:
        limit: Max facts to return (sorted by entity, attribute).

    Returns:
        ``{"count": int, "entries": [{entity, attribute, value, origin,
        confidence, ...}, ...]}``.
    """
    dump = service.cortex_dump()
    entries = dump.get("entries", [])[: max(0, int(limit))]
    return {"count": len(entries), "entries": entries}


@_tool()
def memory_get(entry_id: int) -> dict[str, Any]:
    """Dereference a source-entry pointer from a fact's `source_entries`: returns
    the full dense memory episode and `consolidated_into` (the facts it formed).
    Reading it gently reinforces it. `{found: false, faded: true}` if the episode
    has since been forgotten.
    """
    return service.get_entry(entry_id)


@_tool()
def memory_reinforce(entry_id: int) -> dict[str, Any]:
    """After reading an episode via `memory_get` and finding it genuinely useful,
    call this to strengthen it — a deliberate 'this mattered' signal that helps the
    episode resist forgetting. Read first, then reinforce.
    """
    return service.reinforce(entry_id)


@_tool(core=True)
def memory_world_set(
    entity: str,
    attribute: str,
    value: str,
    source_url: str = "",
    source_quote: str = "",
    freshness_class: str = "volatile",
    confidence: float = 0.85,
    retrieved_at: float | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    """Assert a canonical WORLD fact — sourced EXTERNAL knowledge, kept separate
    from the user/project cortex (its own ``world_facts`` table, origin=source).

    Use this for durable facts about the world that your frozen training data may
    have wrong or stale (current model versions, prices, who holds a role, research
    findings). A newer source supersedes an older value at the same slot. Trust is
    age-decayed at read time by ``freshness_class``.

    Args:
        entity / attribute / value: the (slot, value), e.g. ``anthropic`` /
            ``latest-model`` / ``opus-4.8``.
        source_url: where the claim came from (the citation).
        source_quote: the 1–2 sentences it was extracted from (shown as evidence).
        freshness_class: ``evergreen`` (definitions/how-things-work; never decays) |
            ``slow`` (months: leading X, a CEO) | ``volatile`` (weeks: latest
            version, price). Default ``volatile`` (under-trust when unsure).
        confidence: 0..1 source confidence (default 0.85).
        retrieved_at: epoch seconds the source was fetched; omit for "now".
        content_hash: optional hash of the source, to detect drift on revisit.

    Returns:
        ``{"action": "inserted"|"confirmed"|"superseded", ...record, effective_confidence, stale}``.
    """
    return service.world_write(
        entity, attribute, value, confidence=confidence, source_url=source_url,
        source_quote=source_quote, freshness_class=freshness_class,
        retrieved_at=retrieved_at, content_hash=content_hash,
    )


@_tool(core=True)
def memory_world_search(query: str, top_k: int = 5) -> dict[str, Any]:
    """Search current WORLD facts (sourced external knowledge) by similarity.

    Each entry carries ``effective_confidence`` (age-decayed), a ``stale`` flag
    (past 2×TTL → re-verify before relying on it), and its ``source_url`` /
    ``source_quote`` so you can cite it. Prefer a fresh, sourced world fact over
    your own (frozen) training intuition when they conflict.

    Returns: ``{"count": int, "entries": [{entity, attribute, value, source_url,
    source_quote, freshness_class, effective_confidence, stale, score}, ...]}``.
    """
    return service.world_search(query, top_k=top_k, min_score=0.0)


@_tool()
def memory_world_facts(limit: int = 120) -> dict[str, Any]:
    """List all current WORLD facts (introspection / audit of the world cortex).

    Returns: ``{"count": int, "entries": [...]}`` sorted by (entity, attribute),
    each with its citation + decayed effective_confidence + stale flag.
    """
    dump = service.world_dump()
    entries = dump.get("entries", [])[: max(0, int(limit))]
    return {"count": len(entries), "entries": entries}


@_tool()
def memory_world_forget(entity: str, attribute: str | None = None) -> dict[str, Any]:
    """Hard-delete WORLD fact(s) — a whole entity or one slot (cleanup; no audit
    trail). Only touches the world cortex, never the user/project facts.

    Returns: ``{"removed": int, "entity": str, "attribute": str|null}``.
    """
    return service.world_forget(entity, attribute)


@_tool(core=True)
def memory_outcome(
    task: str,
    outcome: str,
    about: str | None = None,
    detail: str | None = None,
    polarity: str | None = None,
) -> dict[str, Any]:
    """Record a PROCEDURAL outcome signal — what happened while doing a task, so the
    agent can learn from its own work across sessions. Cheap and immediate: this
    logs a *signal*; the dream later synthesises durable LESSONS from accumulated
    signals (single-writer — this never writes a lesson directly).

    Use it the moment you notice an outcome worth remembering:
      * a success — an approach / tool / source that worked well for this kind of task;
      * a dead-end (failure) — something that did NOT work, so a future run avoids it;
      * a correction — the user overrode an assumption (these are also auto-captured).

    Args:
        task: the kind of task, in stable wording ("deploy engine to host",
            "debug a failing PG migration"). Becomes a graph ``task-type`` node.
        outcome: ``success`` | ``failure`` | ``correction``.
        about: the tool / source / approach the outcome concerns (the edge object,
            e.g. ``tar --same-owner``). Optional but makes the lesson traversable.
        detail: what worked / what the dead-end was (free text).
        polarity: ``+`` do-this | ``-`` avoid; usually omit (inferred from outcome).

    Returns: ``{"recorded": bool, "signal_id": int, "task": str, "outcome": str}``.
    """
    return service.record_outcome(
        task, outcome, about=about, detail=detail, polarity=polarity)


@_tool(core=True)
def memory_lesson_search(query: str, top_k: int = 5) -> dict[str, Any]:
    """Search learned LESSONS (procedural memory) by similarity to the task at hand.

    Call this at the START of a task to recall what worked, what to avoid, and what
    the user corrected last time — fewer wasted steps. Dead-ends come back with
    ``polarity`` ``-`` and ``outcome`` ``failure``; heed them.

    Returns: ``{"count": int, "entries": [{task, aspect, lesson, about, polarity,
    outcome, confidence, score}, ...]}``.
    """
    return service.lesson_search(query, top_k=top_k)


@_tool()
def memory_lessons(limit: int = 120) -> dict[str, Any]:
    """List all current LESSONS (introspection / audit of procedural memory).

    Returns: ``{"count": int, "entries": [...]}`` sorted by (task, aspect), each
    with its polarity, outcome, ``about`` object, confidence, and provenance.
    """
    return service.lessons_dump(limit=limit)


@_tool()
def memory_lesson_forget(task: str, aspect: str | None = None) -> dict[str, Any]:
    """Delete LESSON(s) — a whole task-type or one aspect (cleanup / manual
    correction). Only touches the lessons store.

    Returns: ``{"removed": int, "task": str, "aspect": str|null}``.
    """
    return service.lesson_forget(task, aspect)


@_tool()
def memory_dream_status() -> dict[str, Any]:
    """Read-only: how much unconsolidated memory is waiting for a dream.

    Returns ``{backlog, idle_seconds, dream_cursor, would_fire}``. Safe to call
    from a SessionStart hook to decide whether to nudge a ``/dream``.
    """
    return service.dream_status()


@_tool()
def memory_dream_pull(limit: int = 40) -> dict[str, Any]:
    """Eligible memories not yet consolidated (timestamp > dream_cursor),
    oldest-first. The agent reads these, extracts canonical facts, writes them
    with ``memory_fact_set``, then calls ``memory_dream_commit``.

    Returns ``{cursor, count, entries:[{text, timestamp, episode_id}, ...]}``.
    """
    return service.dream_pull(limit=limit)


@_tool()
def memory_dream_commit(cursor: float) -> dict[str, Any]:
    """Advance the dream cursor (monotonic) after consolidating up to ``cursor``
    (the newest timestamp from the pull). Returns ``{dream_cursor}``.
    """
    return service.dream_commit(cursor)


@_tool()
def memory_dream_run(limit: int | None = None) -> dict[str, Any]:
    """Run one server-side dream with the configured extractor: pull -> extract
    -> fact_set -> commit. Uses the regex floor (Tier 0, no LLM) unless a
    ``memory.dream`` extractor endpoint is configured (Tier 2), in which case it
    uses that. For the highest quality without any config, the agent should
    instead use ``memory_dream_pull`` + ``memory_fact_set`` (the ``/dream``
    command).

    ``limit`` caps how many memories this call consolidates (default the
    configured ``max_batch``). Loop with a large ``limit`` until ``pulled``
    is 0 to drain the full backlog in one shot.

    Returns ``{pulled, claims, inserted, confirmed, contested, superseded,
    relations, cursor, lessons}``.
    """
    from pseudolife_memory.memory.dream import build_extractor
    return service.dream_run(
        build_extractor(service.config.memory.dream), limit=limit,
    )


@_tool()
def memory_list_sources() -> dict[str, Any]:
    """Enumerate every source tag in the bank, with entry counts.

    Use before ``memory_search``, ``memory_recent``, or ``memory_delete``
    to discover what tags actually exist instead of guessing. Sorted by
    count descending.

    Returns:
        ``{"sources": [{"source": str, "count": int}, ...], "total": N}``.
    """
    return service.list_sources()


@_tool()
def memory_delete(
    text: str | None = None,
    substring: str | None = None,
    source: str | None = None,
    episode: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Remove memories matching any of the provided filters.

    Hygiene tool — call when you stored junk during testing, when a
    source tag accumulated noise, or when a specific wrong fact needs
    to be purged outright. (For "this is now wrong, but keep the
    history" use ``memory_supersede`` or ``memory_consolidate`` instead.)

    At least one filter is required; bare ``memory_delete()`` returns
    an error so accidental wholesale deletion is impossible. Filters
    combine with OR (an entry matching any filter is removed).

    Args:
        text: Exact text match.
        substring: Remove any entry whose text contains this substring.
        source: Remove every entry tagged with this source.
        episode: Remove every entry stamped with this episode id —
            handy for wiping an experimental session wholesale.
        tag: Remove every entry that carries this tag (single value).

    Returns:
        ``{"deleted_count": N, "deleted_texts": [...]}``. The texts
        list is capped at 20 to keep responses small on large purges.
    """
    return service.delete(
        text=text, substring=substring, source=source,
        episode=episode, tag=tag,
    )


@_tool()
def memory_recent(
    n: int = 10,
    sources: list[str] | None = None,
    episodes: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """List the N most recently stored memories, newest first.

    Unlike ``memory_search``, this is timestamp-ordered — useful for
    debugging ("what did I just store?") or for catching up at the
    start of a session ("what happened recently?").

    Args:
        n: How many to return. Default 10.
        sources: Optional source-tag filter.
        episodes: Optional episode-id filter. AND-combined with the
            other filters. None = no filter.
        tags: Optional tag filter. Entry matches when its tag set
            intersects this list. None = no filter.
    """
    return service.recent(
        n=n, sources=sources, episodes=episodes, tags=tags,
    )


@_tool()
def memory_supersede(old_text: str, new_text: str) -> dict[str, Any]:
    """Mark an outdated fact obsolete and record its replacement.

    Use when you (or the user) realise a stored memory is wrong or
    out-of-date. The old entry stays in the bank but is flagged
    ``superseded`` so retrieval ranks the correction higher and
    surfaces both together — the LLM sees "old fact + ↳ corrected by:
    new fact" so it can answer state-probe questions correctly.

    Matching is by exact text first, then top-1 embedding retrieval
    as fallback. The ``new_text`` is also stored as a fresh memory so
    future searches surface it on its own merit.

    Args:
        old_text: The fact to retire. Doesn't need to match exactly —
            a near-paraphrase works via embedding fallback.
        new_text: The replacement. Will be stored with
            ``source="correction"`` for audit.

    Returns:
        ``{"superseded_count": N, "superseded_texts": [...],
        "new_memory_stored": bool}``.
    """
    return service.supersede(old_text=old_text, new_text=new_text)


@_tool(core=True)
def memory_stats() -> dict[str, Any]:
    """Per-band sizes, capacities, hit rates, totals.

    Useful for "how much have I remembered about X?" or for diagnosing
    why retrieval feels off (low hit rate on a deep tier means
    promotion isn't happening as expected).
    """
    return service.stats()


@_tool()
def memory_save() -> dict[str, Any]:
    """Flush CMS tensors to disk now.

    Stores are persisted lazily — call this explicitly when you want a
    snapshot, or at the end of a long working session. ChromaDB (the
    reference bank) persists itself transparently, so this only
    matters for the neural bands.
    """
    return service.save()


@_tool()
def memory_episode_start(
    title: str, hint: str | None = None,
) -> dict[str, Any]:
    """Open a NESTED sub-episode under the current open (session) episode.

    Every memory stored while the episode is open carries the episode
    id + title automatically, enabling later queries like
    ``memory_search(..., episodes=[id])`` ("what did we work on in this
    session?") and ``memory_episode_summary(id)`` for a structured
    rundown. A session-scoped search expands to the whole subtree, so a
    sub-episode's entries surface under its parent session too.

    Session episodes are opened/closed for you by the session-lifecycle
    hooks. Use this for a substantial multi-step TASK: it nests under the
    open session (the parent STAYS open) and ``memory_episode_end`` pops
    back to it. With nothing open, it opens a root episode. (It no longer
    auto-closes a prior open episode — sub-episodes nest rather than
    replace.)

    Args:
        title: Human label for the episode. Surfaces in retrieval
            responses and in ``memory_episode_list``.
        hint: Optional longer descriptor / goal.

    Returns:
        ``{"id": str, "title": str, "started_at": float, "ended_at":
        None, "hint": str|None, "closed_by_new_start": bool,
        "parent_id": str|None, "session_key": str|None}``.
    """
    return service.episode_start(title=title, hint=hint)


@_tool()
def memory_episode_end() -> dict[str, Any]:
    """Close the current open (leaf) episode and pop back to its parent.

    With a nested sub-episode open, this closes it and resumes stamping
    new memories with the PARENT (session) episode. With only a root
    episode open, it closes that and stores afterward carry
    ``episode_id=None`` until the next ``memory_episode_start``. Returns
    the closed episode dict (with ``ended_at`` set), or an empty dict
    when nothing is open.
    """
    return service.episode_end()


@_tool()
def memory_episode_list(
    limit: int = 20, include_open: bool = True,
) -> dict[str, Any]:
    """List episodes newest-first with per-episode entry counts.

    Use to discover what sessions are available before scoping a search
    or summary. Each row includes ``id``, ``title``, ``started_at``,
    ``ended_at`` (null if currently open), ``hint``, and
    ``entry_count``.

    Args:
        limit: Max episodes returned. Default 20.
        include_open: When False, hide the currently-open episode.
    """
    return service.episode_list(limit=limit, include_open=include_open)


@_tool()
def memory_episode_summary(id: str) -> dict[str, Any]:
    """Stats + tag distribution + recent entries for an episode.

    Useful at the end of a session ("summarise what I worked on") or
    when planning a consolidation pass on an old session.

    Args:
        id: The episode id (from ``memory_episode_start`` /
            ``memory_episode_list``).

    Returns:
        ``{"found": bool, "id", "title", "started_at", "ended_at",
        "entry_count", "tag_distribution": [...], "source_distribution":
        [...], "recent_entries": [<entry>...]}``. Returns
        ``{"found": False, "id": id}`` when the id is unknown.
    """
    return service.episode_summary(id=id)


@_tool()
def memory_list_tags() -> dict[str, Any]:
    """Enumerate every tag in the bank, with occurrence counts.

    Sister tool to ``memory_list_sources``. Run before
    ``memory_search(..., tags=[...])`` to discover tags Claude has
    actually stored rather than guessing. Sorted by count descending;
    ``total`` counts occurrences (an entry with two tags counts as 2).
    """
    return service.list_tags()


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
    """Surface clusters of mutually-similar memories ripe for consolidation.

    The memory bank accumulates near-duplicate facts over time — the
    same decision phrased five different ways across five sessions.
    This tool clusters such candidates by mutual cosine similarity so
    Claude can read the cluster, synthesise a single canonical note,
    and commit it via ``memory_consolidate``.

    Two modes:

    * **Query-driven** (``query`` given): embed the query, retrieve the
      top-N candidates through the standard search pipeline (filters,
      rerank, BM25 all apply), then cluster.
    * **Episode-scoped** (``query=None``, ``episode=...``): treat the
      episode's entries as the candidate pool. Useful for "summarise
      this session" workflows.

    At least one of ``query`` / ``episode`` must be given — without an
    anchor there's no principled cluster to surface.

    Args:
        query: Topic to consolidate around. None when episode-scoping.
        episode: Restrict to this episode id.
        sources / tags: Same semantics as ``memory_search``.
        top_k: Candidate pool size before clustering. Default 20.
        min_cohesion: Minimum cosine between seed and cluster member.
            Default 0.6. Lower to surface looser groups (noisier);
            raise to only flag near-duplicates.
        min_cluster_size: Drop clusters smaller than this. Default 2.
        max_clusters: Hard cap on returned clusters. Default 10.

    Returns:
        ``{"query": str|None, "episode": str|None, "count": int,
        "clusters": [{"cohesion": float, "seed_score": float,
        "size": int, "members": [<entry>...]}, ...]}``. Members are
        ordered highest-relevance first.
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
    """Atomic supersede-and-store — replace a cluster with one canonical note.

    Pair with ``memory_consolidation_candidates``: read a cluster's
    members, decide on a synthesis, then call this with the cluster's
    texts in ``replaces`` and your synthesis in ``new_text``. Every
    entry matching one of ``replaces`` (exact text first, embedding
    fallback for paraphrases) gets marked superseded by ``new_text``;
    the synthesis is then stored as a fresh memory.

    Supersession is preserved so retrieval still surfaces the
    historical predecessors with the consolidated note as their
    successor — *the memory bank gets shorter without losing the
    audit trail*.

    Args:
        replaces: Texts (or near-paraphrases) of the entries to retire.
            At least one is required.
        new_text: The consolidated synthesis. Stored as a fresh memory.
        source: Defaults to ``"consolidation"`` for audit clarity.
        tags: Optional tags on the new entry — ``["consolidated"]``
            makes consolidations easy to find later.

    Returns:
        ``{"superseded_count": int, "superseded_texts": [...],
        "new_memory_stored": bool, "new_memory_surprise": float}``.
    """
    return service.consolidate(
        replaces=replaces, new_text=new_text, source=source, tags=tags,
    )


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
    """Assert a typed relation between two entities (upsert a graph edge).

    Use when two things are durably connected: ``("web-app", "runs-on",
    "host-1")``, ``("pseudolife-mcp", "stores-data-in", "postgres")``.
    Entities are auto-created (and resolved through aliases — see
    ``memory_alias``); re-asserting an existing edge bumps its confidence.
    Edges are additive — remove a wrong one with ``memory_graph_unrelate``.

    The relation must come from the closed registry (builtins:
    ``depends-on``, ``part-of``, ``runs-on``, ``hosts``, ``uses``,
    ``configures``, ``stores-data-in``, ``related-to``). Common variants
    normalize automatically (``depends_on`` → ``depends-on``); a truly
    unknown name is rejected WITH the top-3 closest matches — pick one,
    fall back to ``related-to``, or (deliberately) grow the vocabulary via
    ``memory_relation_define``.

    Args:
        src: Subject entity, e.g. ``"web-app"``.
        relation: Registry relation name (separator-insensitive).
        dst: Object entity, e.g. ``"host-1"``.
        origin: ``"user"`` / ``"action"`` / ``"agent"`` — who asserts it.
        confidence: 0..1, default 0.8.
        src_type / dst_type: Optional soft type hints for the entities
            (e.g. ``"service"``, ``"host"``). First hint wins; a mismatch
            against the relation's expected types WARNS but still stores.

    Returns:
        ``{"src", "relation", "dst", "confidence", "warnings": [...]}`` on
        success; ``{"error": "unknown_relation", "suggestions": [...]}``
        on a vocabulary miss.
    """
    return service.graph_relate(
        src=src, relation=relation, dst=dst, origin=origin,
        confidence=confidence, src_type=src_type, dst_type=dst_type,
    )


@_tool()
def memory_graph_unrelate(src: str, relation: str, dst: str) -> dict[str, Any]:
    """Retract a relation — mark the edge superseded (kept for audit).

    The edge disappears from ``memory_graph`` results but stays in the
    table with a supersession timestamp. Re-asserting the same triple
    later revives it.

    Returns:
        ``{"removed": bool, "src", "relation", "dst"}``, or
        ``{"removed": False, "reason": "unknown_entity", ...}`` when a
        named entity doesn't exist.
    """
    return service.graph_unrelate(src=src, relation=relation, dst=dst)


@_tool()
def memory_alias(entity: str, alias: str) -> dict[str, Any]:
    """Bind an alternative name to an entity (e.g. ``pg`` → ``postgres``).

    Every fact / graph lookup resolves aliases first, so facts stored
    under either name land on the same node. The entity is auto-created
    if it doesn't exist yet.

    Returns:
        ``{"entity", "canonical", "aliases": [...]}`` (the full alias list
        after the bind).
    """
    return service.graph_alias(entity=entity, alias=alias)


@_tool()
def memory_digest() -> dict[str, Any]:
    """Topology digest of the knowledge graph as of the last dream: most-connected
    entities (god-nodes), surprising cross-community connections, and questions
    the graph is uniquely positioned to answer. Read-only; returns
    {available: false} until a dream has produced one.
    """
    return service.graph_digest()


@_tool()
def memory_communities(community_id: int | None = None) -> dict[str, Any]:
    """List the graph's communities (clusters of related entities) with size and
    cohesion, or — given a community_id — the members of that community. Read-only.
    """
    return service.communities(community_id=community_id)


@_tool()
def memory_briefing(max_unsure: int = 3, max_lessons: int = 3) -> dict[str, Any]:
    """Session-start briefing: what your memory is unsure about (surprising graph
    links + open questions) plus lessons from past work (avoid / prefer). Pull this
    at the start of a task. Read-only; `available: false` + empty `markdown` on a
    cold bank (no dream digest, no lessons yet). `max_unsure` caps surprising links
    AND open questions at that many EACH; `max_lessons` caps lessons (avoid-first)."""
    return service.session_briefing(max_unsure=max_unsure, max_lessons=max_lessons)


@_tool(core=True)
def memory_graph(
    entity: str,
    depth: int = 1,
    include_facts: bool = True,
    to: str | None = None,
    relation_filter: str | None = None,
) -> dict[str, Any]:
    """Read an entity's neighborhood: nodes, typed edges, canonical facts.

    The server does the multi-hop reasoning for you: transitive relations
    (``depends-on``, ``part-of``) arrive pre-closed and inverse pairs
    (``runs-on``/``hosts``) pre-mirrored — derived edges are marked
    ``derived: true`` with rule provenance (``via: ["transitive:depends-on"]``)
    so conclusions read as plain facts.

    Args:
        entity: Root entity (alias-aware).
        depth: Hops from the root, capped at 3. Default 1.
        include_facts: Attach each node's current canonical facts
            (``attribute`` / ``value`` / ``origin`` / ``confidence``).
        to: Optional second entity — the shortest path between the two is
            returned under ``paths`` (and its nodes are included even when
            beyond ``depth``).
        relation_filter: Optional case-insensitive substring; keep only edges
            whose relation contains it (e.g. "runs-on"). Replaces the former
            get_neighbors convenience tool.

    Returns:
        ``{"found": bool, "entity", "depth", "nodes": [{entity, canonical,
        etype, aliases, facts}], "edges": [{src, relation, dst, derived,
        confidence|via}], "paths": [[entity, ...]]}``.
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


@_tool()
def memory_path(source: str, target: str, max_hops: int = 8) -> dict[str, Any]:
    """Shortest path between two entities — how ``source`` connects to
    ``target``. Returns the entity chain and the typed edges along it, or an
    empty path when none exists within ``max_hops``. Read-only.
    """
    return service.graph_path(source, target, max_hops=max_hops)


@_tool(core=True)
def memory_recall(query: str, hops: int = 3, top_k: int = 5) -> dict[str, Any]:
    """Multi-hop retrieval: follow the knowledge graph to answer RELATIONAL
    questions that single-shot ``memory_search`` can't (it returns flat
    similarity, not chains).

    Use ``memory_recall`` for questions whose answer is reached by following
    links — "what does X ultimately run on?", "where does Y's data end up?",
    "what is X connected to?", "how does A reach C?". Use ``memory_search`` for
    direct lookups ("what is X's port?").

    It searches for a seed entity, walks its graph neighbourhood one hop per
    iteration (up to ``hops``, max 5), and gathers the bridging entities, facts,
    edges, and paths. Read-only — it never writes.

    Returns ``seeds``, ``entities`` (each with current facts), ``edges`` (with a
    ``derived`` flag for inferred transitive/inverse links), ``paths``, the
    supporting ``texts``, and ``iterations``. ``low_confidence: true`` means no
    seed entity matched the query — fall back to ``memory_search``.

    Args:
        query: A natural-language relational question.
        hops: Max graph hops / iterations (default 3, capped at 5).
        top_k: Results per internal search (default 5).
    """
    return service.recall(query, hops=hops, top_k=top_k)


@_tool()
def memory_relation_define(
    name: str,
    description: str,
    transitive: bool = False,
    inverse_of: str | None = None,
    src_type: str | None = None,
    dst_type: str | None = None,
) -> dict[str, Any]:
    """Add a relation to the closed vocabulary — a deliberate act.

    The registry exists to stop relation-name drift (``depends_on`` /
    ``dependsOn`` / ``uses`` fragmenting the graph), so define sparingly:
    prefer the builtins, and reach for this only when a recurring
    connection genuinely fits none of them. Builtins cannot be redefined.

    Args:
        name: Registry name (normalized: lowercase, separators → ``-``).
        description: One line on what the relation means.
        transitive: When True, ``memory_graph`` computes the closure on
            read (A→B→C implies A→C, marked derived).
        inverse_of: Existing relation this mirrors (``runs-on`` ↔
            ``hosts``); derived inverse edges appear on read.
        src_type / dst_type: Soft expected entity types — mismatches warn
            on ``memory_graph_relate`` but never reject.

    Returns:
        ``{"defined": str, "transitive", "inverse_of", "src_type",
        "dst_type"}`` or a structured error.
    """
    return service.relation_define(
        name=name, description=description, transitive=transitive,
        inverse_of=inverse_of, src_type=src_type, dst_type=dst_type,
    )


# Deep dream — full-corpus graph consolidation.
@_tool()
def memory_deep_dream(apply: bool = False) -> dict[str, Any]:
    """Manual full-corpus GRAPH consolidation (Phase-2 'C'). dry-run (default,
    apply=False) returns a preview — would-supersede / would-merge sets, re-score
    count, and semantic link CANDIDATES (with context snippets) — and writes
    nothing. apply=True commits the re-score and the provably-safe self-clean
    (supersede hard type-violations + merge exact duplicates).

    BACKUP FIRST before apply=True (ops/backup.ps1 on the host). After apply,
    drive Step C from this session: dispatch subagents over `candidates` to
    propose typed relations, then post survivors with memory_graph_propose_links;
    confirm them per-item in the Atlas Review queue (proposed_link findings).

    Returns: dry-run -> {dry_run, rescored, would_supersede, would_merge,
    candidates, totals}; apply -> {applied, rescored, superseded, merged,
    candidates, totals}."""
    return service.deep_dream(apply=apply)


# Deep dream — link proposals.
@_tool()
def memory_graph_propose_links(proposals: list[dict]) -> dict[str, Any]:
    """Ingest deep-dream Step-C link proposals (each {src, relation, dst,
    similarity?, rationale?}). Gated by edge_confidence; stored in edge_proposals
    (NOT edges) for review in Atlas. Returns {proposed, skipped}."""
    return service.graph_propose_links(proposals)


@_tool()
def memory_graph_accept_proposal(proposal_id: int) -> dict[str, Any]:
    """Promote a pending edge proposal to a real edge (origin=agent). Returns
    {accepted, src, relation, dst}."""
    return service.graph_accept_proposal(proposal_id)


@_tool()
def memory_graph_reject_proposal(proposal_id: int) -> dict[str, Any]:
    """Reject a pending edge proposal (kept for audit). Returns {rejected}."""
    return service.graph_reject_proposal(proposal_id)


@_tool(core=True)
def document_ingest(path: str, source: str | None = None) -> dict[str, Any]:
    """Read a file (.txt / .md / .pdf) and index it in the reference bank.

    The reference bank is separate from neural memory: it stores full
    documents chunked by token-approximate character windows and
    retrieves by pure cosine similarity (no gradient updates). Use it
    for background knowledge — codebases you want to ask questions
    about, PDFs of papers, reference manuals.

    Args:
        path: Absolute path on the user's filesystem.
        source: Display name. Defaults to the filename.

    Returns:
        ``{"source": str, "chunks_stored": int, "chunks_total": int}``.
    """
    return service.ingest_document(path=path, source=source)


@_tool(core=True)
def document_search(query: str, top_k: int = 5) -> dict[str, Any]:
    """RAG search over the reference bank only — no neural memories.

    When you've ingested a document corpus and want pure-document
    answers without conversational memory mixed in. For "what does the
    user know AND what's in the docs", use ``memory_search`` instead —
    it returns both pools merged.
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
