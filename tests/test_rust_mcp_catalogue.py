"""The Rust daemon embeds the MCP tool catalogue as recorded from this daemon
(rust/daemon/src/mcp/tools.jsonl and tiers.json, spec item M9). A tool's
description, schema, annotations or tier changing here must be re-recorded
there, or the two daemons would advertise different tools.

Re-record against a running daemon whose token resolves to the minimal tier:
    python rust/daemon/harness/mcp_catalogue.py <port> <token> rust/daemon/src/mcp
The byte form (key order, escapes) is checked by the live differential
harness (rust/daemon/harness/mcp_compare.py); this test compares values.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

CATALOGUE = Path(__file__).resolve().parents[1] / "rust" / "daemon" / "src" / "mcp"
RERECORD = ("re-record with `python rust/daemon/harness/mcp_catalogue.py <port> <token> "
            "rust/daemon/src/mcp` (see this module's docstring)")


def _registry():
    from pseudolife_memory import mcp_server  # noqa: PLC0415 — lazy import.

    tools = asyncio.run(mcp_server.mcp.list_tools())
    return mcp_server, tools


def test_rust_catalogue_matches_the_registered_tools():
    _, tools = _registry()
    lines = (CATALOGUE / "tools.jsonl").read_text(encoding="utf-8").splitlines()
    recorded = [json.loads(line) for line in lines if line]
    live = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    assert [t["name"] for t in recorded] == [t["name"] for t in live], RERECORD
    for rec, cur in zip(recorded, live):
        assert rec == cur, f"{cur['name']} changed: {RERECORD}"


def test_rust_catalogue_tiers_match_the_registry():
    mcp_server, tools = _registry()
    tiers = json.loads((CATALOGUE / "tiers.json").read_text(encoding="utf-8"))
    assert tiers == {t.name: mcp_server._TOOL_TIERS[t.name] for t in tools}, RERECORD
