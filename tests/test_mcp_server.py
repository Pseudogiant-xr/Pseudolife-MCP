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


def test_all_tools_registered() -> None:
    """The MCP server exposes exactly the documented tool set."""
    from pseudolife_memory import mcp_server  # noqa: PLC0415 — lazy import.

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == sorted([
        "memory_store",
        "memory_search",
        "memory_recent",
        "memory_list_sources",
        "memory_supersede",
        "memory_delete",
        "memory_stats",
        "memory_save",
        "document_ingest",
        "document_search",
        # Tier C — episode lifecycle + tag listing + consolidation.
        "memory_session_title",
        "memory_episode_start",
        "memory_episode_end",
        "memory_episode_list",
        "memory_episode_summary",
        "memory_list_tags",
        "memory_consolidation_candidates",
        "memory_consolidate",
        # Cortex — canonical-fact layer.
        "memory_fact_get",
        "memory_fact_set",
        "memory_fact_forget",
        "memory_facts",
        "memory_fact_resolve",
        "memory_history",
        # World cortex — sourced external-knowledge layer.
        "memory_world_set",
        "memory_world_search",
        "memory_world_facts",
        "memory_world_forget",
        # Procedural / outcome memory — lessons (schema v10).
        "memory_outcome",
        "memory_lesson_search",
        "memory_lessons",
        "memory_lesson_forget",
        # Dream — MIRAS->cortex consolidation.
        "memory_dream_pull",
        "memory_dream_status",
        "memory_dream_commit",
        "memory_dream_run",
        # Phase 2 — knowledge graph + ontology-lite.
        "memory_graph_relate",
        "memory_graph_unrelate",
        "memory_alias",
        "memory_graph",
        "memory_recall",
        "memory_relation_define",
        # Graph foundation (v0.6) — recall/graph extras, graph-insight, provenance traces.
        "memory_path",
        "memory_digest",
        "memory_communities",
        "memory_briefing",
        "memory_get",
        "memory_reinforce",
        # Deep dream — full-corpus graph consolidation.
        "memory_deep_dream",
        # Deep dream — link proposals.
        "memory_graph_propose_links",
        "memory_graph_accept_proposal",
        "memory_graph_reject_proposal",
        "memory_graph_accept_entity_merge",
        "memory_graph_accept_entity_junk",
        "memory_graph_reject_entity_proposal",
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


_EXPECTED_CORE = sorted([
    "memory_store", "memory_search", "memory_fact_get", "memory_fact_set",
    "memory_fact_resolve", "memory_graph", "memory_recall", "memory_graph_relate",
    "memory_world_search", "memory_world_set", "memory_lesson_search",
    "memory_outcome", "document_search", "document_ingest", "memory_stats",
])


def test_should_register_gate_logic() -> None:
    from pseudolife_memory.mcp_server import _should_register  # noqa: PLC0415
    assert _should_register("full", core=False) is True
    assert _should_register("full", core=True) is True
    assert _should_register("core", core=True) is True
    assert _should_register("core", core=False) is False


def test_core_tier_membership_is_exactly_the_core_set() -> None:
    from pseudolife_memory import mcp_server  # noqa: PLC0415
    core_names = sorted(n for n, is_core in mcp_server._TOOL_TIERS.items() if is_core)
    assert core_names == _EXPECTED_CORE


def test_memory_dream_run_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "the beacon port is 7777", "source": "notes"})
    out = _invoke("memory_dream_run", {})
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
    forget = _invoke("memory_fact_forget", {"entity": "project"})
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
    facts = _invoke("memory_facts", {})
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


def test_memory_list_sources_via_mcp_dispatch(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "A1", "source": "alpha"})
    _invoke("memory_store", {"text": "A2", "source": "alpha"})
    _invoke("memory_store", {"text": "B1", "source": "beta"})
    out = _invoke("memory_list_sources", {})
    by_source = {row["source"]: row["count"] for row in out["sources"]}
    assert by_source["alpha"] == 2
    assert by_source["beta"] == 1


def test_memory_delete_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "Junk", "source": "test"})
    _invoke("memory_store", {"text": "Keep", "source": "test"})
    out = _invoke("memory_delete", {"text": "Junk"})
    assert out["deleted_count"] == 1
    recent = _invoke("memory_recent", {"n": 10})
    texts = [e["text"] for e in recent["entries"]]
    assert "Junk" not in texts
    assert "Keep" in texts


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

    listing = _invoke("memory_episode_list", {"limit": 5})
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


def test_memory_list_tags_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "a", "source": "x", "tags": ["red"]})
    _invoke("memory_store", {"text": "b", "source": "x", "tags": ["red", "blue"]})
    out = _invoke("memory_list_tags", {})
    counts = {row["tag"]: row["count"] for row in out["tags"]}
    assert counts["red"] == 2
    assert counts["blue"] == 1


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
    assert by_text["Consolidated: current phrasing"]["superseded"] is False
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
