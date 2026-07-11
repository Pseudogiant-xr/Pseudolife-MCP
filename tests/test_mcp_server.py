"""Tests for the MCP server module — tool registration + dispatch wiring.

We don't spin up a real stdio transport; instead we drive the FastMCP
instance's ``call_tool`` directly so the assertions are deterministic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_all_registered_tools_run_off_the_event_loop() -> None:
    """2026-07-02 review fix: the MCP SDK invokes sync tools inline on the
    uvicorn event loop, so one long tool call (dream_run, document_ingest,
    first-call model init) froze every other session, /health, and the
    console. Every registered tool must be an async wrapper that
    thread-dispatches its sync body (the REST layer already does the
    equivalent via run_in_executor)."""
    from pseudolife_memory import mcp_server  # noqa: PLC0415 — lazy import.

    tools = mcp_server.mcp._tool_manager.list_tools()
    assert tools, "no tools registered?"
    blocking = [t.name for t in tools if not t.is_async]
    assert blocking == [], f"tools that would block the event loop: {blocking}"


def test_module_level_tool_fns_stay_sync_callable() -> None:
    """The Console/tests call tool bodies directly — the module attribute
    must remain the plain sync function; only the registered copy is async."""
    import inspect

    from pseudolife_memory import mcp_server  # noqa: PLC0415 — lazy import.

    assert not inspect.iscoroutinefunction(mcp_server.memory_stats)


def test_all_tools_registered() -> None:
    """The MCP server exposes exactly the documented tool set."""
    from pseudolife_memory import mcp_server  # noqa: PLC0415 — lazy import.

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == sorted([
        # Associative stream.
        "memory_store",
        "memory_search",
        "memory_recent",
        "memory_supersede",
        "memory_stats",
        "memory_toolset",
        "document_ingest",
        "document_search",
        # Episodes + consolidation.
        "memory_session_title",
        "memory_episode_start",
        "memory_episode_end",
        "memory_episode_summary",
        "memory_consolidation_candidates",
        "memory_consolidate",
        # Cortex — canonical-fact layer.
        "memory_fact_get",
        "memory_fact_set",
        "memory_fact_resolve",
        "memory_history",
        # World cortex + lessons.
        "memory_world_set",
        "memory_world_search",
        "memory_outcome",
        "memory_lesson_search",
        # Consolidated verbs (2026-07-02): forget across all stores, the
        # dream lifecycle, and the graph review queue.
        "memory_forget",
        "memory_dream",
        "memory_graph_review",
        # Knowledge graph.
        "memory_graph_relate",
        "memory_graph_unrelate",
        "memory_alias",
        "memory_graph",
        "memory_recall",
        "memory_relation_define",
        # Engram traces / retention.
        "memory_get",
        "memory_reinforce",
    ])


def test_each_tool_has_non_empty_docstring() -> None:
    """Tools without docstrings show up as raw names in Claude's tool list —
    the description is what makes them useful. Catch missing docs early."""
    from pseudolife_memory import mcp_server  # noqa: PLC0415

    tools = asyncio.run(mcp_server.mcp.list_tools())
    for tool in tools:
        assert tool.description, f"Tool {tool.name!r} has no description."
        assert len(tool.description) > 30, (
            f"Tool {tool.name!r} description is too short to be useful."
        )


# ---------------------------------------------------------------------------
# Dispatch — invoke tools through the FastMCP machinery
# ---------------------------------------------------------------------------


def _invoke(tool_name: str, args: dict) -> dict:
    """Call a registered tool and parse the JSON result."""
    from pseudolife_memory import mcp_server  # noqa: PLC0415

    result = asyncio.run(mcp_server.mcp.call_tool(tool_name, args))
    # FastMCP returns a tuple in newer versions: (content_list, structured_dict).
    # Older versions return just content_list. Handle both.
    if isinstance(result, tuple):
        content, structured = result
    else:
        content, structured = result, None
    # Structured payload is what an MCP client uses — prefer it when present.
    if structured is not None:
        return structured
    # Fall back to text-content JSON parse for older SDK shapes.
    text_parts = [
        item.text for item in content if hasattr(item, "text")
    ]
    return json.loads("".join(text_parts))


def test_search_explain_attaches_trace_and_default_does_not(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "the gadget port is 8080", "source": "notes"})
    plain = _invoke("memory_search", {"query": "gadget port"})
    explained = _invoke("memory_search", {"query": "gadget port", "explain": True})
    assert "trace" not in plain
    assert "trace" in explained and isinstance(explained["trace"], dict)


def test_memory_trace_tool_is_gone() -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert "memory_trace" not in names


def test_graph_relation_filter_keeps_only_matching_edges(monkeypatch) -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415
    fake = {"found": True, "entity": "svc-a", "nodes": [], "paths": [],
            "edges": [{"src": "svc-a", "relation": "runs-on", "dst": "jvm-21"},
                      {"src": "svc-a", "relation": "uses", "dst": "redis"}]}
    monkeypatch.setattr(mcp_server.service, "graph_neighborhood",
                        lambda **kw: dict(fake))
    out = _invoke("memory_graph", {"entity": "svc-a", "relation_filter": "runs-on"})
    rels = {e["relation"] for e in out["edges"]}
    assert rels == {"runs-on"}


def test_get_neighbors_tool_is_gone() -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert "get_neighbors" not in names


_EXPECTED_MINIMAL = sorted([
    # The 7-tool eager surface for minimal-tier clients (Claude Desktop).
    "memory_store", "memory_search", "memory_fact_get", "memory_fact_set",
    "memory_outcome", "memory_session_title", "memory_toolset",
])

_EXPECTED_CORE = sorted(_EXPECTED_MINIMAL + [
    "memory_fact_resolve", "memory_graph", "memory_recall",
    "memory_graph_relate", "memory_world_search", "memory_world_set",
    "memory_lesson_search", "document_search", "document_ingest",
    "memory_stats", "memory_get", "memory_episode_start", "memory_episode_end",
])


def test_all_tools_register_regardless_of_toolset_env(tmp_path: Path, monkeypatch) -> None:
    """Visibility model: PSEUDOLIFE_MCP_TOOLSET no longer gates registration."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "core")
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    assert len(asyncio.run(mod.mcp.list_tools())) == len(mod._TOOL_TIERS)
    assert mod._DEFAULT_TIER == "core"


