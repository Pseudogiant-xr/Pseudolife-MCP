"""MCP tool surface — exposes the Pseudolife memory tools to MCP clients.

Built on the FastMCP decorator API from the official ``mcp`` Python SDK.
Each ``@_tool()`` becomes a JSON-RPC tool. The surface (consolidated
2026-07-02, 55 → 32 tools; 35 as of v26's set-slot pair) spans the associative stream (``memory_store`` /
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
  v0.1 file-only mode — except under ``pseudolife-mcp serve``, where the
  daemon first tries the ``[lite]`` embedded Postgres and exports the
  resolved DSN through this same variable (see
  :mod:`pseudolife_memory.storage.embedded_pg`;
  ``PSEUDOLIFE_MCP_STORAGE=files`` opts out).
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
from datetime import datetime
from typing import Annotated, Any, Literal

# Silence torch._dynamo's noisy fall-back warnings on systems without
# Triton (i.e. every Windows install). The embedder forward pass works
# fine in eager — dynamo just tries to compile and gives up loudly.
# Setting TORCHDYNAMO_DISABLE before any torch import is enough; don't
# also touch TORCH_LOGS (an empty value crashes torch's log initialiser).
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

from anyio import to_thread  # noqa: E402
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
# ``Annotated[T, Field(description=...)]`` on a tool signature is how a
# per-argument contract reaches the client: FastMCP builds each tool's
# inputSchema from a pydantic model derived from the signature
# (mcp/server/fastmcp/utilities/func_metadata.py), so the Field description
# lands in ``inputSchema.properties[arg].description``. Defaults stay plain
# signature defaults — Field carries description ONLY, so coercion and
# optionality are untouched.
from pydantic import Field  # noqa: E402

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

_MCP_INSTRUCTIONS = """Pseudolife is durable memory shared across sessions. At task start, call memory_search for relevant context and memory_lesson_search for prior outcomes. Store durable facts, decisions, corrections, and useful observations with memory_store (one claim per call); use memory_fact_set for canonical current values. At task end, record success, failure, or correction with memory_outcome. Use memory_toolset(action="expand") before calling tools outside the visible tier."""


def transport_security_for(auth_configured: bool) -> TransportSecuritySettings:
    """DNS-rebinding policy for the streamable-HTTP endpoint (``/mcp``).

    Keyed on whether a bearer token gates the endpoint — deliberately NOT on
    the bind host, and never left to the SDK's own heuristic. FastMCP's
    constructor auto-enables protection only when handed a loopback ``host=``
    and disables it entirely otherwise; this module has never passed one, so
    both directions of that heuristic are wrong here. Inheriting the loopback
    allowlist rejects every LAN, reverse-proxy, Tailscale-name and
    compose-service-name client with ``421 Invalid Host header`` before auth
    or any handler runs (the documented LAN recipe was dead this way, while
    ``/health`` and ``/api`` kept working); forwarding the container's
    ``0.0.0.0`` instead would silently switch the protection off.

    * **Token configured** → permissive. ``Authorization`` already proves
      intent — a browser cannot attach a bearer cross-origin without a
      preflight that fails — so a Host allowlist can only break legitimate
      remote clients. Same reasoning the Console's ``_browser_gate``
      (:mod:`pseudolife_memory.web.api`) has applied to ``/api`` since the
      2026-07-02 review.
    * **No token** → loopback allowlist, protection on. The Console app
      deliberately does not run ``_browser_gate`` on ``/mcp``, so this is the
      only thing standing between a rebinding browser and an unauthenticated
      bank. The shipped container default (``0.0.0.0`` +
      ``PSEUDOLIFE_MCP_TRUST_BIND``, published to 127.0.0.1) lands here and
      keeps exactly the protection it had before this policy existed — the
      allowlist below is byte-identical to the SDK's.
    """
    if auth_configured:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*",
                         "http://[::1]:*"],
    )


mcp = FastMCP(
    "Pseudolife Memory",
    instructions=_MCP_INSTRUCTIONS,
    # Explicit, never inherited. The daemon replaces this with the
    # token-aware policy via apply_transport_security() once it knows
    # whether auth is configured; the protected variant is the safe start.
    transport_security=transport_security_for(auth_configured=False),
)


def apply_transport_security(auth_configured: bool) -> TransportSecuritySettings:
    """Install the transport-security policy on the shared FastMCP instance.

    Must run BEFORE the first ``streamable_http_app()`` call: the SDK builds
    the ``StreamableHTTPSessionManager`` lazily there and caches whatever
    settings it was handed, so a later change would be a no-op.
    """
    settings = transport_security_for(auth_configured)
    mcp.settings.transport_security = settings
    return settings


from pseudolife_memory.toolset_tiers import (
    PrincipalTierState, normalize_tier, parse_tier_map, rank as _tier_rank,
)

# Principal-scoped toolset tiers (specs 2026-07-11, re-keyed 2026-08-10).
# All tools register; the transport tools/list handler filters per
# principal (bearer-token principal, else writer id). PSEUDOLIFE_MCP_TOOLSET
# is the DEFAULT tier (was: a registration gate); PSEUDOLIFE_MCP_TIER_MAP
# maps principals (writer-id namespace) to default tiers. The Cortex
# Console is unaffected (REST calls service.*, not MCP tools).
_DEFAULT_TIER = normalize_tier(os.environ.get("PSEUDOLIFE_MCP_TOOLSET"),
                               warn_context="PSEUDOLIFE_MCP_TOOLSET")
_TIER_MAP = parse_tier_map(os.environ.get("PSEUDOLIFE_MCP_TIER_MAP"))
_PRINCIPAL_TIERS = PrincipalTierState()
_TOOL_TIERS: dict[str, str] = {}


def _visible_tool_names(tier: str) -> set[str]:
    r = _tier_rank(tier)
    return {n for n, t in _TOOL_TIERS.items() if _tier_rank(t) <= r}


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


def _tool(*, tier: str = "full"):
    """Record the tool's tier and register it (always — tiers gate
    visibility in tools/list, not existence)."""
    def deco(fn):
        _TOOL_TIERS[fn.__name__] = tier
        mcp.tool()(_async_offload(fn))
        return fn  # module attr stays the plain sync fn (tests / Console)
    return deco


# ── Associative stream ────────────────────────────────────────────────────


@_tool(tier="minimal")
def memory_store(
    text: Annotated[str, Field(
        description="The claim to remember.")],
    source: Annotated[str, Field(
        description="Stable per-project/topic tag for later filtering.")] = "agent",
    tags: Annotated[list[str] | None, Field(
        description='Optional labels, e.g. ["decision", "blocker"].')] = None,
    origin: Annotated[Literal["user", "action", "agent"] | None, Field(
        description="Who asserted the claim.")] = None,
    episode: Annotated[str | None, Field(
        description="Episode handle for attribution.")] = None,
) -> dict[str, Any]:
    """Store one durable fact, decision, or observation. Use proactively
    for anything worth keeping — one claim per call. Near-duplicates are
    dropped, not erred (``stored=False``,
    ``reason="below_surprise_threshold"``). For canonical NOW use
    ``memory_fact_set``.

    Returns: ``{stored, surprise, reason, cortex_promoted}``.
    """
    return service.store(
        text=text, source=source, tags=tags, origin=origin, episode=episode)


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


def _iso_seconds(ts: float | None) -> str | None:
    """Epoch seconds -> local ISO-8601 to the second, for in-context reading.

    Second precision so two same-day writes to rival slots are orderable —
    the case that matters is telling a stale fact from the write that should
    have replaced it. Returns None for a missing/zero stamp rather than
    inventing an epoch date.
    """
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


# ── Supersede-at-discovery affordances (2026-07-29) ──────────────────────
# A recalled fact an agent notices is wrong must get CORRECTED, not just
# narrated (outcome signal 327: a contradiction was reported in prose and
# the record left standing for the next session to re-believe). The
# briefing's TRUST ORDER instruction alone does not produce the behavior,
# so the affordance sits here, at the moment of recall: aged/stale/
# contested facts carry the exact correction call for their slot, and the
# response states the norm once. Gate: freshness.needs_correction_nudge
# (TTL/3 — the stale flag at 2×TTL fires too late for the incident shape).

CORRECTION_NOTE = (
    "Facts flagged correct_with above are aged or contested. If one "
    "contradicts what you observe, run its correction call NOW with the "
    "verified value (re-assert the same value if it checks out) — noticing "
    "without writing leaves the error for the next session.")


def _cortex_correct_with(f: dict[str, Any]) -> str | None:
    """The copy-paste correction call for a cortex fact, or None when the
    fact is fresh enough (or durable enough) not to warrant one."""
    from pseudolife_memory.memory.freshness import needs_correction_nudge
    aged = needs_correction_nudge(
        f.get("freshness_class") or "evergreen",
        f.get("last_confirmed") or f.get("asserted_at"))
    if not (f.get("contested") or f.get("stale") or aged):
        return None
    return (f"memory_fact_set(entity={f['entity']!r}, "
            f"attribute={f['attribute']!r}, "
            f"value=<the verified current value>)")


def _world_correct_with(e: dict[str, Any]) -> str | None:
    """Same affordance for a world fact — corrections route to
    ``memory_world_set`` and carry a citation."""
    from pseudolife_memory.memory.freshness import needs_correction_nudge
    aged = needs_correction_nudge(
        e.get("freshness_class") or "volatile",
        e.get("last_confirmed") or e.get("retrieved_at") or e.get("asserted_at"))
    if not (e.get("stale") or aged):
        return None
    return (f"memory_world_set(entity={e['entity']!r}, "
            f"attribute={e['attribute']!r}, "
            f"value=<the verified current value>, source_url=<citation>)")


@_tool(tier="minimal")
def memory_search(
    query: Annotated[str, Field(
        description="Natural-language description; specific beats vague.")],
    top_k: Annotated[int, Field(
        description="Max entries returned.")] = 8,
    sources: Annotated[list[str] | None, Field(
        description="Keep only entries with one of these source tags.")] = None,
    bands: Annotated[list[str] | None, Field(
        description="Keep only entries held by one of these bands.")] = None,
    episodes: Annotated[list[str] | None, Field(
        description="Keep only entries stamped with one of these episode "
                    "ids.")] = None,
    tags: Annotated[list[str] | None, Field(
        description="Keep only entries carrying one of these tags.")] = None,
    min_score: Annotated[float | None, Field(
        description="Override the 0.25 relevance floor.")] = None,
    disable_recency_boost: Annotated[bool, Field(
        description="True to score without the recency bias.")] = False,
    rerank: Annotated[bool | None, Field(
        description="Tri-state override for cross-encoder reranking "
                    "(~200ms); None follows config.")] = None,
    bm25: Annotated[bool | None, Field(
        description="Tri-state override for keyword scoring, which aids "
                    "exact-term queries; None follows config.")] = None,
    explain: Annotated[bool, Field(
        description="Attach a ranking ``trace``; implies verbose.")] = False,
    verbose: Annotated[bool, Field(
        description="Full per-entry metadata; the default is compact "
                    "``{id, text, source, tags, score}`` plus supersession "
                    "when set.")] = False,
) -> dict[str, Any]:
    """Retrieve memories for a query — associative recall plus canonical
    facts. Call at task start or when context may apply. ``cortex``
    facts arrive AHEAD of ``entries`` — the current, deduped answer
    (``contested: true`` awaits ``memory_fact_resolve``).
    ``low_confidence=True``: no confident match, prefer abstaining. On a
    superseded entry, prefer ``superseded_by_text``. Temporal cues may
    add ``events`` (oldest first). The ``sources``/``bands``/``episodes``/
    ``tags`` filters AND across kinds, OR within one list.

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
                    # Task 6: a grouped set-slot entry has no scalar
                    # ``confidence`` (it summarises many current members,
                    # each with its own) — ``.get`` degrades to ``None``
                    # rather than KeyError-ing the whole search.
                    "confidence": f.get("confidence"), "score": f.get("score"),
                    "contested": f.get("contested", False),
                    # Currency. The cortex is the layer an agent trusts most,
                    # so a stale fact here is worse than a stale entry — and
                    # supersession only fires within one (entity, attribute)
                    # slot, so the SAME fact recorded under a second entity
                    # name goes uncorrected and uncontested. Without a date
                    # the two are indistinguishable; with one the reader can
                    # tell which write is newer. (2026-07-26: a ten-day-old
                    # "(v1 prompt)" fact sat beside a fresh "v2" fact, both
                    # contested=False, and an agent picked v1.)
                    "asserted_at": _iso_seconds(f.get("asserted_at")),
                    "last_confirmed": _iso_seconds(f.get("last_confirmed")),
                    "age": f.get("age"),
                    # v23: only surfaced when the writer marked the fact
                    # transient. Evergreen (the default) decays to nothing and
                    # would just be noise on every durable fact.
                    **(
                        {"freshness_class": f.get("freshness_class"),
                         "effective_confidence": f.get("effective_confidence"),
                         "stale": f.get("stale", False)}
                        if (f.get("freshness_class") or "evergreen") != "evergreen"
                        else {}
                    ),
                    **(
                        {"contender_value": f.get("contender_value"),
                         "contender_origin": f.get("contender_origin", "")}
                        if f.get("contested") else {}
                    ),
                    # Serving-side staleness policy (memory.search.
                    # stale_policy): the fields the policy adds must survive
                    # this key re-selection, or the most-used read surface
                    # would serve the quarantine wrapper without the
                    # original value beside it.
                    **(
                        {"warning": f["warning"]}
                        if f.get("warning") else {}
                    ),
                    **(
                        {"last_known_value": f["last_known_value"]}
                        if "last_known_value" in f else {}
                    ),
                    # Supersede-at-discovery: aged/stale/contested facts
                    # carry their exact correction call (see CORRECTION_NOTE).
                    **(
                        {"correct_with": cw}
                        if (cw := _cortex_correct_with(f)) else {}
                    ),
                }
                for f in facts
            ]
            if any("correct_with" in c for c in result["cortex"]):
                result["correction_note"] = CORRECTION_NOTE
            # Dedup against the UNDERLYING value, not the served one: under
            # stale_policy="quarantine" the served value is the wrapper
            # string, and keying on it would re-expose the raw stale value
            # in the entries below the quarantined fact (2026-08-09 review
            # finding).
            fact_vals = [f.get("last_known_value", f.get("value", ""))
                         for f in facts]
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
    n: Annotated[int, Field(
        description="How many entries to return.")] = 10,
    sources: Annotated[list[str] | None, Field(
        description="Keep only entries with one of these source tags.")] = None,
    episodes: Annotated[list[str] | None, Field(
        description="Keep only entries stamped with one of these episode "
                    "ids.")] = None,
    tags: Annotated[list[str] | None, Field(
        description="Keep only entries carrying one of these tags.")] = None,
    verbose: Annotated[bool, Field(
        description="Full per-entry metadata; default entries are "
                    "compact.")] = False,
) -> dict[str, Any]:
    """List the N most recently stored memories, newest first — timestamp
    order, not relevance. Useful for "what did I just store?" and for
    catching up at the start of a session. The ``sources``/``episodes``/
    ``tags`` filters AND-combine.
    """
    result = service.recent(
        n=n, sources=sources, episodes=episodes, tags=tags,
    )
    return result if verbose else _compact_entries(result)


