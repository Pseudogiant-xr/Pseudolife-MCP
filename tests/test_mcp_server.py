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
        "memory_trace",
        "memory_recent",
        "memory_list_sources",
        "memory_supersede",
        "memory_delete",
        "memory_stats",
        "memory_save",
        "document_ingest",
        "document_search",
        # Tier C — episode lifecycle + tag listing + consolidation.
        "memory_episode_start",
        "memory_episode_end",
        "memory_episode_list",
        "memory_episode_summary",
        "memory_list_tags",
        "memory_consolidation_candidates",
        "memory_consolidate",
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


def test_memory_stats_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "Stats round-trip fact", "source": "test"})
    stats = _invoke("memory_stats", {})
    assert "bands" in stats
    assert stats["total_memories"] >= 1


def test_memory_trace_via_mcp_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    import importlib
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)

    _invoke("memory_store", {"text": "Trace dispatch fact", "source": "t"})
    out = _invoke("memory_trace", {"query": "Trace dispatch", "top_k": 3})
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