def test_visible_tool_names_per_tier() -> None:
    from pseudolife_memory import mcp_server as mod
    assert sorted(mod._visible_tool_names("minimal")) == _EXPECTED_MINIMAL
    assert sorted(mod._visible_tool_names("core")) == _EXPECTED_CORE
    assert mod._visible_tool_names("full") == set(mod._TOOL_TIERS)


def test_tier_map_env_parsed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PSEUDOLIFE_MCP_TIER_MAP", "claude-desktop:minimal,claude-code:core")
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    assert mod._TIER_MAP == {"claude-desktop": "minimal", "claude-code": "core"}


def test_memory_dream_run_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "the beacon port is 7777", "source": "notes"})
    out = _invoke("memory_dream", {"action": "run"})
    assert "pulled" in out and "cursor" in out
    # Single-writer cortex: no extractor LLM is configured in tests, so the dream
    # writes nothing (no regex floor fallback). The promote-with-extractor path is
    # covered at the service level in test_dream.py.
    got = _invoke("memory_fact_get", {"entity": "beacon", "attribute": "port"})
    assert got["record"] is None


def test_start_dream_sweep_warns_without_extractor(tmp_path: Path, monkeypatch, caplog) -> None:
    import importlib
    import logging
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PSEUDOLIFE_DREAM_BASE_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_DREAM_MODEL", raising=False)
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    with caplog.at_level(logging.WARNING, logger="pseudolife-mcp"):
        mod.start_dream_sweep()   # dream enabled by default, no extractor configured
    msgs = " ".join(r.getMessage().lower() for r in caplog.records)
    assert "extractor" in msgs and "cortex" in msgs


