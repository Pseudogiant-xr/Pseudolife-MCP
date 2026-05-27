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
def memory_store(text: str, source: str = "claude") -> dict[str, Any]:
    """Store a fact, observation, or decision in neural memory.

    Use proactively when you learn something durable: a user preference,
    a project decision, an outcome, a correction. Don't store fleeting
    chatter — the surprise gate already drops near-duplicates, but
    treating ``memory_store`` as "log everything" wastes your context
    on retrieval.

    Args:
        text: The fact to remember. One claim per call works best.
        source: Free-form tag for filtering ("pseudolife", "general",
            "v0.7.6"). Default "claude". Pass a stable tag per
            project/topic so ``memory_search`` can scope its results.

    Returns:
        ``{"stored": bool, "surprise": float, "reason": str|None}``.
        ``stored=False`` with ``reason="below_surprise_threshold"``
        means the bank already knows this — that's a feature, not an
        error. ``reason="filtered_meta"`` means the text looked like a
        self-referential statement about the memory system itself.
    """
    return service.store(text=text, source=source)


@mcp.tool()
def memory_search(
    query: str,
    top_k: int = 8,
    sources: list[str] | None = None,
    bands: list[str] | None = None,
    min_score: float | None = None,
    disable_recency_boost: bool = False,
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

    Returns:
        ``{"query": str, "count": int, "entries": [<entry>...]}``.
        Each entry includes text, source, bank, timestamp, score, and
        a ``superseded`` flag — when ``True``, prefer the entry's
        ``superseded_by_text`` over its own text.
    """
    return service.search(
        query=query,
        top_k=top_k,
        sources=sources,
        bands=bands,
        min_score=min_score,
        disable_recency_boost=disable_recency_boost,
    )


@mcp.tool()
def memory_trace(
    query: str,
    top_k: int = 8,
    sources: list[str] | None = None,
    bands: list[str] | None = None,
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

    Returns:
        ``{"query", "count", "entries", "trace"}``. The trace contains
        ``config``, ``filters``, ``tiers`` (per-band candidate breakdown),
        ``chain_residual``, ``reference_pool``, and ``final_topk``.
    """
    return service.trace(
        query=query, top_k=top_k, sources=sources, bands=bands,
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
) -> dict[str, Any]:
    """Remove memories matching any of the provided filters.

    Hygiene tool — call when you stored junk during testing, when a
    source tag accumulated noise, or when a specific wrong fact needs
    to be purged outright. (For "this is now wrong, but keep the
    history" use ``memory_supersede`` instead.)

    At least one filter is required; bare ``memory_delete()`` returns
    an error so accidental wholesale deletion is impossible. Filters
    combine with OR (an entry matching any filter is removed).

    Args:
        text: Exact text match.
        substring: Remove any entry whose text contains this substring.
        source: Remove every entry tagged with this source.

    Returns:
        ``{"deleted_count": N, "deleted_texts": [...]}``. The texts
        list is capped at 20 to keep responses small on large purges.
    """
    return service.delete(text=text, substring=substring, source=source)


@mcp.tool()
def memory_recent(
    n: int = 10, sources: list[str] | None = None,
) -> dict[str, Any]:
    """List the N most recently stored memories, newest first.

    Unlike ``memory_search``, this is timestamp-ordered — useful for
    debugging ("what did I just store?") or for catching up at the
    start of a session ("what happened recently?").

    Args:
        n: How many to return. Default 10.
        sources: Optional source-tag filter.
    """
    return service.recent(n=n, sources=sources)


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
