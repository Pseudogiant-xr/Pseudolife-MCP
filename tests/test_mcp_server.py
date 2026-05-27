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
