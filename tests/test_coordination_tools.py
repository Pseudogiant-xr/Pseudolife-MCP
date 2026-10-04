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


def test_message_history_surface_is_additive_and_instance_scoped(monkeypatch):
    from pseudolife_memory import mcp_server as mod, coordination
    seen = []
    monkeypatch.setattr(coordination, "dispatch", lambda svc, action, args: seen.append(
        (action, args)) or {"messages": [], "has_more": False})
    mod.memory_message(action="history", peer="peer-id", limit=2, after="own:history:1")
    assert seen == [("history", {"peer": "peer-id", "limit": 2, "after": "own:history:1"})]
    tool = mod.mcp._tool_manager.get_tool("memory_message")
    parsed = tool.fn_metadata.arg_model.model_validate({"action": "history", "limit": 2})
    assert parsed.action == "history" and parsed.limit == 2
    with pytest.raises(TypeError):
        mod.memory_message(action="history", agent_id="other")


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


def test_agents_update_passes_park_fields_and_refuses_them_elsewhere(monkeypatch):
    from pseudolife_memory import mcp_server as mod, coordination
    seen = []
    monkeypatch.setattr(coordination, "dispatch", lambda svc, action, args: seen.append(
        (action, args)) or {})
    mod.memory_agents(action="update", park_reason="blocked", park_needs="GPU free",
                      park_clear_by="anyone", park_resume="rerun the bench", park_expires=1.5e9)
    # An empty reason is the tool's way to say null: it clears the park.
    mod.memory_agents(action="update", park_reason="")
    assert seen == [("update", {"park_reason": "blocked", "park_needs": "GPU free",
                                "park_clear_by": "anyone", "park_resume": "rerun the bench",
                                "park_expires": 1.5e9}),
                    ("update", {"park_reason": None})]
    for action in ("list", "claim", "release"):
        with pytest.raises(ValueError, match="unexpected_parameter"):
            mod.memory_agents(action=action, lease="claim:x", park_reason="done")


def test_message_send_passes_clears_and_urgent(monkeypatch):
    from pseudolife_memory import mcp_server as mod, coordination
    seen = []
    monkeypatch.setattr(coordination, "dispatch", lambda svc, action, args: seen.append(
        (action, args)) or {"state": "queued", "wake": {"decision": "rung", "reason": "clears"}})
    out = mod.memory_message(action="send", to="peer", text="GPU is free", request_id="r1",
                             clears="GPU free", urgent=True)
    assert out["wake"]["decision"] == "rung"
    mod.memory_message(action="send", to="peer", text="fyi", request_id="r2")
    assert seen == [("send", {"to": "peer", "text": "GPU is free", "request_id": "r1",
                              "clears": "GPU free", "urgent": True}),
                    ("send", {"to": "peer", "text": "fyi", "request_id": "r2"})]


def test_sessions_are_asked_to_park_where_the_text_is_not_benched():
    """The park request reaches a session through the tool description and
    the Stop hook's gate. The check-in sentence is kept ready but not
    served: CHECKIN_TEXT is pinned to the text the check-in bench measured
    (#435), so it joins only with a new bench run."""
    from pseudolife_memory import mcp_server as mod
    from pseudolife_memory.coordination import (
        CHECKIN_TEXT, PARK_CHECKIN_SENTENCE, PARK_GATE_MESSAGE)
    for text in (PARK_CHECKIN_SENTENCE, PARK_GATE_MESSAGE, mod.memory_agents.__doc__):
        for field in ("park_reason", "park_needs", "park_clear_by", "park_resume"):
            assert field in text, (field, text[:40])
    assert PARK_CHECKIN_SENTENCE not in CHECKIN_TEXT


# ── refusals name what was wrong (review 2026-10-04, M1) ──────────────────


_HEADERS = {"authorization": "Bearer fixture-secret", "x-pl-agent": "own",
            "x-pl-agent-key": "private-fixture"}


def _board_service():
    from contextlib import nullcontext
    return SimpleNamespace(config=SimpleNamespace(coordination=SimpleNamespace(
        enabled=True, allowed_principals=["default"], audit_retention_days=90)),
        _lock=nullcontext(), _storage=object(), _ensure_init=lambda: None,
        _hlc=SimpleNamespace(tick=lambda: (100, 1)))


