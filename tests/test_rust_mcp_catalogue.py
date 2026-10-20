"""The Rust daemon embeds the MCP tool catalogue as recorded from this daemon
(rust/daemon/src/mcp/tools.jsonl and tiers.json, spec item M9). A tool's
description, schema, annotations or tier changing here must be re-recorded
there, or the two daemons would advertise different tools.

Re-record against a running daemon whose token resolves to the minimal tier:
    python rust/daemon/harness/mcp_catalogue.py <port> <token> rust/daemon/src/mcp
The byte form (key order, escapes) is checked against the SDK wire serializer
here and against live responses in rust/daemon/harness/mcp_compare.py.
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
    from mcp.types import JSONRPCResponse, ListToolsResult, methods

    _, tools = _registry()
    lines = (CATALOGUE / "tools.jsonl").read_text(encoding="utf-8").splitlines()
    recorded = [json.loads(line) for line in lines if line]
    live = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    assert [t["name"] for t in recorded] == [t["name"] for t in live], RERECORD
    for rec, cur in zip(recorded, live):
        assert rec == cur, f"{cur['name']} changed: {RERECORD}"

    # The SDK runner applies its protocol surface before serializing JSON-RPC.
    # Do not sort schema dictionaries: their property order is wire data.
    result = methods.serialize_server_result(
        "tools/list", "2025-11-25",
        ListToolsResult(tools=tools).model_dump(by_alias=True, mode="json", exclude_none=True),
    )
    wire = JSONRPCResponse(jsonrpc="2.0", id=1, result=result).model_dump_json(
        by_alias=True, exclude_unset=True,
    ).encode("utf-8")
    embedded = ('{"jsonrpc":"2.0","id":1,"result":{"tools":['
                + ",".join(lines) + ']}}').encode("utf-8")
    assert embedded == wire, f"catalogue wire bytes changed: {RERECORD}"


def test_rust_catalogue_tiers_match_the_registry():
    mcp_server, tools = _registry()
    tiers = json.loads((CATALOGUE / "tiers.json").read_text(encoding="utf-8"))
    assert tiers == {t.name: mcp_server._TOOL_TIERS[t.name] for t in tools}, RERECORD


def test_catalogue_rejects_input_schema_property_reordering(tmp_path, monkeypatch):
    import pytest

    lines = (CATALOGUE / "tools.jsonl").read_text(encoding="utf-8").splitlines()
    tool = json.loads(lines[0])
    properties = tool["inputSchema"]["properties"]
    assert len(properties) > 1
    tool["inputSchema"]["properties"] = dict(reversed(list(properties.items())))
    lines[0] = json.dumps(tool, ensure_ascii=False, separators=(",", ":"))
    (tmp_path / "tools.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "CATALOGUE", tmp_path)
    with pytest.raises(AssertionError):
        test_rust_catalogue_matches_the_registered_tools()
