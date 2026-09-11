"""Model coordination tools carry routing data, never instance credentials."""
from types import SimpleNamespace

import pytest


def test_message_surface_cannot_override_identity(monkeypatch):
    from pseudolife_memory import mcp_server as mod, coordination
    seen = []
    monkeypatch.setattr(coordination, "dispatch", lambda svc, action, args: seen.append(
        (action, args)) or {"status": "queued"})
    out = mod.memory_message(action="send", to="peer", text="Please review", request_id="r1")
    assert out == {"status": "queued"}
    assert seen == [("send", {"to": "peer", "text": "Please review", "request_id": "r1"})]
    with pytest.raises(TypeError):
        mod.memory_message(action="send", sender_id="peer")


def test_agents_tool_uses_awareness_when_no_adapter_identity(monkeypatch):
    from pseudolife_memory import mcp_server as mod
    from pseudolife_memory.writer_context import bind_request_headers, unbind_request_headers
    monkeypatch.setattr(mod, "service", SimpleNamespace(
        coordination_awareness=lambda: {"peers": [{"title": "Peer task"}]}))
    token = bind_request_headers({})
    try:
        assert mod.memory_agents()["peers"] == [{"title": "Peer task"}]
    finally:
        unbind_request_headers(token)