def test_memory_store_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    """Tool calls reach the service and produce the expected shape.

    Point the service at a per-test data_dir so repeated test runs
    don't pollute a shared bank.
    """
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    # Force-reload the module so the new env-var is picked up.
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    out = _invoke("memory_store", {"text": "An end-to-end MCP test memory", "source": "test"})
    assert out["stored"] is True
    assert out["reason"] is None
    assert "surprise" in out
    assert "cortex_promoted" in out


def test_memory_fact_set_get_forget_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    set_out = _invoke("memory_fact_set", {
        "entity": "project", "attribute": "language", "value": "rust", "origin": "user",
    })
    assert set_out["action"] == "inserted"
    # case/separator-insensitive lookup
    got = _invoke("memory_fact_get", {"entity": "Project", "attribute": "language"})
    assert got["record"]["value"] == "rust"
    assert got["record"]["origin"] == "user"
    # forget purges the slot
    forget = _invoke("memory_forget", {"scope": "fact", "entity": "project"})
    assert forget["removed"] == 1
    assert _invoke("memory_fact_get", {"entity": "project", "attribute": "language"})["record"] is None


def test_store_auto_promotes_and_search_surfaces_cortex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    mod.service.config.memory.cortex.auto_promote = True   # opt-in (default off)

    out = _invoke("memory_store", {
        "text": "I have a Ragdoll cat named Jacque", "source": "conversation",
    })
    assert out["cortex_promoted"] >= 1                      # slot auto-promoted
    facts = mod.service.cortex_dump()   # dump left the MCP surface (Console-only)
    assert any(e["entity"] == "Jacque" and e["origin"] == "user" for e in facts["entries"])
    # cortex-first: the canonical fact is surfaced in search
    res = _invoke("memory_search", {"query": "Ragdoll cat named Jacque", "top_k": 5})
    assert "cortex" in res and any(f["entity"] == "Jacque" for f in res["cortex"])


def test_memory_stats_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "Stats round-trip fact", "source": "test"})
    stats = _invoke("memory_stats", {})
    assert "bands" in stats
    assert stats["total_memories"] >= 1


def test_memory_search_explain_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "Trace dispatch fact", "source": "t"})
    out = _invoke("memory_search", {"query": "Trace dispatch", "top_k": 3, "explain": True})
    assert "trace" in out
    assert "tiers" in out["trace"]


# ---------------------------------------------------------------------------
# Tier C — episode lifecycle + consolidation tool dispatch
# ---------------------------------------------------------------------------