@_tool()
def memory_supersede(
    old_text: Annotated[str, Field(
        description="The memory now obsolete. Matched exact-text first, "
                    "then by nearest embedding — a close paraphrase "
                    "works.")],
    new_text: Annotated[str, Field(
        description="The replacement claim; stored fresh.")],
) -> dict[str, Any]:
    """Mark a stored memory obsolete and record its replacement. The old
    entry is kept but flagged superseded, so retrieval ranks the correction
    higher and shows both together.

    Returns: ``{superseded_count, superseded_texts, new_memory_stored}``.
    """
    return service.supersede(old_text=old_text, new_text=new_text)


@_tool(tier="core")
def memory_stats() -> dict[str, Any]:
    """Memory-bank vital signs: store occupancy vs capacity, hit rates,
    true-drop count, and totals. Use to gauge how much has been remembered
    or to diagnose why retrieval feels off.
    """
    return service.stats()


_TIER_ADDS = {
    "core": "graph + recall, world facts, lessons, documents, stats, "
            "episodes, memory_get/fact_resolve",
    "full": "supersede/forget/history/reinforce, recent, dream + "
            "graph-review, aliases, consolidation, relation-define",
}


async def _notify_list_changed(ctx: Context) -> bool:
    """Best-effort tools/list_changed. False when there is no live
    transport session (tests, embedded stdio) — the memory_toolset result
    names the newly visible tools, and calls are ungated regardless."""
    try:
        await ctx.session.send_tool_list_changed()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("tools/list_changed not sent: %s", exc)
        return False


