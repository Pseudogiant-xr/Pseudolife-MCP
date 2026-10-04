"""An argument name a tool does not have is refused, not silently dropped.

The 2026-10-04 transcript review measured models sending ``limit=`` to
``memory_search`` (68 Claude calls, 72 Codex calls, 56 more on
``memory_lesson_search``), ``content=`` to ``memory_store``, ``note=`` /
``notes=`` / ``text=`` to ``memory_outcome`` and ``episode=`` to tools with
no episode parameter. The argument models ignored unknown names, so each
call succeeded with the argument gone. These tests drive the calls through
an in-process MCP client, the same ``tools/call`` path a real client takes.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.helpers import reload_mcp_filemode


def _call(mod, tool: str, args: dict, **kwargs):
    from mcp.client import Client

    async def go():
        async with Client(mod.mcp) as client:
            return await client.call_tool(tool, args, **kwargs)
    return asyncio.run(go())


def _list_tools(mod):
    from mcp.client import Client

    async def go():
        async with Client(mod.mcp) as client:
            return (await client.list_tools()).tools
    return asyncio.run(go())


def _text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def _never_called(mod, monkeypatch, method: str) -> list:
    calls: list = []
    monkeypatch.setattr(mod.service, method,
                        lambda *a, **k: calls.append((a, k)) or {"ok": True})
    return calls


def test_search_with_limit_is_an_error_naming_top_k(tmp_path, monkeypatch):
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    calls = _never_called(mod, monkeypatch, "search")
    result = _call(mod, "memory_search", {"query": "release steps", "limit": 3})
    assert result.is_error, _text(result)
    text = _text(result)
    assert "unknown parameter 'limit' for memory_search" in text
    assert "did you mean 'top_k'?" in text
    assert "Accepted: query, top_k," in text
    assert calls == []


@pytest.mark.parametrize("tool,wrong,right,args", [
    ("memory_lesson_search", "limit", "top_k", {"query": "q"}),
    ("memory_recent", "limit", "n", {}),
    ("memory_store", "content", "text", {"text": "kept"}),
    ("memory_store", "content", "text", {}),
    ("memory_outcome", "note", "detail", {"task": "t", "outcome": "success"}),
    ("memory_outcome", "notes", "detail", {"task": "t", "outcome": "success"}),
    ("memory_outcome", "text", "detail", {"task": "t", "outcome": "success"}),
    ("memory_search", "querry", "query", {}),  # difflib, no alias
])
def test_measured_confusions_get_a_suggestion(
        tmp_path, monkeypatch, tool, wrong, right, args):
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    result = _call(mod, tool, {**args, wrong: "x"})
    assert result.is_error, _text(result)
    text = _text(result)
    assert f"unknown parameter '{wrong}' for {tool}" in text
    assert f"did you mean '{right}'?" in text


def test_an_alias_never_shadows_a_real_parameter(tmp_path, monkeypatch):
    """``limit`` is a real memory_message parameter and ``text`` a real
    memory_store one: the alias map only speaks where the name is unknown."""
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    store = _never_called(mod, monkeypatch, "store")
    result = _call(mod, "memory_store", {"text": "kept"})
    assert not result.is_error, _text(result)
    assert len(store) == 1
    tools = {t.name: t for t in _list_tools(mod)}
    assert "limit" in tools["memory_message"].input_schema["properties"]


def test_episode_on_a_tool_without_one_is_refused(tmp_path, monkeypatch):
    """World, set and resolve writes take no episode (attribution comes from
    the transport session where the service stamps one); the standing texts
    say to pass it only where a tool accepts it."""
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    calls = _never_called(mod, monkeypatch, "world_write")
    result = _call(mod, "memory_world_set", {
        "entity": "python", "attribute": "latest", "value": "3.14",
        "episode": "abcdef123456"})
    assert result.is_error, _text(result)
    text = _text(result)
    assert "unknown parameter 'episode' for memory_world_set" in text
    assert "episode" not in text.split("Accepted:", 1)[1]
    assert calls == []


def test_episode_still_reaches_a_tool_that_takes_it(tmp_path, monkeypatch):
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(mod.service, "cortex_write",
                        lambda *a, **k: seen.append(k) or {"action": "inserted"})
    result = _call(mod, "memory_fact_set", {
        "entity": "e", "attribute": "a", "value": "v", "episode": "abcdef123456"})
    assert not result.is_error, _text(result)
    assert seen[0]["episode"] == "abcdef123456"


def test_every_unknown_name_is_reported(tmp_path, monkeypatch):
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    result = _call(mod, "memory_search", {"query": "q", "limit": 3, "page": 2})
    assert result.is_error
    text = _text(result)
    assert "'limit'" in text and "'page'" in text


def test_request_meta_is_not_an_argument(tmp_path, monkeypatch):
    """MCP ``_meta`` (Codex's thread id, progress tokens) travels beside
    ``arguments``, never inside it, so the refusal cannot catch it."""
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    calls: list = []
    monkeypatch.setattr(mod.service, "search",
                        lambda *a, **k: calls.append(k) or {"count": 0, "entries": []})
    result = _call(mod, "memory_search", {"query": "q"},
                   meta={"threadId": "019a0000-0000-7000-8000-000000000000"})
    assert not result.is_error, _text(result)
    assert len(calls) == 1


def test_stringified_lists_are_still_decoded(tmp_path, monkeypatch):
    """Claude Code sends anyOf list parameters as JSON text; the strict
    check must run beside the string-safe pre-parse, not replace it."""
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(mod.service, "store",
                        lambda **k: seen.append(k) or {"stored": True})
    result = _call(mod, "memory_store",
                   {"text": "t", "tags": json.dumps(["decision", "123"])})
    assert not result.is_error, _text(result)
    assert seen[0]["tags"] == ["decision", "123"]


def test_every_schema_forbids_unknown_names(tmp_path, monkeypatch):
    mod = reload_mcp_filemode(tmp_path, monkeypatch)
    for tool in mod.mcp._tool_manager.list_tools():
        assert tool.parameters.get("additionalProperties") is False, tool.name
