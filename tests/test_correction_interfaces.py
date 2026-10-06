"""Selected entry identity survives MCP, HTTP, and Console correction calls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.helpers import invoke_tool
from pseudolife_memory.web.routes import ConsoleRoutes


@pytest.mark.parametrize("method,selector", [
    ("supersede", {"entry_id": 41}),
    ("consolidate", {"entry_ids": [41, 42]}),
])
@pytest.mark.parametrize("transport", ["mcp", "http"])
def test_correction_dispatch_preserves_selected_ids(monkeypatch, method, selector, transport):
    calls = []

    def record(**kwargs):
        calls.append(kwargs)
        return {"new_memory_stored": True, "superseded_ids": [41]}

    svc = SimpleNamespace(supersede=record, consolidate=record)
    payload = {**selector, "new_text": "The revised note"}
    if transport == "mcp":
        from pseudolife_memory import mcp_server
        monkeypatch.setattr(mcp_server, "service", svc)
        result = invoke_tool(f"memory_{method}", payload)
    else:
        result = ConsoleRoutes(svc).dispatch("POST", f"/api/{method}", {}, payload)
    assert result["new_memory_stored"] is True
    assert len(calls) == 1
    assert all(calls[0][key] == value for key, value in selector.items())
    assert calls[0].get("old_text" if method == "supersede" else "replaces") is None


@pytest.mark.parametrize("method,key,bad", [
    ("supersede", "entry_id", True),
    ("supersede", "entry_id", "41"),
    ("consolidate", "entry_ids", [41, True]),
    ("consolidate", "entry_ids", [41.0]),
])
def test_mcp_does_not_coerce_correction_ids(monkeypatch, method, key, bad):
    from pseudolife_memory import mcp_server
    calls = []
    monkeypatch.setattr(mcp_server, "service", SimpleNamespace(**{
        method: lambda **kwargs: calls.append(kwargs) or {"called": True},
    }))
    refused = invoke_tool(f"memory_{method}", {key: bad, "new_text": "The revised note"})
    assert refused["error"] == "invalid_argument" and refused["param"] == key
    assert not calls


@pytest.mark.parametrize("tool", ["memory_supersede", "memory_consolidate"])
def test_replacement_text_stays_a_required_tool_argument(tool):
    """A correction without replacement text is a client error, not a no-op call."""
    from pseudolife_memory import mcp_server

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    required = set(tools[tool].input_schema.get("required") or [])
    assert "new_text" in required
    assert not required & {"old_text", "entry_id", "replaces", "entry_ids"}


# The Console's half of these contracts (correction by id, a rejection reported
# rather than toasted, the bulk-delete second confirm) is unit-tested beside
# the code that implements it: frontend/src/lib/stream.test.ts and
# consolidation.test.ts, run by CI's frontend job.