async def memory_toolset(
    action: Literal["expand", "collapse", "status"],
    ctx: Context,
) -> dict[str, Any]:
    """Adjust YOUR visible toolset, one tier at a time (minimal → core →
    full; scoped to your credential/writer identity, free, instant). Core
    adds graph/recall, world facts, lessons, documents; full adds
    supersede/forget/history, dream and graph-review admin. ``status``
    reports the ladder. Expand first: clients reject hidden-tool calls.
    """
    from pseudolife_memory.toolset_tiers import TIERS, step

    principal = _tier_principal()
    default_tier = (_TIER_MAP.get((principal or "").strip().lower())
                    or _DEFAULT_TIER)
    current = _resolve_principal_tier()

    if action == "status":
        return {"current": current, "default": default_tier,
                "ladder": list(TIERS), "adds": _TIER_ADDS}

    new = step(current, +1 if action == "expand" else -1,
               floor="minimal" if action == "expand" else default_tier)
    if new == current:
        return {"changed": False, "current": current,
                "reason": ("already at full" if action == "expand"
                           else f"already at your floor ({default_tier})")}

    _PRINCIPAL_TIERS.set(principal, new)
    before, after = _visible_tool_names(current), _visible_tool_names(new)
    out: dict[str, Any] = {
        "changed": True, "current": new, "previous": current,
        "visible_tools_added": sorted(after - before),
        "visible_tools_removed": sorted(before - after),
    }
    out["list_changed_sent"] = await _notify_list_changed(ctx)
    return out


# Native async registration: the handler must touch the transport session
# (send_tool_list_changed), so it skips the _async_offload thread hop — its
# body is dict ops only and cannot block the event loop.
_TOOL_TIERS["memory_toolset"] = "minimal"
mcp.tool()(memory_toolset)


# core memory_fact_get returns source_entries ids —
# core mode must be able to dereference them.
@_tool(tier="core")
def memory_get(
    entry_id: Annotated[int, Field(
        description="A memory id, as returned by search results or by a "
                    "fact's ``source_entries``.")],
) -> dict[str, Any]:
    """Dereference a memory id to the full stored episode plus
    ``consolidated_into`` — the canonical facts it produced. Reading it
    gently reinforces it. Returns ``{found: false, faded: true}`` when the
    episode has since been forgotten.
    """
    return service.get_entry(entry_id)


@_tool()
def memory_reinforce(
    entry_id: Annotated[int, Field(
        description="The id of the memory to strengthen.")],
) -> dict[str, Any]:
    """Strengthen one memory after reading it via ``memory_get`` and finding
    it genuinely useful — a deliberate "this mattered" signal that helps it
    resist forgetting. Read first, then reinforce.
    """
    return service.reinforce(entry_id)


# ── Cortex — canonical facts ──────────────────────────────────────────────


@_tool(tier="minimal")
def memory_fact_get(
    entity: Annotated[str, Field(
        description="The slot's subject; the (entity, attribute) match "
                    "is case- and separator-insensitive.")],
    attribute: Annotated[str, Field(
        description="The slot's attribute.")],
) -> dict[str, Any]:
    """Look up the one CURRENT value at an ``(entity, attribute)`` slot.
    One value per slot. A null record means EMPTY, not unknown —
    ``memory_search`` still finds context. A set-valued slot returns
    ``{kind: "set", members, removed}`` instead — ``members: []`` means
    EMPTY too.

    Returns: ``{record | null, contenders}`` (+ ``entity_ref`` when the
    entity has a graph node). Non-empty ``contenders`` = unsettled
    conflict (see ``memory_fact_resolve``); on an empty slot,
    ``candidates`` lists nearby slots — ranked leads, not answers.
    """
    rec = service.cortex_lookup(entity, attribute)
    out = {
        "record": rec,
        "contenders": service.cortex_contenders(entity, attribute)["contenders"],
    }
    # A fully-emptied set slot (every member removed) still comes back as a
    # truthy {"kind": "set", "members": [], "removed": [...]} dict —
    # "members": [] IS the empty-slot signal (Task 5 review finding).
    # Route it through the same empty-slot paths as a scalar miss instead
    # of letting it read as a found record.
    is_empty_set = isinstance(rec, dict) and rec.get("kind") == "set" and not rec.get("members")
    if (rec is None or is_empty_set) and not out["contenders"]:
        out["candidates"] = service.cortex_candidates(entity, attribute)
    # Supersede-at-discovery: an aged/stale record carries its exact
    # correction call, and the response states the norm once.
    if rec is not None and not is_empty_set:
        cw = _cortex_correct_with(
            {**rec, "contested": bool(out["contenders"])})
        if cw:
            rec["correct_with"] = cw
            out["correction_note"] = CORRECTION_NOTE
    # Graph join: when the subject has a graph node, surface its id +
    # aliases so callers can pivot into memory_graph.
    ref = service.entity_ref(entity)
    if ref is not None:
        out["entity_ref"] = {
            "entity_id": ref["id"], "canonical": ref["canonical"],
            "etype": ref["etype"], "aliases": ref["aliases"],
        }
    return out


@_tool(tier="minimal")
def memory_fact_set(
    entity: Annotated[str, Field(
        description="The slot's subject; the (entity, attribute) match "
                    "is case- and separator-insensitive.")],
    attribute: Annotated[str, Field(
        description="The slot's attribute.")],
    value: Annotated[str, Field(
        description="The value that is canonical NOW.")],
    origin: Annotated[Literal["user", "action", "agent"] | None, Field(
        description='Who asserted it: "user" = the human told you; '
                    'otherwise "action"/"agent". Omitted records '
                    '"agent".')] = None,
    confidence: Annotated[float, Field(
        description="How sure you are, 0..1.")] = 0.8,
    episode: Annotated[str | None, Field(
        description="Episode handle for attribution.")] = None,
    freshness_class: Annotated[
        Literal["auto", "evergreen", "slow", "volatile"], Field(
            description='How fast the value rots. "auto" infers the decay '
                        "rate from the entity kind.")] = "auto",
) -> dict[str, Any]:
    """Assert a canonical fact — insert, confirm, or correct a slot.

    A new value at an existing slot supersedes the old (history kept).
    A conflicting write parks as a contender (``action="contested"``,
    winner under ``current``) — check with the human, settle via
    ``memory_fact_resolve``.

    Returns: ``{action: inserted|confirmed|superseded|contested, ...record}``.
    """
    return service.cortex_write(
        entity, attribute, value,
        confidence=confidence, support=(origin or "agent"), episode=episode,
        freshness_class=freshness_class,
    )


@_tool(tier="minimal")
def memory_set_add(
    entity: Annotated[str, Field(description="The slot's subject.")],
    attribute: Annotated[str, Field(description="The slot's attribute.")],
    member: Annotated[str, Field(
        description="The member to add or re-confirm.")],
) -> dict[str, Any]:
    """Add/confirm a member of a set-valued slot (many concurrent values,
    not one NOW value). A scalar there converts to a set on first call —
    one-way — except number-led scalars ("32", "$1,500"), which are
    protected: the add parks as a contender (action="contested", settle
    via memory_fact_resolve). Read with memory_fact_get.

    Returns: {action, entity, attribute, member, members_count}.
    """
    return service.set_add(entity, attribute, member)


@_tool(tier="minimal")
def memory_set_remove(
    entity: Annotated[str, Field(description="The slot's subject.")],
    attribute: Annotated[str, Field(description="The slot's attribute.")],
    member: Annotated[str, Field(
        description="The current member to retract.")],
) -> dict[str, Any]:
    """Retract one current set member (audit row kept). Read with
    memory_fact_get.

    Returns: {action, entity, attribute, member, members_count}.
    """
    return service.set_remove(entity, attribute, member)


@_tool(tier="core")
def memory_fact_resolve(
    entity: Annotated[str, Field(description="The contested slot's subject.")],
    attribute: Annotated[str, Field(
        description="The contested slot's attribute.")],
    accept: Annotated[bool, Field(
        description="True adopts the parked contender as the new current "
                    "value (the old value is kept as history); False "
                    "discards the contender and keeps the current "
                    "value.")],
) -> dict[str, Any]:
    """Settle a CONTESTED fact slot after checking with the human.

    Returns: ``{resolved, accepted, action, current, record}`` or
    ``{resolved: false, reason: "no_contender"}``.
    """
    return service.cortex_resolve(entity, attribute, accept)


