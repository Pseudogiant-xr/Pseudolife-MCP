"""MCP server entrypoint — exposes PseudoLife memory tools over stdio.

Built on the FastMCP decorator API from the official ``mcp`` Python SDK.
Each ``@mcp.tool()`` becomes a JSON-RPC tool Claude Code can call:

    memory_store              Remember a fact, decision, observation
    memory_search             Retrieve by associative similarity
    memory_recent             List most-recently-stored memories
    memory_supersede          Mark an old fact obsolete + store correction
    memory_stats              Bank sizes, hit rates, totals
    memory_save               Flush CMS tensors to disk
    document_ingest           Add a file (txt/md/pdf) to the reference bank
    document_search           RAG search the reference bank only

Configuration
-------------
* ``PSEUDOLIFE_MCP_DATA_DIR`` — where memory tensors + ChromaDB live.
  Defaults to ``./data`` relative to the cwd when Claude Code launches
  the server. **Recommended: set this explicitly** in your ``.mcp.json``
  so the data path is stable regardless of which directory you start
  Claude Code from.
* ``PSEUDOLIFE_MCP_CONFIG`` — path to a ``config.yaml``. Optional; sane
  defaults are baked in by :class:`MemoryService`.

Transport: stdio. The MCP client (Claude Code) launches this script as
a subprocess and communicates via stdin/stdout — no network port, no
auth, single-tenant per process. That's exactly the threat model we
want for "Claude's personal memory on the user's PC".
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


# ── Tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
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

    Slot-shaped facts in ``text`` are auto-promoted into the canonical
    **cortex** layer when they match the deterministic extractor:
    ``<entity> <attribute> is <value>`` where the attribute is a known
    dev word (port / version / host / branch / default timeout / …),
    ``my <attr> is <value>``, ``<Entity>'s <attr> is <value>``, or a
    single-line ``<entity> <attr>: <value>``. Name the entity explicitly
    — "the default branch is master" is skipped as entity-less. For
    anything the extractor misses, ``memory_fact_set`` is the deliberate
    path. See ``memory_fact_get`` / ``memory_fact_set``.

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


@mcp.tool()
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

    Returns:
        ``{"query": str, "count": int, "entries": [<entry>...], "cortex":
        [<fact>...]}``. ``entries`` are associative recall hits (text, source,
        bank, timestamp, score, episode, tags, ``superseded`` flag — when True
        prefer ``superseded_by_text``). ``cortex`` (when present) lists canonical
        facts for the query — ``{entity, attribute, value, origin, confidence,
        score}`` — surfaced AHEAD of recall because they're the current,
        deduped answer; ``origin`` says whether the user stated it, a tool
        confirmed it, or you concluded it (treat ``agent`` as revisable).
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
        facts = service.cortex_search(query, top_k=5, min_score=0.3).get("entries", [])
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
    return result


@mcp.tool()
def memory_trace(
    query: str,
    top_k: int = 8,
    sources: list[str] | None = None,
    bands: list[str] | None = None,
    episodes: list[str] | None = None,
    tags: list[str] | None = None,
    rerank: bool | None = None,
    bm25: bool | None = None,
) -> dict[str, Any]:
    """Search + structured ranking trace — debug why an entry didn't surface.

    Same envelope as ``memory_search`` but also returns a ``trace`` dict
    showing exactly what the ranking pipeline did: per-tier candidates
    with raw_score, recency, source/supersession multipliers, and the
    ``drop_reason`` (or ``kept=True``) for each. Use when retrieval
    feels wrong and you want to see why — "Was the entry filtered by
    source? Below the relevance threshold? Outranked by a popular entry
    in a deeper band?".

    Args:
        query, top_k, sources, bands: Same semantics as ``memory_search``.
        rerank: Override the reranker flag — same semantics as
            ``memory_search.rerank``. When True, the trace's ``reranker``
            block includes per-candidate ``original_score``, ``ce_score``,
            and ``fused_score`` so you can see exactly how the
            cross-encoder reshuffled the bi-encoder ordering.
        bm25: Override the BM25 flag — same semantics as
            ``memory_search.bm25``. When True, the trace's ``bm25``
            block records raw + normalised scores per hit, any
            BM25-only injections, and the candidate-pool size.

    Returns:
        ``{"query", "count", "entries", "trace"}``. The trace contains
        ``config``, ``filters``, ``tiers`` (per-band candidate breakdown),
        ``chain_residual``, ``bm25``, ``reference_pool``, ``reranker``,
        and ``final_topk``.
    """
    return service.trace(
        query=query, top_k=top_k, sources=sources, bands=bands,
        episodes=episodes, tags=tags,
        rerank=rerank, bm25=bm25,
    )


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def memory_world_facts(limit: int = 120) -> dict[str, Any]:
    """List all current WORLD facts (introspection / audit of the world cortex).

    Returns: ``{"count": int, "entries": [...]}`` sorted by (entity, attribute),
    each with its citation + decayed effective_confidence + stale flag.
    """
    dump = service.world_dump()
    entries = dump.get("entries", [])[: max(0, int(limit))]
    return {"count": len(entries), "entries": entries}


@mcp.tool()
def memory_world_forget(entity: str, attribute: str | None = None) -> dict[str, Any]:
    """Hard-delete WORLD fact(s) — a whole entity or one slot (cleanup; no audit
    trail). Only touches the world cortex, never the user/project facts.

    Returns: ``{"removed": int, "entity": str, "attribute": str|null}``.
    """
    return service.world_forget(entity, attribute)


@mcp.tool()
def memory_dream_status() -> dict[str, Any]:
    """Read-only: how much unconsolidated memory is waiting for a dream.

    Returns ``{backlog, idle_seconds, dream_cursor, would_fire}``. Safe to call
    from a SessionStart hook to decide whether to nudge a ``/dream``.
    """
    return service.dream_status()


@mcp.tool()
def memory_dream_pull(limit: int = 40) -> dict[str, Any]:
    """Eligible memories not yet consolidated (timestamp > dream_cursor),
    oldest-first. The agent reads these, extracts canonical facts, writes them
    with ``memory_fact_set``, then calls ``memory_dream_commit``.

    Returns ``{cursor, count, entries:[{text, timestamp, episode_id}, ...]}``.
    """
    return service.dream_pull(limit=limit)


@mcp.tool()
def memory_dream_commit(cursor: float) -> dict[str, Any]:
    """Advance the dream cursor (monotonic) after consolidating up to ``cursor``
    (the newest timestamp from the pull). Returns ``{dream_cursor}``.
    """
    return service.dream_commit(cursor)


@mcp.tool()
def memory_dream_run() -> dict[str, Any]:
    """Run one server-side dream with the regex floor (Tier 0, no LLM): pull ->
    extract -> fact_set -> commit. For higher quality, the agent should instead
    use ``memory_dream_pull`` + ``memory_fact_set`` (the ``/dream`` command).

    Returns ``{pulled, claims, inserted, confirmed, contested, superseded,
    cursor}``.
    """
    from pseudolife_memory.memory.dream import RegexExtractor
    return service.dream_run(RegexExtractor())


@mcp.tool()
def memory_list_sources() -> dict[str, Any]:
    """Enumerate every source tag in the bank, with entry counts.

    Use before ``memory_search``, ``memory_recent``, or ``memory_delete``
    to discover what tags actually exist instead of guessing. Sorted by
    count descending.

    Returns:
        ``{"sources": [{"source": str, "count": int}, ...], "total": N}``.
    """
    return service.list_sources()


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def memory_stats() -> dict[str, Any]:
    """Per-band sizes, capacities, hit rates, totals.

    Useful for "how much have I remembered about X?" or for diagnosing
    why retrieval feels off (low hit rate on a deep tier means
    promotion isn't happening as expected).
    """
    return service.stats()


@mcp.tool()
def memory_save() -> dict[str, Any]:
    """Flush CMS tensors to disk now.

    Stores are persisted lazily — call this explicitly when you want a
    snapshot, or at the end of a long working session. ChromaDB (the
    reference bank) persists itself transparently, so this only
    matters for the neural bands.
    """
    return service.save()


@mcp.tool()
def memory_episode_start(
    title: str, hint: str | None = None,
) -> dict[str, Any]:
    """Open a new episode — a bracketed working session.

    Every memory stored while the episode is open carries the episode
    id + title automatically, enabling later queries like
    ``memory_search(..., episodes=[id])`` ("what did we work on in this
    session?") and ``memory_episode_summary(id)`` for a structured
    rundown.

    If another episode is already open, this auto-closes it (with a
    ``closed_by_new_start=True`` flag on the closed one) before opening
    the new episode — graceful degradation when ``memory_episode_end``
    was forgotten.

    Args:
        title: Human label for the episode. Surfaces in retrieval
            responses and in ``memory_episode_list``.
        hint: Optional longer descriptor / goal.

    Returns:
        ``{"id": str, "title": str, "started_at": float, "ended_at":
        None, "hint": str|None, "closed_by_new_start": bool}``.
    """
    return service.episode_start(title=title, hint=hint)


@mcp.tool()
def memory_episode_end() -> dict[str, Any]:
    """Close the currently-open episode.

    Returns the closed episode dict (with ``ended_at`` set), or an
    empty dict when no episode is open. Stores made after this call
    will have ``episode_id=None`` until you call
    ``memory_episode_start`` again.
    """
    return service.episode_end()


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def memory_list_tags() -> dict[str, Any]:
    """Enumerate every tag in the bank, with occurrence counts.

    Sister tool to ``memory_list_sources``. Run before
    ``memory_search(..., tags=[...])`` to discover tags Claude has
    actually stored rather than guessing. Sorted by count descending;
    ``total`` counts occurrences (an entry with two tags counts as 2).
    """
    return service.list_tags()


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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

    Use when two things are durably connected: ``("redacted", "runs-on",
    "agent-box")``, ``("pseudolife-mcp", "stores-data-in", "postgres")``.
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
        src: Subject entity, e.g. ``"redacted"``.
        relation: Registry relation name (separator-insensitive).
        dst: Object entity, e.g. ``"agent-box"``.
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


@mcp.tool()
def memory_graph_unrelate(src: str, relation: str, dst: str) -> dict[str, Any]:
    """Retract a relation — mark the edge superseded (kept for audit).

    The edge disappears from ``memory_graph`` / Cypher results but stays
    in the table with a supersession timestamp. Re-asserting the same
    triple later revives it.

    Returns:
        ``{"removed": bool, "src", "relation", "dst"}``, or
        ``{"removed": False, "reason": "unknown_entity", ...}`` when a
        named entity doesn't exist.
    """
    return service.graph_unrelate(src=src, relation=relation, dst=dst)


@mcp.tool()
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


@mcp.tool()
def memory_graph(
    entity: str,
    depth: int = 1,
    include_facts: bool = True,
    to: str | None = None,
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

    Returns:
        ``{"found": bool, "entity", "depth", "nodes": [{entity, canonical,
        etype, aliases, facts}], "edges": [{src, relation, dst, derived,
        confidence|via}], "paths": [[entity, ...]]}``.
    """
    return service.graph_neighborhood(
        entity=entity, depth=depth, include_facts=include_facts, to=to,
    )


@mcp.tool()
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


@mcp.tool()
def memory_graph_query(cypher: str, limit: int = 50) -> dict[str, Any]:
    """Read-only openCypher over the knowledge graph (requires AGE).

    Power tool for queries ``memory_graph`` can't express — aggregation,
    multi-relation patterns, degree counts. Vertices carry label
    ``Entity`` with properties ``canonical`` / ``display`` / ``etype``;
    edge labels are the relation names with ``-`` folded to ``_``
    (``depends-on`` → ``depends_on``). Derived/inferred edges are NOT in
    the mirror — only asserted ones; use ``memory_graph`` when you need
    inference.

    Example: ``MATCH (a:Entity)-[:depends_on]->(b:Entity) RETURN
    a.display, b.display``.

    Args:
        cypher: A read-only query. Mutating clauses (CREATE / MERGE / SET
            / DELETE / REMOVE / DROP) are rejected — mutate via
            ``memory_graph_relate``.
        limit: Max rows, default 50.

    Returns:
        ``{"count": int, "rows": [[col, ...], ...]}`` (agtype values as
        strings), or ``{"error": "age_unavailable"}`` when the extension
        isn't installed.
    """
    return service.graph_cypher(cypher=cypher, limit=limit)


@mcp.tool()
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


@mcp.tool()
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
