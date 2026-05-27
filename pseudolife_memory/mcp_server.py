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
) -> dict[str, Any]:
    """Store a fact, observation, or decision in neural memory.

    Use proactively when you learn something durable: a user preference,
    a project decision, an outcome, a correction. Don't store fleeting
    chatter — the surprise gate already drops near-duplicates, but
    treating ``memory_store`` as "log everything" wastes your context
    on retrieval.

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

    Returns:
        ``{"stored": bool, "surprise": float, "reason": str|None}``.
        ``stored=False`` with ``reason="below_surprise_threshold"``
        means the bank already knows this — that's a feature, not an
        error. ``reason="filtered_meta"`` means the text looked like a
        self-referential statement about the memory system itself.
    """
    return service.store(text=text, source=source, tags=tags)


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
        ``{"query": str, "count": int, "entries": [<entry>...]}``.
        Each entry includes text, source, bank, timestamp, score,
        episode_id / episode_title (or null), tags, and a ``superseded``
        flag — when ``True``, prefer the entry's ``superseded_by_text``
        over its own text.
    """
    return service.search(
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


def main() -> None:
    """Console-script entrypoint. Starts the stdio transport."""
    logger.info(
        "PseudoLife-MCP starting (data_dir=%s, config=%s)",
        service.data_dir,
        _config_path or "<defaults>",
    )
    mcp.run()  # stdio transport by default.


if __name__ == "__main__":
    main()