@_tool()
def memory_history(
    entity: Annotated[str, Field(
        description="The subject whose past to read.")],
    attribute: Annotated[str | None, Field(
        description="The fact slot to read; omit for chain mode.")] = None,
    as_of: Annotated[str | float | None, Field(
        description="Slot mode only: ISO-8601 or epoch seconds; returns "
                    "only versions written by then.")] = None,
) -> dict[str, Any]:
    """Read an entity's past. SLOT mode (``attribute`` given): every
    version of that canonical fact slot, oldest→newest, each with
    writer/session, tx/valid time, and age ("what did this used to be?
    who set it?") — compaction thins chains past ~30d. CHAIN mode
    (``attribute`` omitted): the entity's dated fact/entry/edge/lesson
    events merged oldest→newest ("what led to X?").

    Returns: ``{entity, attribute, count, versions}`` (slot mode) or
    ``{found, entity, count, events}`` (chain mode).
    """
    if attribute is None:
        return service.chain(entity)
    return service.history(entity, attribute, as_of=as_of)


# ── World cortex + lessons ────────────────────────────────────────────────


@_tool(tier="core")
def memory_world_set(
    entity: Annotated[str, Field(description="The slot's subject.")],
    attribute: Annotated[str, Field(description="The slot's attribute.")],
    value: Annotated[str, Field(
        description="The externally-sourced value.")],
    source_url: Annotated[str, Field(
        description="http(s) citation URL; any other scheme is "
                    "rejected.")] = "",
    source_quote: Annotated[str, Field(
        description="The 1-2 sentences the claim was extracted from.")] = "",
    freshness_class: Annotated[
        Literal["evergreen", "slow", "volatile"], Field(
            description="Trust decay applied at read time: ``evergreen`` "
                        "never decays, ``slow`` is months, ``volatile`` is "
                        "weeks.")] = "volatile",
    confidence: Annotated[float, Field(
        description="Source confidence, 0..1.")] = 0.85,
    retrieved_at: Annotated[float | None, Field(
        description="Epoch seconds the source was fetched.")] = None,
    content_hash: Annotated[str | None, Field(
        description="Hash of the fetched source, for change detection.")] = None,
) -> dict[str, Any]:
    """Assert a canonical WORLD fact — sourced EXTERNAL knowledge (versions,
    prices, who-holds-a-role, research findings), kept separate from
    user/project facts. Route verified web/docs findings here, with the
    citation. A newer source supersedes an older value at the same slot.

    Returns: ``{action: inserted|confirmed|superseded|rejected, ...record}``.
    """
    return service.world_write(
        entity, attribute, value, confidence=confidence, source_url=source_url,
        source_quote=source_quote, freshness_class=freshness_class,
        retrieved_at=retrieved_at, content_hash=content_hash,
    )


@_tool(tier="core")
def memory_world_search(
    query: Annotated[str, Field(
        description="Natural-language description of the external fact "
                    "needed.")],
    top_k: Annotated[int, Field(description="Max entries returned.")] = 5,
    verbose: Annotated[bool, Field(
        description="Full provenance metadata; default entries are "
                    "compact.")] = False,
) -> dict[str, Any]:
    """Search current WORLD facts (sourced external knowledge) by
    similarity. Use when a task turns on an external fact your training
    data may have stale. Entries carry ``effective_confidence``
    (age-decayed), a ``stale`` flag (re-verify before relying on it), and
    their ``source_url`` / ``source_quote`` for citation.

    Returns: ``{count, entries}``.
    """
    result = service.world_search(query, top_k=top_k, min_score=0.0)
    # Supersede-at-discovery: aged/stale facts carry their exact correction
    # call, and the response states the norm once (attached before
    # compaction so both projections keep it).
    flagged = False
    for e in result.get("entries", []):
        cw = _world_correct_with(e)
        if cw:
            e["correct_with"] = cw
            flagged = True
    if flagged:
        result["correction_note"] = CORRECTION_NOTE
    if not verbose:
        result["entries"] = [_compact_world(e) for e in result.get("entries", [])]
    return result


def _compact_world(e: dict[str, Any]) -> dict[str, Any]:
    # "warning" / "last_known_value" are the stale_policy fields (demote /
    # quarantine) — the compact projection must carry them or the default
    # world surface serves the quarantine wrapper with the original value
    # destroyed (2026-08-09 review finding).
    out = {k: e[k] for k in ("entity", "attribute", "value",
                             "effective_confidence", "stale", "score",
                             "correct_with", "warning")
           if k in e}
    if "last_known_value" in e:
        out["last_known_value"] = e["last_known_value"]
    if e.get("source_url"):
        out["source_url"] = e["source_url"]
    if e.get("source_quote"):
        out["source_quote"] = e["source_quote"]
    return out


@_tool(tier="minimal")
def memory_outcome(
    task: Annotated[str, Field(
        description='Kind of task, in stable wording ("deploy engine to '
                    'host") so signals for the same work group.')],
    outcome: Annotated[Literal["success", "failure", "correction"], Field(
        description="What happened.")],
    about: Annotated[str | None, Field(
        description="The tool or approach concerned; aids traversal.")] = None,
    detail: Annotated[str | None, Field(
        description="What worked, or what the dead-end was.")] = None,
    polarity: Annotated[str | None, Field(
        description='"+" do-this or "-" avoid; usually omit — it is '
                    "inferred from the outcome.")] = None,
    episode: Annotated[str | None, Field(
        description="Episode handle for attribution.")] = None,
) -> dict[str, Any]:
    """Record a procedural outcome — what worked, failed, or was
    corrected. Dream synthesises signals into lessons surfaced next
    session; logging stops repeated mistakes.

    Returns: ``{recorded, signal_id, task, outcome}``; needs Postgres.
    """
    return service.record_outcome(
        task, outcome, about=about, detail=detail, polarity=polarity,
        episode=episode)