def _refusal(name, args):
    """A board tool called through FastMCP: its refusal must reach the
    client as an MCP tool error whose JSON names the code."""
    import asyncio
    import json
    from pseudolife_memory import mcp_server as mod
    result = asyncio.run(mod.mcp.call_tool(name, args))
    text = "".join(item.text for item in result.content if hasattr(item, "text"))
    assert result.is_error, text
    assert "CoordinationRefused" not in text and "ValueError" not in text
    return json.loads(text)


def test_send_without_request_id_names_the_missing_parameter(monkeypatch):
    """Every send without request_id in the 2026-10-04 transcripts was
    retried by guessing; the refusal now says which parameter is missing."""
    from pseudolife_memory import coordination
    real = coordination.dispatch
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    monkeypatch.setattr(coordination, "dispatch", lambda svc, action, params: real(
        _board_service(), action, params, headers=_HEADERS))
    out = _refusal("memory_message", {"action": "send", "to": "peer", "text": "hi"})
    assert out["error"] == "missing_parameter"
    assert (out["param"], out["action"]) == ("request_id", "send")
    assert "request_id" in out["message"] and "send" in out["message"]


def test_dispatch_unexpected_parameter_names_it_and_the_accepted_ones(monkeypatch):
    from pseudolife_memory.coordination import CoordinationRefused, dispatch
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    with pytest.raises(CoordinationRefused) as caught:
        dispatch(_board_service(), "ack", {"message_id": "m1", "text": "hi"},
                 headers=_HEADERS)
    refused = caught.value
    assert refused.code == "unexpected_parameter"
    assert refused.param == "text" and refused.accepted == ["message_id"]
    assert "ack" in refused.detail and "text" in refused.detail
    assert "message_id" in refused.detail


def test_dispatch_unknown_action_lists_the_accepted_actions(monkeypatch):
    from pseudolife_memory.coordination import CoordinationRefused, dispatch
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    with pytest.raises(CoordinationRefused) as caught:
        dispatch(_board_service(), "shout", {}, headers=_HEADERS)
    refused = caught.value
    assert refused.code == "unknown_coordination_action"
    assert "send" in refused.accepted and "receive" in refused.accepted
    assert "shout" not in refused.detail and "send" in refused.detail


def test_agents_refusals_name_the_parameter():
    """memory_agents' own argument checks name the parameter and, where
    one action takes a fixed set, the accepted ones."""
    missing = _refusal("memory_agents", {"action": "claim"})
    assert missing["error"] == "missing_parameter"
    assert (missing["param"], missing["action"]) == ("lease", "claim")
    assert "lease" in missing["message"]
    extra = _refusal("memory_agents", {"action": "list", "status": "busy"})
    assert extra["error"] == "unexpected_parameter"
    assert (extra["param"], extra["action"]) == ("status", "list")
    assert "status" in extra["message"] and "update" in extra["message"]
    park = _refusal("memory_agents", {"action": "claim", "lease": "claim:x",
                                      "park_reason": "done"})
    assert park["error"] == "unexpected_parameter" and park["param"] == "park_reason"


def test_an_unknown_park_reason_reaches_the_model_with_the_accepted_ones(monkeypatch):
    """The storage refusal's detail, param and accepted list survive the
    dispatch boundary into the MCP tool error."""
    from pseudolife_memory import coordination
    from pseudolife_memory.storage.coordination import PARK_REASONS, CoordinationStore
    real = coordination.dispatch

    class Store:
        def update(self, principal, agent_id, credential, **fields):
            return CoordinationStore._fields(**fields)

    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-secret")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    monkeypatch.setattr(coordination, "_store", lambda svc: Store())
    monkeypatch.setattr(coordination, "dispatch", lambda svc, action, params: real(
        _board_service(), action, params, headers=_HEADERS))
    out = _refusal("memory_agents", {"action": "update", "park_reason": "sleeping"})
    assert out["error"] == "invalid_park" and out["param"] == "park_reason"
    assert out["accepted"] == list(PARK_REASONS)
    assert all(reason in out["message"] for reason in PARK_REASONS)
