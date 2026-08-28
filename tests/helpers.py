"""Small test doubles and utilities shared across otherwise unrelated suites.

Lives here rather than in ``conftest.py`` because these are plain callables,
not pytest fixtures: importing them explicitly keeps each test file honest
about what it depends on. Dream-specific doubles live in
``tests/dream_helpers.py``, the console app's ASGI driver in
``tests/asgi_helpers.py``.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import socket

import torch


def unit_vec(seed: int, dim: int = 8) -> torch.Tensor:
    """A deterministic unit embedding — the cortex suites' stand-in for the
    real embedder, so store-level tests stay fast and offline."""
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(dim, generator=g)
    return v / v.norm()


def reload_mcp_filemode(tmp_path, monkeypatch):
    """Reimport ``mcp_server`` bound to a throwaway file-mode data dir.

    The module builds its ``service`` singleton at import time from the
    environment, so tool-layer tests must reload it after pointing
    ``PSEUDOLIFE_MCP_DATA_DIR`` at a tmp dir; the DATABASE_URL delete forces
    file mode even on a machine with the bench Postgres configured.
    """
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    import pseudolife_memory.mcp_server as mod
    importlib.reload(mod)
    return mod


def free_port() -> int:
    """An unused loopback port, for the suites that spawn a real daemon."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pg_reachable(url: str) -> bool:
    """Whether the bench Postgres answers — the daemon/shim suites' skip gate.

    ``psycopg`` is imported here rather than at module scope so a machine
    without it can still import this module for the non-PG helpers; both
    callers already gate themselves with ``pytest.importorskip("psycopg")``.
    """
    import psycopg

    try:
        with psycopg.connect(url, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def invoke_tool(tool_name: str, args: dict) -> dict:
    """Call a registered MCP tool through FastMCP and parse the JSON result.

    SDK v1 FastMCP returned ``(content_list, structured_dict)`` (or bare
    content); v2 MCPServer returns a ``CallToolResult``. All three shapes are
    handled here so the tool-layer suites do not each carry the version
    knowledge. The structured payload is what a real MCP client uses, so it
    wins when present.
    """
    from pseudolife_memory import mcp_server  # noqa: PLC0415

    result = asyncio.run(mcp_server.mcp.call_tool(tool_name, args))
    if isinstance(result, tuple):
        content, structured = result
    elif hasattr(result, "content"):
        content = result.content
        structured = getattr(result, "structured_content", None)
    else:
        content, structured = result, None
    if structured is not None:
        return structured
    return json.loads(
        "".join(item.text for item in content if hasattr(item, "text")))