@_tool(tier="core")
def memory_lesson_search(
    query: Annotated[str, Field(
        description="The task at hand, described the way it would have "
                    "been logged.")],
    top_k: Annotated[int, Field(description="Max entries returned.")] = 5,
    verbose: Annotated[bool, Field(
        description="Full provenance metadata; default entries are "
                    "compact.")] = False,
) -> dict[str, Any]:
    """Search learned lessons (procedural memory) by similarity to the task
    at hand. Call at the START of a task: what worked, what to avoid, what
    the user corrected before. Heed polarity ``-`` entries — known dead-ends.

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
    scope: Annotated[Literal["memory", "fact", "world", "lesson"], Field(
        description="Which store to delete from.")],
    entity: Annotated[str | None, Field(
        description="fact/world scope: the slot's subject. lesson scope: "
                    "the task.")] = None,
    attribute: Annotated[str | None, Field(
        description="fact/world scope: one slot's attribute; omit to purge "
                    "the whole entity. lesson scope: the aspect.")] = None,
    text: Annotated[str | None, Field(
        description="memory scope: delete entries whose text matches this "
                    "exactly.")] = None,
    substring: Annotated[str | None, Field(
        description="memory scope: delete entries whose text contains "
                    "this.")] = None,
    source: Annotated[str | None, Field(
        description="memory scope: delete entries with this source "
                    "tag.")] = None,
    episode: Annotated[str | None, Field(
        description="memory scope: delete entries stamped with this "
                    "episode id.")] = None,
    tag: Annotated[str | None, Field(
        description="memory scope: delete entries carrying this tag.")] = None,
) -> dict[str, Any]:
    """Hard-delete from one memory store. Cleanup for junk/test data — no
    audit trail. For "now wrong, keep history" use ``memory_fact_set``
    (facts) or ``memory_supersede`` (memories) instead.

    ``scope="memory"`` needs at least one of ``text``/``substring``/
    ``source``/``episode``/``tag``, and those OR-combine — ANY match
    deletes, unlike memory_search's AND. The other scopes need ``entity``.

    Returns: ``{deleted_count | removed, ...}``; ``{error}`` on bad input.
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
    action: Literal["status", "pull", "commit", "run", "deep", "runs",
                    "rollback"],
    limit: Annotated[int | None, Field(
        description="pull/run: how many memories to process (pull defaults "
                    "to 40). runs: how many passes to list (defaults to "
                    "10).")] = None,
    cursor: Annotated[float | None, Field(
        description="commit: the newest pulled timestamp. Required for "
                    "that action.")] = None,
    apply: Annotated[bool, Field(
        description="deep: True writes the consolidation (graph tables are "
                    "snapshotted first); the default is a dry run.")] = False,
    snippets: Annotated[bool, Field(
        description="deep: False omits the evidence snippets.")] = True,
    run_id: Annotated[int | None, Field(
        description="rollback: which pass to revert; defaults to the "
                    "newest committed pass.")] = None,
) -> dict[str, Any]:
    """Drive the dream — consolidation of recent memories into canonical
    facts and graph structure.

    Actions:
        ``status``: backlog + whether a sweep would fire. Read-only.
        ``pull``: unconsolidated memories, oldest first; write facts via
            ``memory_fact_set``, then ``commit``.
        ``run``: a server-side dream with the configured extractor
            (loop to drain).
        ``deep``: full-corpus graph consolidation; dry run unless
        ``apply``. Settle candidates via
            ``memory_graph_review``; duplicate lesson/world slots are
            listed for hand curation.
        ``runs``: recent dream passes (tallies, status).
        ``rollback``: revert a committed pass from its journal (facts +
            events; traces/cursor kept).

    Returns: per-action dict; ``{error}`` on bad input.
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
        return service.dream_run_auto(limit=limit)
    if action == "deep":
        return service.deep_dream(apply=apply, include_snippets=snippets)
    if action == "runs":
        return service.dream_runs(limit=limit or 10)
    if action == "rollback":
        return service.dream_rollback(run_id=run_id)
    return {"error": "unknown_action",
            "actions": ["status", "pull", "commit", "run", "deep", "runs",
                        "rollback"]}


def _coerce_id_list(value: Any) -> list[int] | None:
    """Accept a real list, a JSON-encoded list (some MCP clients stringify
    untyped list params), or None."""
    if value is None:
        return None
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, (list, tuple)):
        try:
            return [int(v) for v in value]
        except (TypeError, ValueError):
            return None
    return None


@_tool()
def memory_graph_review(
    action: Literal["list", "propose", "relate", "dismiss_pair",
                    "dismiss_slot_pair", "accept_link", "reject_link",
                    "accept_merge", "accept_junk", "reject_entity"] = "list",
    proposal_id: Annotated[int | None, Field(
        description="Id actions: the one proposal to settle.")] = None,
    proposal_ids: Annotated[list[int] | None, Field(
        description="Id actions: settle many proposals in one call, "
                    "instead of ``proposal_id``.")] = None,
    proposals: Annotated[list[dict] | None, Field(
        description="propose: ``[{src, relation, dst, similarity?, "
                    "rationale?}]``.")] = None,
    scope: Annotated[str | None, Field(
        description="list: keep only findings of this kind.")] = None,
    src: Annotated[str | None, Field(
        description="relate/dismiss_pair: the first entity. "
                    'dismiss_slot_pair: an "entity|attribute" key from the '
                    "deep response.")] = None,
    dst: Annotated[str | None, Field(
        description="relate/dismiss_pair: the second entity. "
                    'dismiss_slot_pair: an "entity|attribute" key from the '
                    "deep response.")] = None,
    relation: Annotated[str | None, Field(
        description="relate: the edge relation to write, from the graph "
                    "vocabulary.")] = None,
    store: Annotated[str | None, Field(
        description='dismiss_slot_pair: which duplicate listing the keys '
                    'came from — "lesson" or "world".')] = None,
) -> dict[str, Any]:
    """Work the graph review queue — deep-dream proposals that need a
    verdict before they touch the graph.

    Actions:
        ``list``: pending findings/proposals.
        ``propose``: file link proposals for review.
        ``relate``: related-not-duplicate verdict — writes the
            ``relation`` edge and dismisses the pair.
        ``dismiss_pair``: mark ``src``/``dst`` genuinely distinct.
        ``dismiss_slot_pair``: same for lesson/world duplicate listings.
        ``accept_link``/``reject_link``: settle an edge proposal by id.
        ``accept_merge``: fold a near-duplicate into its twin.
        ``accept_junk``: delete an over-extraction artifact.
        ``reject_entity``: keep the entity, dismiss the proposal.

    Returns: per-action dict; ``{error}`` on bad input.
    """
    if action == "list":
        return service.graph_review(scope=scope)
    if action == "propose":
        if not proposals:
            return {"error": "proposals_required"}
        return service.graph_propose_links(proposals)
    if action == "relate":
        if not src or not dst or not relation:
            return {"error": "src_relation_dst_required"}
        out = service.graph_relate(src, relation, dst, origin="agent")
        if out.get("error"):
            return out
        dismissed = service.graph_dismiss_duplicate(src, dst)
        return {**out, "pair_dismissed": bool(dismissed.get("dismissed"))}
    if action == "dismiss_pair":
        if not src or not dst:
            return {"error": "src_dst_required"}
        return service.graph_dismiss_duplicate(src, dst)
    if action == "dismiss_slot_pair":
        # Listed keys fold literal pipes into "-" (service._slot_key), so the
        # first "|" is always the entity/attribute boundary.
        if not store or not src or not dst or "|" not in src or "|" not in dst:
            return {"error": "store_src_dst_required",
                    "detail": "store='lesson'|'world'; src/dst are "
                              "'entity|attribute' keys from the deep response"}
        return service.curation_dismiss_duplicate(
            store, *src.split("|", 1), *dst.split("|", 1))
    handlers = {
        "accept_link": service.graph_accept_proposal,
        "reject_link": service.graph_reject_proposal,
        "accept_merge": lambda pid: service.graph_accept_entity_merge(
            pid, decided_by="agent"),
        "accept_junk": lambda pid: service.graph_accept_entity_junk(
            pid, decided_by="agent"),
        "reject_entity": lambda pid: service.graph_reject_entity_proposal(
            pid, decided_by="agent"),
    }
    handler = handlers.get(action)
    if handler is None:
        return {"error": "unknown_action",
                "actions": ["list", "propose", "relate", "dismiss_pair",
                            "dismiss_slot_pair", "accept_link", "reject_link",
                            "accept_merge", "accept_junk", "reject_entity"]}
    batch = _coerce_id_list(proposal_ids)
    if batch:
        results = [handler(pid) for pid in batch]
        # Every id handler reports success under "accepted" or "rejected";
        # a stale id yields {"...": False} (or reason=not_pending) and must
        # not count as settled.
        ok = sum(1 for r in results
                 if bool(r.get("accepted")) or bool(r.get("rejected")))
        return {"action": action, "settled": ok, "results": results}
    if proposal_id is None:
        return {"error": "proposal_id_required", "action": action}
    return handler(proposal_id)


# ── Episodes + consolidation ──────────────────────────────────────────────


@_tool(tier="core")  # the CLAUDE.md workflow opens sub-episodes for big tasks.
def memory_episode_start(
    title: Annotated[str, Field(
        description="Short name for the task, used in later recaps.")],
    hint: Annotated[str | None, Field(
        description="Optional note on what the task is about.")] = None,
) -> dict[str, Any]:
    """Open a named sub-episode for a substantial multi-step task. It nests
    under the auto-managed session episode; memories stored while it is open
    carry its id + title, enabling episode-scoped search and summaries
    later. ``memory_episode_end`` closes it and pops back to the session.

    Returns: ``{id, title, started_at, parent_id, ...}``.
    """
    return service.episode_start(title=title, hint=hint)


@_tool(tier="core")  # pairs with memory_episode_start.
def memory_episode_end() -> dict[str, Any]:
    """Close the current open episode and pop back to its parent (the
    session). Returns the closed episode dict, or ``{}`` when nothing is
    open.
    """
    return service.episode_end()


@_tool(tier="minimal")  # the recommended workflow names the session early.
def memory_session_title(
    title: Annotated[str, Field(
        description='What this session is about, e.g. "Pseudolife-MCP" or '
                    '"auth-refactor".')],
) -> dict[str, Any]:
    """Name THIS session's auto-opened episode (default titles are
    generic). Call once at the start of work so session recaps read
    meaningfully. Idempotent; call again to rename.
    """
    return service.set_session_title(title=title)


@_tool()
def memory_episode_summary(
    id: Annotated[str, Field(
        description="An episode id, as it appears on search/recent "
                    "results.")],
) -> dict[str, Any]:
    """Stats, tag/source distribution, and recent entries for one episode —
    "summarise what we worked on". Returns ``{found: false}`` for an
    unknown id.
    """
    return service.episode_summary(id=id)


@_tool()
def memory_consolidation_candidates(
    query: Annotated[str | None, Field(
        description="Topic-driven anchor: cluster memories near this "
                    "description.")] = None,
    episode: Annotated[str | None, Field(
        description="Session-driven anchor: cluster within this episode "
                    "id.")] = None,
    sources: Annotated[list[str] | None, Field(
        description="Consider only entries with one of these source "
                    "tags.")] = None,
    tags: Annotated[list[str] | None, Field(
        description="Consider only entries carrying one of these "
                    "tags.")] = None,
    top_k: Annotated[int, Field(
        description="How many candidate entries to cluster over.")] = 20,
    min_cohesion: Annotated[float, Field(
        description="Minimum intra-cluster cosine — raise it to flag only "
                    "near-duplicates.")] = 0.6,
    min_cluster_size: Annotated[int, Field(
        description="Drop clusters with fewer members than this.")] = 2,
    max_clusters: Annotated[int, Field(
        description="Max clusters returned.")] = 10,
) -> dict[str, Any]:
    """Find clusters of near-duplicate memories ripe for consolidation —
    the same thing phrased five ways across five sessions. Anchor with a
    ``query`` or an ``episode``; read the clusters, synthesise one
    canonical note, then commit it via ``memory_consolidate``.

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
    replaces: Annotated[list[str], Field(
        description="The memories being folded in; each is matched by "
                    "exact text or close paraphrase.")],
    new_text: Annotated[str, Field(
        description="The canonical note that replaces them.")],
    source: Annotated[str | None, Field(
        description="Source tag for the new note.")] = None,
    tags: Annotated[list[str] | None, Field(
        description="Labels for the new note.")] = None,
) -> dict[str, Any]:
    """Replace a cluster of near-duplicate memories with one canonical note.
    Every entry matching ``replaces`` is marked superseded by ``new_text``,
    which is stored fresh — the bank gets shorter without losing the audit
    trail.

    Returns: ``{superseded_count, superseded_texts, new_memory_stored}``.
    """
    return service.consolidate(
        replaces=replaces, new_text=new_text, source=source, tags=tags,
    )


