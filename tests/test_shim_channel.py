"""Optional channel transport preserves the shim's forwarding contract."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from pseudolife_memory import shim


THREAD_ID = "aaaaaaaa-1111-4111-8111-111111111111"


def test_channel_proxy_preserves_upstream_instructions_and_tools(monkeypatch):
    from mcp.client import session, streamable_http
    from mcp.server import stdio
    import sys

    seen = {}

    @asynccontextmanager
    async def http_client(**kwargs):
        seen["headers"] = kwargs["headers"]
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object()

    class Remote:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            return SimpleNamespace(instructions="Fixture daemon instructions")

    @asynccontextmanager
    async def inbox():
        yield None

    async def serve_channel(server, read, write, inbox_factory, **kwargs):
        seen["instructions"] = server.instructions
        seen["inbox"] = inbox_factory
        seen["options"] = kwargs["notification_options"]

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setitem(sys.modules, "pseudolife_memory.channel",
                        SimpleNamespace(serve_channel=serve_channel))

    asyncio.run(shim._proxy("http://fixture.invalid", "fixture-token",
                            "fixture-session", channel_inbox=inbox))
    assert "Fixture daemon instructions" in seen["instructions"]
    assert "user approval" in seen["instructions"]
    assert seen["inbox"] is inbox
    assert seen["options"].tools_changed is True
    assert seen["headers"]["X-PL-Session"] == "fixture-session"
    assert seen["headers"]["Authorization"] == "Bearer fixture-token"


def test_channel_cli_is_explicit_and_leaves_ordinary_shim_default(monkeypatch):
    from pseudolife_memory import cli
    import sys

    calls = []
    monkeypatch.setattr(shim, "run_shim", lambda **kw: calls.append(kw))
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp"])
    cli.main()
    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "channel"])
    cli.main()
    assert calls == [{}, {"channel": True}]


def test_opted_in_shim_injects_private_instance_identity(monkeypatch):
    from pseudolife_memory import coordination_adapter
    seen = {}
    class Adapter:
        def __init__(self, *args, **kwargs):
            seen["options"] = kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        instance_headers = {"X-PL-Agent": "a1", "X-PL-Agent-Key": "private-fixture"}
        unread_hint = None
        async def inbox(self):
            yield
    async def proxy(*args, **kwargs):
        seen["proxy"] = kwargs
    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", Adapter)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_WAKE", "1")
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token", "session", channel=True))
    assert seen["options"]["wake_enabled"] is True
    assert seen["proxy"]["agent_headers"] == Adapter.instance_headers
    assert seen["proxy"]["channel_inbox"] is not None


def test_slow_adapter_startup_falls_back_after_cancellation(monkeypatch):
    from pseudolife_memory import coordination_adapter
    called = []
    class SlowAdapter:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            await asyncio.Event().wait()
        async def __aexit__(self, *a):
            pass
    async def proxy(*a, **kw):
        called.append(kw)
    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", SlowAdapter)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setattr(shim, "_ADAPTER_STARTUP_SECONDS", 0.01, raising=False)
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    asyncio.run(asyncio.wait_for(shim._run_session_proxy("http://fixture", "token", "s"), 0.2))
    assert called == [{}]


def test_codex_tool_metadata_overrides_session_and_attaches_lazily(monkeypatch):
    from mcp import types
    from mcp.client import session, streamable_http
    from mcp.server import Server, stdio

    headers = []
    requested = []

    @asynccontextmanager
    async def http_client(**kwargs):
        headers.append(dict(kwargs["headers"]))
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object()

    class Remote:
        def __init__(self, *args):
            self._tool_output_schemas = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            return SimpleNamespace(instructions="fixture")

        async def call_tool(self, name, arguments):
            return types.CallToolResult(content=[], is_error=False)

    class Adapter:
        instance_headers = {
            "X-PL-Agent": "fixture-agent",
            "X-PL-Agent-Key": "private-fixture",
        }
        unread_hint = None

    class Registry:
        async def get(self, thread_id):
            requested.append(thread_id)
            return Adapter()
        def unread_hint(self, thread_id, adapter):
            return adapter.unread_hint

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        await handler.handler(None, types.CallToolRequestParams(
            name="memory_stats", arguments={}, _meta={"threadId": THREAD_ID}))

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setattr(Server, "run", serve)

    asyncio.run(shim._proxy(
        "http://fixture.invalid", "fixture-token", "process-session",
        codex_metadata=True, coordination_registry=Registry()))

    assert requested == [THREAD_ID]
    assert headers[0]["X-PL-Session"] == "process-session"  # startup has no call metadata
    assert headers[1]["X-PL-Session"] == THREAD_ID
    assert headers[1]["X-PL-Agent"] == "fixture-agent"
    assert headers[1]["X-PL-Agent-Key"] == "private-fixture"


def test_invalid_codex_metadata_never_uses_coordination_identity(monkeypatch):
    from mcp import types
    from mcp.client import session, streamable_http
    from mcp.server import Server, stdio

    headers = []

    @asynccontextmanager
    async def http_client(**kwargs):
        headers.append(dict(kwargs["headers"]))
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object()

    class Remote:
        def __init__(self, *args):
            self._tool_output_schemas = {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): return SimpleNamespace(instructions="fixture")
        async def call_tool(self, name, arguments):
            return types.CallToolResult(content=[], is_error=False)

    class Registry:
        async def get(self, thread_id):
            pytest.fail("invalid metadata must not select persisted credentials")

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        await handler.handler(None, types.CallToolRequestParams(
            name="memory_stats", arguments={}, _meta={"threadId": "../../shared"}))

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setattr(Server, "run", serve)

    asyncio.run(shim._proxy(
        "http://fixture.invalid", "fixture-token", "process-session",
        codex_metadata=True, coordination_registry=Registry()))

    assert headers[1]["X-PL-Session"] == "process-session"
    assert "X-PL-Agent" not in headers[1]


def test_codex_pull_mode_builds_lazy_registry_and_closes_it(monkeypatch):
    from pseudolife_memory import codex_coordination, coordination_adapter

    seen = {}

    class Registry:
        def __init__(self, *args, **kwargs):
            seen["registry_args"] = args
            seen["registry_options"] = kwargs
            self.closed = False
        async def aclose(self):
            self.closed = True

    class NeverEager:
        def __init__(self, *args, **kwargs):
            pytest.fail("Codex pull mode must wait for host thread metadata")

    async def proxy(*args, **kwargs):
        seen["proxy"] = kwargs

    monkeypatch.setattr(codex_coordination, "CodexCoordinationRegistry", Registry)
    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", NeverEager)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    monkeypatch.delenv("PSEUDOLIFE_AGENT_STATE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_AGENT_WAKE", raising=False)

    asyncio.run(shim._run_session_proxy(
        "http://fixture.invalid", "fixture-token", "process-session"))

    registry = seen["proxy"]["coordination_registry"]
    assert seen["proxy"]["codex_metadata"] is True
    assert seen["registry_args"] == ("http://fixture.invalid", "fixture-token")
    assert registry.closed is True


def test_codex_wake_requires_explicit_authenticated_bridge(monkeypatch):
    from pseudolife_memory import codex_coordination
    seen = {}

    class Registry:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
        async def aclose(self): pass

    async def proxy(*args, **kwargs): pass

    monkeypatch.setattr(codex_coordination, "CodexCoordinationRegistry", Registry)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_WAKE", "true")
    monkeypatch.setenv("PSEUDOLIFE_CODEX_SERVER_URL", "ws://127.0.0.1:9999")
    monkeypatch.setenv("PSEUDOLIFE_CODEX_SERVER_TOKEN", "private-host-token")
    monkeypatch.delenv("PSEUDOLIFE_AGENT_STATE", raising=False)

    asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))

    assert seen["delivery_url"] == "ws://127.0.0.1:9999"
    assert seen["delivery_token"] == "private-host-token"


def test_codex_wake_without_bridge_credentials_falls_back_to_pull(monkeypatch, capsys):
    from pseudolife_memory import codex_coordination
    seen = {}

    class Registry:
        def __init__(self, *args, **kwargs): seen.update(kwargs)
        async def aclose(self): pass

    async def proxy(*args, **kwargs): pass

    monkeypatch.setattr(codex_coordination, "CodexCoordinationRegistry", Registry)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_WAKE", "1")
    monkeypatch.delenv("PSEUDOLIFE_CODEX_SERVER_URL", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_CODEX_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_AGENT_STATE", raising=False)

    asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))

    assert "delivery_url" not in seen
    assert "using pull coordination" in capsys.readouterr().err


def test_codex_wake_rejects_reused_bank_credential(monkeypatch, capsys):
    from pseudolife_memory import codex_coordination
    seen = {}

    class Registry:
        def __init__(self, *args, **kwargs): seen.update(kwargs)
        async def aclose(self): pass

    async def proxy(*args, **kwargs): pass

    monkeypatch.setattr(codex_coordination, "CodexCoordinationRegistry", Registry)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_WAKE", "1")
    monkeypatch.setenv("PSEUDOLIFE_CODEX_SERVER_URL", "ws://127.0.0.1:9999")
    monkeypatch.setenv("PSEUDOLIFE_CODEX_SERVER_TOKEN", "fixture-shared-secret")
    monkeypatch.delenv("PSEUDOLIFE_AGENT_STATE", raising=False)

    asyncio.run(shim._run_session_proxy(
        "http://fixture", "fixture-shared-secret", "process-session"))

    assert "delivery_url" not in seen
    warning = capsys.readouterr().err
    assert "using pull coordination" in warning
    assert "fixture-shared-secret" not in warning


def test_codex_session_metadata_is_used_without_coordination_opt_in(monkeypatch):
    seen = {}

    async def proxy(*args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.delenv("PSEUDOLIFE_AGENT_COORDINATION", raising=False)
    asyncio.run(shim._run_session_proxy("http://fixture", None, "process-session"))

    assert seen == {"codex_metadata": True}


def test_codex_automatic_mode_rejects_one_fixed_state_file(monkeypatch, capsys):
    from pseudolife_memory import codex_coordination

    seen = {}

    class NeverRegistry:
        def __init__(self, *args, **kwargs):
            pytest.fail("a fixed path could share credentials across Codex threads")

    async def proxy(*args, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(codex_coordination, "CodexCoordinationRegistry", NeverRegistry)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_STATE", "shared.json")

    asyncio.run(shim._run_session_proxy("http://fixture", "token", "process-session"))

    assert seen == {"codex_metadata": True}
    assert "PSEUDOLIFE_AGENT_STATE_DIR" in capsys.readouterr().err


def test_codex_channel_keeps_eager_channel_adapter(monkeypatch):
    from pseudolife_memory import coordination_adapter
    seen = {}

    class Adapter:
        def __init__(self, *args, **kwargs):
            seen["options"] = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = None
        async def inbox(self): yield

    async def proxy(*args, **kwargs):
        seen["proxy"] = kwargs

    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", Adapter)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", "1")
    asyncio.run(shim._run_session_proxy(
        "http://fixture", "token", "process-session", channel=True))

    assert "coordination_registry" not in seen["proxy"]
    assert "codex_metadata" not in seen["proxy"]
    assert seen["proxy"]["agent_headers"] == Adapter.instance_headers


@pytest.mark.parametrize("callback_enabled,hint", [(False, None), (True, None), (True, ""),
                                                   (True, "Coordination: pending messages; use receive.")])
@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize("structured_content", [
    None,
    {"bands": {"flat": 3}},
    {"bands": {"flat": 3}, "coordination_hint": "Upstream coordination value"},
])
def test_cached_unread_hint_preserves_upstream_result_without_network(
        monkeypatch, callback_enabled, hint, is_error, structured_content):
    from mcp import types
    from mcp.client import session, streamable_http
    from mcp.server import Server, stdio

    upstream = types.CallToolResult(
        content=[types.TextContent(type="text", text="Original daemon response")],
        structured_content=structured_content, is_error=is_error,
        _meta={"fixture": "preserved"})
    original = upstream.model_dump(by_alias=True)
    seen = {"http_clients": 0, "calls": [], "hint_reads": 0}

    @asynccontextmanager
    async def http_client(**kwargs):
        seen["http_clients"] += 1
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object()

    class Remote:
        def __init__(self, *args):
            self._tool_output_schemas = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            return SimpleNamespace(instructions="Fixture daemon instructions")

        async def call_tool(self, name, arguments):
            seen["calls"].append((name, arguments))
            return upstream

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        seen["result"] = await handler.handler(None, types.CallToolRequestParams(
            name="memory_stats", arguments={"detail": True}))

    def read_hint():
        seen["hint_reads"] += 1
        return hint

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setattr(Server, "run", serve)
    asyncio.run(shim._proxy("http://fixture.invalid", "fixture-token", "fixture-session",
                            coordination_hint=read_hint if callback_enabled else None))

    result = seen["result"].model_dump(by_alias=True)
    expected_non_content = {key: value for key, value in original.items() if key != "content"}
    if (callback_enabled and hint and structured_content is not None
            and "coordination_hint" not in structured_content):
        expected_non_content["structuredContent"] = {
            **structured_content, "coordination_hint": hint}
    assert {key: value for key, value in result.items() if key != "content"} == expected_non_content
    expected_content = list(original["content"])
    if callback_enabled and hint:
        expected_content.append(types.TextContent(type="text", text=hint).model_dump(by_alias=True))
    assert result["content"] == expected_content
    assert upstream.model_dump(by_alias=True) == original
    assert seen["calls"] == [("memory_stats", {"detail": True})]
    assert seen["http_clients"] == 2  # Startup instructions and the actual tool call only.
    assert seen["hint_reads"] == int(callback_enabled)
