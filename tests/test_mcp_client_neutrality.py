"""Public MCP client-neutrality guards."""

import inspect
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_mcp_initialization_advertises_memory_workflow() -> None:
    from pseudolife_memory import mcp_server

    instructions = mcp_server.mcp._mcp_server.instructions
    assert instructions
    assert "task start" in instructions.lower()
    assert "memory_search" in instructions
    assert "memory_outcome" in instructions
    assert "Claude" not in instructions


def test_mcp_store_default_source_is_client_neutral() -> None:
    from pseudolife_memory import mcp_server

    assert inspect.signature(mcp_server.memory_store).parameters["source"].default == "agent"


def test_public_package_metadata_is_client_neutral() -> None:
    pyproject = _read("pyproject.toml")
    manifest = _read("server.json")
    assert "for Claude Code" not in pyproject
    assert "for Claude:" not in manifest
    assert "MCP-compatible" in pyproject
    assert "MCP-compatible" in manifest