# ── Knowledge graph ───────────────────────────────────────────────────────


@_tool(tier="core")
def memory_graph_relate(
    src: Annotated[str, Field(description="The subject entity.")],
    relation: Annotated[str, Field(
        description="From the closed registry: ``depends-on``, "
                    "``part-of``, ``runs-on``, ``hosts``, ``uses``, "
                    "``configures``, ``stores-data-in``, ``related-to``. "
                    "Separator variants normalise; an unknown name is "
                    "rejected WITH the closest matches.")],
    dst: Annotated[str, Field(description="The object entity.")],
    origin: Annotated[str | None, Field(
        description="Who asserted the edge.")] = None,
    confidence: Annotated[float, Field(
        description="How sure you are, 0..1.")] = 0.8,
    src_type: Annotated[str | None, Field(
        description="Entity kind for ``src``: set when the node is "
                    "created, or filled in on an existing node that has "
                    "none.")] = None,
    dst_type: Annotated[str | None, Field(
        description="Entity kind for ``dst``: set when the node is "
                    "created, or filled in on an existing node that has "
                    "none.")] = None,
) -> dict[str, Any]:
    """Assert a typed relation between two entities, e.g. ``("web-app",
    "runs-on", "host-1")``. Entities auto-create and resolve through
    aliases; re-asserting an edge bumps its confidence. On a rejected
    relation, pick a suggestion, fall back to ``related-to``, or grow the
    vocabulary deliberately via ``memory_relation_define``.

    Returns: ``{src, relation, dst, confidence, warnings}`` or
    ``{error: "unknown_relation", suggestions}``.
    """
    return service.graph_relate(
        src=src, relation=relation, dst=dst, origin=origin,
        confidence=confidence, src_type=src_type, dst_type=dst_type,
    )


@_tool()
def memory_graph_unrelate(
    src: Annotated[str, Field(description="The edge's subject entity.")],
    relation: Annotated[str, Field(description="The edge's relation.")],
    dst: Annotated[str, Field(description="The edge's object entity.")],
) -> dict[str, Any]:
    """Retract a relation — the edge is marked superseded (kept for audit)
    and leaves ``memory_graph`` results. Re-asserting the same triple later
    revives it.
    """
    return service.graph_unrelate(src=src, relation=relation, dst=dst)


@_tool()
def memory_alias(
    entity: Annotated[str, Field(
        description="The canonical entity to bind onto.")],
    alias: Annotated[str, Field(
        description="The alternative name.")],
) -> dict[str, Any]:
    """Bind an alternative name to an entity (e.g. ``pg`` → ``postgres``)
    so facts and graph lookups under either name land on the same node.
    Returns the entity's full alias list.
    """
    return service.graph_alias(entity=entity, alias=alias)