def test_memory_episode_lifecycle_via_mcp_dispatch(
    tmp_path: Path, monkeypatch,
) -> None:
    """start → store → end → list — the canonical Claude workflow."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    started = _invoke(
        "memory_episode_start",
        {"title": "Tier C work", "hint": "implementing episodes"},
    )
    assert started["title"] == "Tier C work"
    assert started["hint"] == "implementing episodes"
    ep_id = started["id"]

    _invoke("memory_store", {"text": "decision A", "source": "claude"})
    closed = _invoke("memory_episode_end", {})
    assert closed["id"] == ep_id
    assert closed["ended_at"] is not None

    # episode_list left the MCP surface (Console-only) — verify via service.
    listing = mod.service.episode_list(limit=5)
    assert any(e["id"] == ep_id for e in listing["episodes"])


def test_memory_episode_summary_via_mcp_dispatch(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    ep = _invoke("memory_episode_start", {"title": "summary session"})
    _invoke(
        "memory_store",
        {"text": "fact one", "source": "claude", "tags": ["alpha"]},
    )
    _invoke(
        "memory_store",
        {"text": "fact two", "source": "claude", "tags": ["alpha", "beta"]},
    )
    out = _invoke("memory_episode_summary", {"id": ep["id"]})
    assert out["found"] is True
    assert out["entry_count"] == 2
    tags = {row["tag"]: row["count"] for row in out["tag_distribution"]}
    assert tags["alpha"] == 2
    assert tags["beta"] == 1


def test_memory_consolidation_candidates_via_mcp_dispatch(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "stdio MCP transport choice", "source": "c"})
    _invoke("memory_store", {"text": "MCP transport is stdio (no port)", "source": "c"})
    _invoke("memory_store", {"text": "stdio chosen for MCP for port-freedom", "source": "c"})
    _invoke("memory_store", {"text": "unrelated cat picture note", "source": "c"})

    out = _invoke(
        "memory_consolidation_candidates",
        {"query": "MCP transport", "top_k": 10, "min_cohesion": 0.4},
    )
    assert "clusters" in out
    # At least one cluster surfaces, and at least 2 stdio-related entries
    # land in it together.
    assert len(out["clusters"]) >= 1
    member_texts = {m["text"] for m in out["clusters"][0]["members"]}
    stdio_count = sum(1 for t in member_texts if "stdio" in t.lower())
    assert stdio_count >= 2


def test_memory_consolidate_via_mcp_dispatch(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "old phrasing one", "source": "c"})
    _invoke("memory_store", {"text": "old phrasing two", "source": "c"})
    out = _invoke(
        "memory_consolidate",
        {
            "replaces": ["old phrasing one", "old phrasing two"],
            "new_text": "Consolidated: current phrasing",
            "tags": ["consolidated"],
        },
    )
    assert out["superseded_count"] == 2
    assert out["new_memory_stored"] is True
    recent = _invoke("memory_recent", {"n": 10})
    by_text = {e["text"]: e for e in recent["entries"]}
    assert by_text["old phrasing one"]["superseded"] is True
    # Compact shape: ``superseded`` only appears when true.
    assert "superseded" not in by_text["Consolidated: current phrasing"]
    assert "consolidated" in by_text["Consolidated: current phrasing"]["tags"]


# ---------------------------------------------------------------------------
# Cortex — provenance contenders + resolve dispatch
# ---------------------------------------------------------------------------


def test_memory_fact_get_returns_contenders_via_mcp(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_fact_set", {"entity": "project", "attribute": "language",
                                "value": "go", "origin": "user"})
    _invoke("memory_fact_set", {"entity": "project", "attribute": "language",
                                "value": "rust", "origin": "agent"})
    got = _invoke("memory_fact_get", {"entity": "project", "attribute": "language"})
    assert got["record"]["value"] == "go"                 # user fact current
    assert any(c["value"] == "rust" for c in got["contenders"])


def test_memory_fact_resolve_accept_and_reject_via_mcp(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_fact_set", {"entity": "svc", "attribute": "port",
                                "value": "8080", "origin": "user"})
    _invoke("memory_fact_set", {"entity": "svc", "attribute": "port",
                                "value": "9090", "origin": "agent"})
    acc = _invoke("memory_fact_resolve", {"entity": "svc", "attribute": "port",
                                          "accept": True})
    assert acc["resolved"] is True
    got = _invoke("memory_fact_get", {"entity": "svc", "attribute": "port"})
    assert got["record"]["value"] == "9090"
    # nothing left to resolve
    none = _invoke("memory_fact_resolve", {"entity": "svc", "attribute": "port",
                                           "accept": False})
    assert none["resolved"] is False


# ---------------------------------------------------------------------------
# Cortex-first dedup — only drop a recall hit that genuinely RESTATES a fact,
# not one that merely mentions the value while adding context.
# ---------------------------------------------------------------------------


def test_restates_fact_drops_only_dominant_restatements() -> None:
    from pseudolife_memory.mcp_server import _restates_fact  # noqa: PLC0415

    # Genuine restatement: the entry is essentially just the value -> drop.
    assert _restates_fact("postgres", "postgres") is True
    assert _restates_fact("Production-Database", "production-database") is True
    assert _restates_fact("host is 10.0.0.5", "10.0.0.5") is True

    # Mentions the value but adds substantial context -> KEEP (the over-drop bug).
    assert _restates_fact("claude code is the MCP client here", "claude") is False
    assert _restates_fact(
        "we migrated the production-database last week after the outage",
        "production-database",
    ) is False
    assert _restates_fact(
        "the db host is 10.0.0.5 per the ops runbook, set during the incident",
        "10.0.0.5",
    ) is False


def test_restates_fact_requires_word_boundary_and_min_length() -> None:
    from pseudolife_memory.mcp_server import _restates_fact  # noqa: PLC0415

    # Substring inside a larger token is not a real mention.
    assert _restates_fact("postgresql", "postgres") is False
    # Short values (<5 chars) are too ambiguous to dedup on.
    assert _restates_fact("rust", "rust") is False
    # Empty / missing.
    assert _restates_fact("anything", "") is False


# ---------------------------------------------------------------------------
# Compact-by-default recall payloads (2026-07-10 token-cost lever) — the five
# recall-path tools return only the fields an agent acts on; ``verbose=True``
# (and ``explain=True`` on memory_search) restores the full metadata. The
# Console REST paths call service.* directly and are unaffected.
# ---------------------------------------------------------------------------


_ENTRY_NOISE = ("timestamp", "access_count", "surprise_score", "bank",
                "episode_id", "episode_title")


def _reload_mod(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


def test_memory_search_entries_are_compact_by_default(tmp_path: Path, monkeypatch) -> None:
    _reload_mod(tmp_path, monkeypatch)
    _invoke("memory_store", {"text": "the widget port is 9191",
                             "source": "notes", "tags": ["net"]})
    out = _invoke("memory_search", {"query": "widget port"})
    assert out["count"] >= 1
    e = out["entries"][0]
    assert set(e) == {"id", "text", "source", "tags", "score"}
    for noise in _ENTRY_NOISE + ("superseded", "superseded_by_text"):
        assert noise not in e


def test_memory_search_verbose_restores_full_metadata(tmp_path: Path, monkeypatch) -> None:
    _reload_mod(tmp_path, monkeypatch)
    _invoke("memory_store", {"text": "the widget port is 9191", "source": "notes"})
    out = _invoke("memory_search", {"query": "widget port", "verbose": True})
    e = out["entries"][0]
    for k in _ENTRY_NOISE + ("superseded",):
        assert k in e, f"verbose entry missing {k!r}"


def test_memory_search_explain_implies_verbose_entries(tmp_path: Path, monkeypatch) -> None:
    _reload_mod(tmp_path, monkeypatch)
    _invoke("memory_store", {"text": "the widget port is 9191", "source": "notes"})
    out = _invoke("memory_search", {"query": "widget port", "explain": True})
    assert "trace" in out
    e = out["entries"][0]
    for k in _ENTRY_NOISE:
        assert k in e, f"explain entry missing {k!r}"


def test_compact_search_keeps_supersession_signal(tmp_path: Path, monkeypatch) -> None:
    """superseded_by_text changes answers — it must survive compaction."""
    _reload_mod(tmp_path, monkeypatch)
    _invoke("memory_store", {"text": "the api key lives in .env", "source": "notes"})
    _invoke("memory_supersede", {"old_text": "the api key lives in .env",
                                 "new_text": "the api key lives in the vault now"})
    out = _invoke("memory_search", {"query": "where does the api key live"})
    old = next(e for e in out["entries"] if e["text"] == "the api key lives in .env")
    assert old["superseded"] is True
    assert old["superseded_by_text"] == "the api key lives in the vault now"


def test_memory_recent_compact_by_default_verbose_restores(tmp_path: Path, monkeypatch) -> None:
    _reload_mod(tmp_path, monkeypatch)
    _invoke("memory_store", {"text": "recent shape probe", "source": "notes",
                             "tags": ["probe"]})
    compact = _invoke("memory_recent", {"n": 5})["entries"][0]
    assert set(compact) == {"id", "text", "source", "tags"}
    full = _invoke("memory_recent", {"n": 5, "verbose": True})["entries"][0]
    for k in _ENTRY_NOISE + ("superseded",):
        assert k in full, f"verbose entry missing {k!r}"


_FULL_LESSON = {
    "task": "deploy daemon", "aspect": "procedure", "lesson": "backup first",
    "about": "ops/update.ps1", "polarity": "+", "outcome": "success",
    "status": "current", "confidence": 0.9, "origin": "action",
    "provenance": ["action"], "asserted_at": 1.0, "last_confirmed": 2.0,
    "supersedes_value": None, "superseded_by_value": None,
    "superseded_at": None, "score": 0.8,
}


def test_memory_lesson_search_compact_by_default(monkeypatch) -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415
    monkeypatch.setattr(
        mcp_server.service, "lesson_search",
        lambda *a, **k: {"count": 1, "entries": [dict(_FULL_LESSON)]})
    e = _invoke("memory_lesson_search", {"query": "deploy"})["entries"][0]
    assert set(e) == {"task", "aspect", "lesson", "about", "polarity",
                      "outcome", "confidence", "score"}
    full = _invoke("memory_lesson_search", {"query": "deploy", "verbose": True})
    assert set(full["entries"][0]) == set(_FULL_LESSON)


def test_memory_lesson_search_compact_keeps_re_verify(monkeypatch) -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415
    row = {**_FULL_LESSON, "re_verify": True, "re_verify_reason": "facts changed"}
    monkeypatch.setattr(
        mcp_server.service, "lesson_search",
        lambda *a, **k: {"count": 1, "entries": [row]})
    e = _invoke("memory_lesson_search", {"query": "deploy"})["entries"][0]
    assert e["re_verify"] is True
    assert e["re_verify_reason"] == "facts changed"


_FULL_WORLD = {
    "entity": "fastmcp", "attribute": "latest version", "value": "2.3",
    "polarity": "+", "status": "current", "confidence": 0.85,
    "effective_confidence": 0.81, "stale": False, "origin": "web",
    "freshness_class": "volatile", "source_url": "https://example.com/x",
    "source_quote": "fastmcp 2.3 released", "retrieved_at": 1.0,
    "asserted_at": 1.0, "last_confirmed": 2.0, "supersedes_value": None,
    "superseded_by_value": None, "superseded_at": None, "score": 0.7,
}


def test_memory_world_search_compact_by_default(monkeypatch) -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415
    monkeypatch.setattr(
        mcp_server.service, "world_search",
        lambda *a, **k: {"count": 1, "entries": [dict(_FULL_WORLD)]})
    e = _invoke("memory_world_search", {"query": "fastmcp version"})["entries"][0]
    assert set(e) == {"entity", "attribute", "value", "effective_confidence",
                      "stale", "source_url", "source_quote", "score"}
    full = _invoke("memory_world_search", {"query": "fastmcp version",
                                           "verbose": True})
    assert set(full["entries"][0]) == set(_FULL_WORLD)


def test_memory_recall_compact_by_default(monkeypatch) -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415
    fake = {
        "query": "q", "seeds": ["svc-a"],
        "entities": [{"entity": "svc-a",
                      "facts": [{"attribute": "port", "value": "9090",
                                 "origin": "agent", "confidence": 0.8}]}],
        "edges": [{"src": "svc-a", "relation": "runs-on", "dst": "jvm-21",
                   "derived": False, "confidence": 0.9, "origin": "agent",
                   "tag": "confirmed"}],
        "paths": [["svc-a", "jvm-21"]], "texts": ["svc-a runs on jvm-21"],
        "iterations": 1, "hops": 3, "low_confidence": False,
    }
    monkeypatch.setattr(mcp_server.service, "recall",
                        lambda *a, **k: dict(fake))
    out = _invoke("memory_recall", {"query": "what does svc-a run on"})
    assert out["entities"] == [{"entity": "svc-a",
                                "facts": [{"attribute": "port", "value": "9090"}]}]
    assert out["edges"] == [{"src": "svc-a", "relation": "runs-on", "dst": "jvm-21"}]
    assert out["paths"] == [["svc-a", "jvm-21"]]      # untouched
    assert out["texts"] == ["svc-a runs on jvm-21"]   # untouched
    full = _invoke("memory_recall", {"query": "what does svc-a run on",
                                     "verbose": True})
    assert full["entities"][0]["facts"][0]["confidence"] == 0.8
    assert full["edges"][0]["tag"] == "confirmed"


# ---------------------------------------------------------------------------
# Session-scoped tier visibility at the transport (spec 2026-07-11)
# ---------------------------------------------------------------------------

from types import SimpleNamespace


class _FakeReqCtx:
    """Bind fake HTTP headers into the SDK's request_ctx for one test."""
    def __init__(self, headers: dict[str, str]):
        self._headers = headers
        self._token = None
    def __enter__(self):
        from mcp.server.lowlevel.server import request_ctx
        self._token = request_ctx.set(
            SimpleNamespace(request=SimpleNamespace(headers=self._headers)))
        return self
    def __exit__(self, *exc):
        from mcp.server.lowlevel.server import request_ctx
        request_ctx.reset(self._token)


