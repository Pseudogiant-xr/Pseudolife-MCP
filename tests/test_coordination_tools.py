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


def test_agents_update_passes_children_and_an_empty_list_clears(monkeypatch):
    from pseudolife_memory import mcp_server as mod, coordination
    seen = []
    monkeypatch.setattr(coordination, "dispatch", lambda svc, action, args: seen.append(
        (action, args)) or {})
    mod.memory_agents(action="update", children=["review storage", "tests"])
    mod.memory_agents(action="update", children=[])
    mod.memory_agents(action="update", status="orchestrating")
    assert seen == [("update", {"children": ["review storage", "tests"]}),
                    ("update", {"children": []}),
                    ("update", {"status": "orchestrating"})]
    for action in ("list", "claim", "release"):
        with pytest.raises(ValueError, match="unexpected_parameter"):
            mod.memory_agents(action=action, lease="claim:x", children=["tests"])


def test_children_survive_a_client_that_stringifies_the_list():
    """Claude Code sends list parameters as JSON text; the tool's argument
    model must still read a list, and an empty one."""
    from pseudolife_memory import mcp_server as mod
    tool = mod.mcp._tool_manager.get_tool("memory_agents")
    meta = tool.fn_metadata
    for raw, expected in (('["tests", "docs"]', ["tests", "docs"]), ("[]", [])):
        parsed = meta.arg_model.model_validate(meta.pre_parse_json({"children": raw}))
        assert parsed.children == expected


def test_checkin_keeps_subagents_off_the_parents_board_writes():
    """A subagent shares its parent's shim, so its board writes would carry
    the parent's identity (probed 2026-09-27). Both served forms say so."""
    from pseudolife_memory.coordination import CHECKIN_INSTRUCTION, CHECKIN_TEXT
    assert "subagent" in CHECKIN_TEXT.lower()
    for word in ("list", "receive", "ack", "send", "status"):
        assert word in CHECKIN_TEXT.split("ubagent", 1)[1]
    assert "subagent" in CHECKIN_INSTRUCTION.lower()