@_tool(tier="core")
def memory_graph(
    entity: Annotated[str, Field(
        description="The root entity to read out from.")],
    depth: Annotated[int, Field(
        description="Hops from the root. Max 3.")] = 1,
    include_facts: Annotated[bool, Field(
        description="False omits each node's canonical facts.")] = True,
    to: Annotated[str | None, Field(
        description="Also return the shortest path from ``entity`` to "
                    "this entity under ``paths``; its nodes are folded "
                    "into the neighborhood.")] = None,
    relation_filter: Annotated[str | None, Field(
        description="Keep only edges whose relation contains this "
                    "substring.")] = None,
) -> dict[str, Any]:
    """Read an entity's graph neighborhood: nodes, typed edges, and each
    node's canonical facts. Transitive/inverse edges arrive pre-derived
    (marked ``derived: true`` with rule provenance).

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


# ── Recall output caps (issue #186) ───────────────────────────────────────
# ``top_k`` bounds only the SEED search; graph expansion fans out from
# there with no bound of its own, and the compact projection memory_search
# applies to entry text was never applied on this path. Issue #186's live
# audit (2026-08-21, real daemon — not reproducible in-tree) measured
# memory_recall(query="what does the stdio shim connect to and what runs
# the MCP tools", hops=3, top_k=5, verbose=False) at 93.7 KB total, enough
# that the calling client refused it. A separate pass over that same
# response reported a per-field breakdown: 53 entities (38.6 KB), 75 edges
# (5.3 KB), 45 uncapped full entry texts (51.9 KB). Those three components
# sum to 95.8 KB, ~2% over the stated 93.7 KB total; both figures are
# reproduced here exactly as audited rather than smoothed, since that live
# response can't be re-measured without the daemon and bank it ran
# against. What IS reproducible in-tree: evals/recall_cap_probe.py builds
# a synthetic hub graph (no DB, no daemon) and records this repo's own
# pre-cap vs post-cap serialized size to
# evals/results/recall-cap-186-payload-probe.json (pinned by
# tests/test_eval_evidence.py — see docs/guide/retrieval.md for that
# number).
#
# A flat prefix slice (``out["entities"][:N]``) is NOT a relevance
# ordering: (1) run_recall appends seeds, then each hop's
# newly-discovered entities in turn, so ``entities[:N]`` is a
# breadth-first PREFIX, not a ranking; (2) within one hop, nodes come back
# sorted by internal entity id (service.py's ``graph_neighborhood``), and
# edges are the graph's global edge list filtered to that neighborhood
# (graph.py's ``build_subgraph``) — storage order, not relevance; (3)
# ``paths`` is always empty on this path (``graph_fn`` is called without
# ``to=``), so ``edges`` is the ONLY connectivity representation — a naive
# ``edges[:N]`` can be entirely consumed by the seed's own 1-hop ring,
# silently dropping the hop-2/hop-3 bridges that are the actual reason to
# call memory_recall instead of memory_search. (``_select_frontier``'s
# ascending-degree sort is real, but it decides which non-hub entities get
# EXPANDED THROUGH next during the walk — it says nothing about the order
# results are later emitted in.)
#
# The caps below instead: reserve a minimum per-hop quota for entities and
# edges (each hop present gets a floor(cap/hops-present) share, with the
# remainder handed to the LATEST hops one each so the reservation itself
# never exceeds the cap; a hop needing fewer releases its unused slots to
# hops that need more, later hops first — ``_hop_quota_select``), prefer
# edges whose src AND dst both survived the entity cap and backfill from
# the rest only if that pool falls short (``_cap_recall_edges``), and
# split the texts budget between the flat seed search and hop-discovered
# support so a hop-discovered text always survives even at top_k >= the
# texts cap (``_cap_recall_texts``). Facts per surviving entity are capped
# separately (``_RECALL_MAX_FACTS_PER_ENTITY``) — the entities that
# survive the cap are disproportionately the fact-heavy hubs, and their
# facts list was otherwise still unbounded.
_RECALL_MAX_ENTITIES = 10
_RECALL_MAX_EDGES = 15
_RECALL_MAX_TEXTS = 6
# memory_search's cortex-first block already treats 5 as the standard
# facts-serving width (this module's cortex_search(query, top_k=5, ...)
# call); reusing it here keeps one recall entity's fact list comparable to
# what search already treats as a normal-sized answer.
# evals/recall_cap_probe.py's synthetic hub entities carry more than this
# by construction, so the probe artifact also records the pre/post
# fact-count this cap produces on that fixture.
_RECALL_MAX_FACTS_PER_ENTITY = 5
# Of _RECALL_MAX_TEXTS, how many slots the flat seed search gets — the
# rest goes to hop-discovered support. min(3, top_k) so a small top_k
# doesn't reserve more than it actually returns. Without this split,
# texts[:cap] is just the seed search's own top_k window: at the default
# top_k=5 and cap=6 exactly one hop-discovered text can ever survive, and
# at top_k >= cap, zero can — every text hop expansion turns up would be
# silently invisible.
_RECALL_SEED_TEXT_BUDGET = 3
# Preview length for a supporting text once the cap above binds — same
# 80/120/200-char + ellipsis convention as the existing text_preview
# fields (cms.py, service.py).
_RECALL_TEXT_CHARS = 200


def _hop_quota_select(items: list[tuple[Any, int]], cap: int) -> list[Any]:
    """Cap a (item, hop) sequence to ``cap`` items so no single hop's ring
    can crowd out deeper hops. Reserves ``cap // H`` slots per distinct hop
    present (H = number of distinct hops), handing the ``cap % H``
    remainder to the LATEST hops one each — ``cap // H`` per bucket alone
    can undershoot ``cap`` by up to H-1 items, and a NAIVE ``ceil(cap/H)``
    per bucket overshoots it whenever every bucket is full (e.g. cap=15,
    H=2 gives 8+8=16), so the reservation itself must sum to exactly
    ``cap`` — never more, so the return value never exceeds ``cap``
    regardless of how full any bucket is. A hop that needs fewer than its
    reservation releases the unused slots to hops that need more, granting
    the LATER hops first (the ones a flat prefix cap would otherwise
    starve first). Within a hop, keeps that hop's earliest-discovered
    items (their relative order is unchanged)."""
    if cap <= 0 or not items:
        return []
    buckets: dict[int, list[Any]] = {}
    for item, hop in items:
        buckets.setdefault(hop, []).append(item)
    hop_order = sorted(buckets)
    H = len(hop_order)
    base, extra = divmod(cap, H)
    quota = {h: base for h in hop_order}
    for h in hop_order[H - extra:]:            # the last `extra` hops
        quota[h] += 1
    take = {h: min(quota[h], len(buckets[h])) for h in hop_order}
    remaining = cap - sum(take.values())
    for h in reversed(hop_order):
        if remaining <= 0:
            break
        spare = len(buckets[h]) - take[h]
        if spare <= 0:
            continue
        grant = min(spare, remaining)
        take[h] += grant
        remaining -= grant
    return [item for h in hop_order for item in buckets[h][:take[h]]]


def _cap_recall_entities(entities: list[dict],
                         entity_hop: dict[str, int]) -> list[dict]:
    tagged = [(e, entity_hop.get(e.get("entity"), 0)) for e in entities]
    return _hop_quota_select(tagged, _RECALL_MAX_ENTITIES)


def _cap_recall_edges(edges: list[dict], edge_hop: list[int],
                      surviving_entities: set[str]) -> list[dict]:
    """Prefer edges whose src AND dst both survived the entity cap (only
    those connect two entities the caller can actually see); backfill from
    the rest only if that pool doesn't fill the cap. Each pool is itself
    hop-quota'd."""
    if len(edge_hop) != len(edges):        # legacy/stub caller: no hop info
        edge_hop = [0] * len(edges)
    both: list[tuple[dict, int]] = []
    rest: list[tuple[dict, int]] = []
    for e, h in zip(edges, edge_hop):
        pool = both if (e.get("src") in surviving_entities
                        and e.get("dst") in surviving_entities) else rest
        pool.append((e, h))
    picked = _hop_quota_select(both, _RECALL_MAX_EDGES)
    if len(picked) < _RECALL_MAX_EDGES:
        picked += _hop_quota_select(rest, _RECALL_MAX_EDGES - len(picked))
    return picked


def _cap_recall_texts(texts: list[str], seed_text_count: int,
                      top_k: int) -> list[str]:
    """Split the cap between the flat seed search and hop-discovered
    support so a hop-discovered text always survives (see the module
    comment above) instead of ``texts[:cap]`` being purely the seed
    window."""
    seed_texts = texts[:seed_text_count]
    hop_texts = texts[seed_text_count:]
    seed_budget = min(_RECALL_SEED_TEXT_BUDGET, top_k, _RECALL_MAX_TEXTS)
    seed_take = min(seed_budget, len(seed_texts))
    hop_take = min(_RECALL_MAX_TEXTS - seed_take, len(hop_texts))
    short = (_RECALL_MAX_TEXTS - seed_take) - hop_take
    if short > 0:  # hop pool ran short -- backfill from unused seed texts
        seed_take = min(seed_take + short, len(seed_texts))
    return seed_texts[:seed_take] + hop_texts[:hop_take]


def _compact_recall_text(t: str) -> str:
    """Truncate one recall supporting text to the preview cap — same
    80/120/200-char + ellipsis convention as the existing text_preview
    fields (cms.py, service.py). ``_compact_entry`` itself only projects
    fields and does not truncate; this is a distinct, recall-specific
    truncation step."""
    if len(t) <= _RECALL_TEXT_CHARS:
        return t
    return t[:_RECALL_TEXT_CHARS] + "…"


@_tool(tier="core")
def memory_recall(
    query: Annotated[str, Field(
        description="Natural-language relational question; its top hits "
                    "seed the graph walk.")],
    hops: Annotated[int, Field(
        description="Max graph hops. Clamped to 1..5.")] = 3,
    top_k: Annotated[int, Field(
        description="Bounds only the SEED search — the initial hits naming "
                    "the entities the walk starts from — NOT the result, "
                    "which is capped separately. Up to 3 ``texts`` slots "
                    "go to seed hits; the rest to hop-discovered "
                    "support.")] = 5,
    verbose: Annotated[bool, Field(
        description="Full fact/edge provenance and untruncated texts. "
                    "Default facts are ``{attribute, value}``, edges "
                    "``{src, relation, dst}``, texts truncated to a "
                    "preview.")] = False,
) -> dict[str, Any]:
    """Multi-hop retrieval over the knowledge graph, for RELATIONAL
    questions whose answer is reached by following links — "what does X
    ultimately run on?", "how does A reach C?" — which single-shot
    ``memory_search`` can't chain. Read-only. ``low_confidence: true``
    means no seed entity matched — fall back to ``memory_search``.

    Returns: ``{seeds, entities, edges, paths, texts, iterations}``.
    ``entities``/``edges``/``texts`` are capped (currently 10/15/6) with a
    per-hop reservation, so a hub seed's own 1-hop ring can't crowd out
    the deeper hops the walk exists to reach; ``edges`` prefers links
    between surviving entities; each entity's ``facts`` is capped
    (currently 5). Details: docs/guide/retrieval.md.
    """
    out = service.recall(query, hops=hops, top_k=top_k)
    entity_hop = out.get("entity_hop") or {}
    seed_text_count = out.get("seed_text_count")
    if seed_text_count is None:            # legacy/stub caller: no hop info
        seed_text_count = min(top_k, len(out.get("texts", [])))

    capped_entities = _cap_recall_entities(out.get("entities", []), entity_hop)
    capped_entities = [
        {**e, "facts": (e.get("facts") or [])[:_RECALL_MAX_FACTS_PER_ENTITY]}
        for e in capped_entities
    ]
    surviving = {e.get("entity") for e in capped_entities}
    out["entities"] = capped_entities
    out["edges"] = _cap_recall_edges(
        out.get("edges", []), out.get("edge_hop", []), surviving)
    out["texts"] = _cap_recall_texts(
        out.get("texts", []), seed_text_count, top_k)
    # Internal bookkeeping only — stale once entities/edges are capped
    # above, and not part of the documented return shape.
    out.pop("entity_hop", None)
    out.pop("edge_hop", None)
    out.pop("seed_text_count", None)

    if not verbose:
        out["entities"] = [
            {"entity": n.get("entity"),
             "facts": [{"attribute": f.get("attribute"), "value": f.get("value")}
                       for f in n.get("facts", [])]}
            for n in out["entities"]
        ]
        out["edges"] = [
            {"src": e.get("src"), "relation": e.get("relation"),
             "dst": e.get("dst")}
            for e in out["edges"]
        ]
        out["texts"] = [_compact_recall_text(t) for t in out["texts"]]
    return out