async def _transport_list(mod) -> list:
    import mcp.types as mtypes
    handler = mod.mcp._mcp_server.request_handlers[mtypes.ListToolsRequest]
    result = await handler(None)
    return result.root.tools


def _reload_tiered(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PSEUDOLIFE_WRITER_ID", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


def test_transport_list_filters_by_writer_map(tmp_path: Path, monkeypatch) -> None:
    mod = _reload_tiered(tmp_path, monkeypatch,
                         PSEUDOLIFE_MCP_TOOLSET="core",
                         PSEUDOLIFE_MCP_TIER_MAP="claude-desktop:minimal")
    with _FakeReqCtx({"x-pl-writer": "claude-desktop", "x-pl-session": "d1"}):
        names = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names == mod._visible_tool_names("minimal")
    # No headers (stdio/tests) -> env default tier
    with _FakeReqCtx({}):
        names = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names == mod._visible_tool_names("core")


def test_transport_list_env_writer_fallback(tmp_path: Path, monkeypatch) -> None:
    """Direct-HTTP Claude Code sends no X-PL-Writer; the daemon's default
    writer id (PSEUDOLIFE_WRITER_ID) feeds the tier map instead."""
    mod = _reload_tiered(tmp_path, monkeypatch,
                         PSEUDOLIFE_MCP_TOOLSET="full",
                         PSEUDOLIFE_MCP_TIER_MAP="claude-code:core",
                         PSEUDOLIFE_WRITER_ID="claude-code")
    with _FakeReqCtx({"x-pl-session": "c1"}):
        names = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names == mod._visible_tool_names("core")


def test_hidden_tools_stay_callable(tmp_path: Path, monkeypatch) -> None:
    """Visibility is not a call gate: a full-tier tool dispatches fine in a
    minimal-default deployment."""
    mod = _reload_tiered(tmp_path, monkeypatch, PSEUDOLIFE_MCP_TOOLSET="minimal")
    _invoke("memory_store", {"text": "hidden-call probe", "source": "t"})
    out = _invoke("memory_recent", {"n": 1})       # memory_recent is full-tier
    assert out["count"] == 1


def test_initialization_advertises_list_changed(tmp_path: Path, monkeypatch) -> None:
    mod = _reload_tiered(tmp_path, monkeypatch)
    opts = mod.mcp._mcp_server.create_initialization_options()
    assert opts.capabilities.tools.listChanged is True


def test_tool_cache_prefilled_with_full_set(tmp_path: Path, monkeypatch) -> None:
    """Hidden tools keep call-time input validation: the SDK tool cache is
    fed the FULL registry, not the filtered view."""
    mod = _reload_tiered(tmp_path, monkeypatch, PSEUDOLIFE_MCP_TOOLSET="minimal")
    with _FakeReqCtx({"x-pl-session": "m1"}):
        asyncio.run(_transport_list(mod))
    assert set(mod.mcp._mcp_server._tool_cache) == set(mod._TOOL_TIERS)


def test_memory_toolset_ladder_and_status(tmp_path: Path, monkeypatch) -> None:
    mod = _reload_tiered(tmp_path, monkeypatch,
                         PSEUDOLIFE_MCP_TOOLSET="core",
                         PSEUDOLIFE_MCP_TIER_MAP="claude-desktop:minimal")
    with _FakeReqCtx({"x-pl-writer": "claude-desktop", "x-pl-session": "lad1"}):
        st = _invoke("memory_toolset", {"action": "status"})
        assert st["current"] == "minimal" and st["default"] == "minimal"
        assert st["ladder"] == ["minimal", "core", "full"]
        assert set(st["adds"]) == {"core", "full"}

        up = _invoke("memory_toolset", {"action": "expand"})
        assert up["changed"] is True and up["current"] == "core"
        assert "memory_recall" in up["visible_tools_added"]
        assert up["list_changed_sent"] is False   # no live transport session here

        up2 = _invoke("memory_toolset", {"action": "expand"})
        assert up2["current"] == "full"
        top = _invoke("memory_toolset", {"action": "expand"})
        assert top["changed"] is False            # already at the top

        down = _invoke("memory_toolset", {"action": "collapse"})
        assert down["current"] == "core"
        down2 = _invoke("memory_toolset", {"action": "collapse"})
        assert down2["current"] == "minimal"
        floor = _invoke("memory_toolset", {"action": "collapse"})
        assert floor["changed"] is False          # floored at the session default

        # And the transport list follows the override
        names = {t.name for t in asyncio.run(_transport_list(mod))}
        assert names == mod._visible_tool_names("minimal")


def test_memory_toolset_expansion_is_session_scoped(tmp_path: Path, monkeypatch) -> None:
    mod = _reload_tiered(tmp_path, monkeypatch,
                         PSEUDOLIFE_MCP_TOOLSET="minimal")
    with _FakeReqCtx({"x-pl-session": "sA"}):
        _invoke("memory_toolset", {"action": "expand"})
    with _FakeReqCtx({"x-pl-session": "sA"}):
        names = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names == mod._visible_tool_names("core")
    with _FakeReqCtx({"x-pl-session": "sB"}):   # untouched session stays minimal
        names_b = {t.name for t in asyncio.run(_transport_list(mod))}
    assert names_b == mod._visible_tool_names("minimal")


def test_memory_toolset_is_minimal_tier_and_registered() -> None:
    from pseudolife_memory import mcp_server as mod
    assert mod._TOOL_TIERS["memory_toolset"] == "minimal"
    assert "memory_toolset" in {t.name for t in asyncio.run(mod.mcp.list_tools())}


def test_list_changed_attempted_on_change_not_on_noop(tmp_path: Path, monkeypatch) -> None:
    """Spec test item 4: the notification fires on a tier change and is NOT
    attempted on a no-op (expand at full / collapse at floor)."""
    mod = _reload_tiered(tmp_path, monkeypatch, PSEUDOLIFE_MCP_TOOLSET="core")
    calls = []

    async def _spy(ctx):
        calls.append(True)
        return True

    monkeypatch.setattr(mod, "_notify_list_changed", _spy)
    with _FakeReqCtx({"x-pl-session": "n1"}):
        out = _invoke("memory_toolset", {"action": "expand"})   # core -> full
        assert out["changed"] is True and calls == [True]
        noop = _invoke("memory_toolset", {"action": "expand"})  # already full
        assert noop["changed"] is False and calls == [True]     # no second send