@_tool()
def memory_relation_define(
    name: Annotated[str, Field(
        description="The new relation name, hyphenated like the "
                    "builtins.")],
    description: Annotated[str, Field(
        description="What the relation means, for later readers.")],
    transitive: Annotated[bool, Field(
        description="True closes the relation transitively, so chained "
                    "edges arrive pre-derived.")] = False,
    inverse_of: Annotated[str | None, Field(
        description="Pair with an existing relation, as ``runs-on`` is the "
                    "inverse of ``hosts``.")] = None,
    src_type: Annotated[str | None, Field(
        description="Soft entity-kind expectation for the subject; a "
                    "mismatch warns, never rejects.")] = None,
    dst_type: Annotated[str | None, Field(
        description="Soft entity-kind expectation for the object; a "
                    "mismatch warns, never rejects.")] = None,
) -> dict[str, Any]:
    """Add a relation to the closed graph vocabulary — a deliberate, rare
    act. Prefer the builtins; define one only when a recurring connection
    genuinely fits none of them.
    """
    return service.relation_define(
        name=name, description=description, transitive=transitive,
        inverse_of=inverse_of, src_type=src_type, dst_type=dst_type,
    )


# ── Reference bank ────────────────────────────────────────────────────────


@_tool(tier="core")
def document_ingest(
    path: Annotated[str, Field(
        description="Path to a .txt / .md / .pdf file, resolved on the "
                    "SERVER's filesystem — with the Docker daemon, a path "
                    "visible inside the container (e.g. a mounted volume), "
                    "not a host path.")],
    source: Annotated[str | None, Field(
        description="Source tag for the chunks; defaults to the "
                    "filename.")] = None,
) -> dict[str, Any]:
    """Index a file into the reference bank — a separate store for
    background documents (papers, manuals, codebases) retrieved by pure
    cosine similarity, kept apart from conversational memory.

    Returns: ``{source, chunks_stored, chunks_total}``.
    """
    return service.ingest_document(path=path, source=source)


@_tool(tier="core")
def document_search(
    query: Annotated[str, Field(
        description="Natural-language description of the passage "
                    "wanted.")],
    top_k: Annotated[int, Field(description="Max chunks returned.")] = 5,
) -> dict[str, Any]:
    """Search the reference bank only — ingested documents, no
    conversational memories mixed in. For docs AND memories together, use
    ``memory_search``.
    """
    return service.search_documents(query=query, top_k=top_k)


def _tier_principal() -> str | None:
    """Tier bucket + map key for the CURRENT request (spec 2026-08-10):
    the named principal from the bearer token, else the writer id
    (X-PL-Writer header, or the daemon default so direct-HTTP clients
    still match the tier map) — principals share the writer-id namespace.
    ``None`` (embedded stdio/tests, nothing configured) is the shared
    bucket."""
    from pseudolife_memory.principals import DEFAULT_PRINCIPAL
    from pseudolife_memory.writer_context import (
        _http_writer_session, current_principal)

    principal = current_principal()
    if principal != DEFAULT_PRINCIPAL:
        return principal
    writer, _ = _http_writer_session()
    return writer or os.environ.get("PSEUDOLIFE_WRITER_ID") or None


def _resolve_principal_tier() -> str:
    """Tier for the CURRENT request: principal override → tier map → env
    default. Safe outside a request (returns the env default)."""
    from pseudolife_memory.toolset_tiers import resolve_tier
    return resolve_tier(
        _tier_principal(),
        state=_PRINCIPAL_TIERS, tier_map=_TIER_MAP, default_tier=_DEFAULT_TIER,
    )


def _wire_transport_tiering() -> None:
    """Replace the transport tools/list handler with the tier-filtered view
    and advertise tools.listChanged (the SDK default omits it, verified on
    mcp 1.27.2). The raw handler bypasses the SDK's caching wrapper — that
    wrapper CLEARS the tool cache and refills it with whatever the handler
    returns, which would strip hidden tools of call-time validation. We
    feed the cache the full registry instead."""
    import mcp.types as mtypes
    from mcp.server.lowlevel.server import NotificationOptions

    server = mcp._mcp_server

    async def _filtered_list(_req) -> mtypes.ServerResult:
        tools = await FastMCP.list_tools(mcp)   # full registry, mcp.types.Tool
        server._tool_cache.update({t.name: t for t in tools})
        names = _visible_tool_names(_resolve_principal_tier())
        return mtypes.ServerResult(mtypes.ListToolsResult(
            tools=[t for t in tools if t.name in names]))

    server.request_handlers[mtypes.ListToolsRequest] = _filtered_list

    _orig = server.create_initialization_options

    def _init_opts(notification_options=None, experimental_capabilities=None):
        # memory_toolset depends on the listChanged capability: force it on
        # whatever options arrive rather than only defaulting the None case.
        opts = notification_options or NotificationOptions()
        opts.tools_changed = True
        return _orig(notification_options=opts,
                     experimental_capabilities=experimental_capabilities)

    server.create_initialization_options = _init_opts


_wire_transport_tiering()


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
    """Idempotent: start the headless dream sweep (Tier 0/2). The same
    thread also runs ``run_sweep_once``'s compaction/dream-run-journal/
    retrieval-log reapers, none of which are actually gated on
    ``dream.enabled`` (issue #178) — so this starts whenever EITHER
    dreaming OR the retrieval log is enabled; a dream-disabled bank with
    the (default-on) retrieval log still needs its own reaper, or
    ``retrieval_events`` grows unbounded. ``run_sweep_once`` itself still
    gates the automatic dream trigger on backlog + quiescence each tick,
    so an idle bank does no LLM work. Daemon-only."""
    global _dream_sweep_started
    if _dream_sweep_started:
        return
    dream_cfg = service.config.memory.dream
    if not (dream_cfg.enabled or service.config.memory.retrieval_log.enabled):
        return
    if dream_cfg.enabled:
        from pseudolife_memory.memory.dream import (build_extractor, NoOpExtractor,
                                                    startup_extractor_warnings)
        if isinstance(build_extractor(dream_cfg), NoOpExtractor):
            logger.warning(
                "dream enabled but no extractor LLM configured "
                "(PSEUDOLIFE_DREAM_BASE_URL/_MODEL unset): cortex auto-population is "
                "disabled; only memory_fact_set writes canonical facts. Point the "
                "daemon at any OpenAI-compatible endpoint to fix it, e.g. "
                "PSEUDOLIFE_DREAM_BASE_URL=http://localhost:11434/v1 "
                "PSEUDOLIFE_DREAM_MODEL=qwen2.5:7b (a local Ollama); the Docker "
                "tier ships an extractor sidecar instead. /health reports this as "
                "extractor: \"none\"."
            )
        for warning in startup_extractor_warnings(dream_cfg):
            logger.warning("dream extractor config: %s", warning)
    _dream_sweep_started = True
    interval = float(dream_cfg.sweep_interval_seconds)
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
        "Pseudolife-MCP embedded stdio starting (data_dir=%s, config=%s)",
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
