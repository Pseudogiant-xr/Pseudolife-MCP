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
        def deliver_hint(self): return None
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
    assert seen["options"]["provider"] is seen["proxy"]["provider"]
    assert (seen["options"]["initial_snapshot"].generation
            == seen["options"]["provider"].snapshot().generation)
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
    assert len(called) == 1
    assert repr(called[0].pop("provider")) == "CredentialProvider(source='static')"
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
            "X-PL-Bank": "fixture-bank",
            "X-PL-Principal": "fixture-principal",
        }
        unread_hint = None
        def deliver_hint(self): return None
        def note_turn(self): pass

    noted = []

    class Registry:
        async def get(self, thread_id, *, snapshot):
            requested.append(thread_id)
            return Adapter()
        def unread_hint(self, thread_id, adapter):
            return adapter.unread_hint
        def note_call(self, thread_id, name, arguments, *, succeeded=False):
            noted.append((thread_id, name, arguments, succeeded))

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
    # The doorbell's view of activity: the call's start, then its success.
    assert noted == [(THREAD_ID, "memory_stats", {}, False), (THREAD_ID, "memory_stats", {}, True)]
    assert headers[0]["X-PL-Session"] == "process-session"  # startup has no call metadata
    assert headers[1]["X-PL-Session"] == THREAD_ID
    assert headers[1]["X-PL-Agent"] == "fixture-agent"
    assert headers[1]["X-PL-Agent-Key"] == "private-fixture"
    assert headers[1]["X-PL-Bank"] == "fixture-bank"
    assert headers[1]["X-PL-Principal"] == "fixture-principal"


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


def test_coordination_tool_fails_closed_when_thread_identity_is_unavailable(monkeypatch):
    from mcp import types
    from mcp.client import session, streamable_http
    from mcp.server import Server, stdio

    seen = {"clients": 0, "calls": 0}

    @asynccontextmanager
    async def http_client(**kwargs):
        seen["clients"] += 1
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object()

    class Remote:
        def __init__(self, *args): self._tool_output_schemas = {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): return SimpleNamespace(instructions="fixture")
        async def call_tool(self, name, arguments):
            seen["calls"] += 1
            return types.CallToolResult(content=[], is_error=False)

    class Registry:
        async def get(self, thread_id, *, snapshot): return None
        def unread_hint(self, thread_id, adapter):
            return "Coordination state needs to be reattached."

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        with pytest.raises(Exception) as caught:
            await handler.handler(None, types.CallToolRequestParams(
                name="memory_message", arguments={"action": "receive"},
                _meta={"threadId": THREAD_ID}))
        assert caught.value.data == {
            "classification": "coordination_unavailable",
            "phase": "initialize",
            "operation_outcome": "not_dispatched",
            "hint": "Coordination state needs to be reattached.",
        }

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setattr(Server, "run", serve)

    asyncio.run(shim._proxy(
        "http://fixture.invalid", "fixture-token", "process-session",
        codex_metadata=True, coordination_registry=Registry()))
    assert seen == {"clients": 1, "calls": 0}


def test_channel_context_mismatch_omits_identity_and_blocks_coordination_tool(monkeypatch):
    from mcp import types
    from mcp.client import session, streamable_http
    from mcp.server import Server, stdio

    seen = {"headers": [], "calls": []}

    @asynccontextmanager
    async def http_client(**kwargs):
        seen["headers"].append(dict(kwargs["headers"]))
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object()

    class Remote:
        def __init__(self, *args): self._tool_output_schemas = {}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): return SimpleNamespace(instructions="fixture")
        async def call_tool(self, name, arguments):
            seen["calls"].append(name)
            return types.CallToolResult(content=[], is_error=False)

    class Adapter:
        instance_headers = {
            "X-PL-Agent": "fixture-agent",
            "X-PL-Agent-Key": "private-key",
            "X-PL-Bank": "old-bank",
            "X-PL-Principal": "old-principal",
        }
        async def validate_snapshot(self, snapshot):
            raise RuntimeError("private wrong-bank detail")

    async def serve(server, *args, **kwargs):
        handler = server.get_request_handler("tools/call")
        ordinary = await handler.handler(None, types.CallToolRequestParams(
            name="memory_stats", arguments={}))
        assert ordinary.is_error is False
        with pytest.raises(Exception) as caught:
            await handler.handler(None, types.CallToolRequestParams(
                name="memory_message", arguments={"action": "receive"}))
        assert caught.value.data == {
            "classification": "coordination_unavailable",
            "phase": "initialize",
            "operation_outcome": "not_dispatched",
            "hint": "Coordination context changed; reattach it.",
        }
        assert "wrong-bank" not in repr(caught.value)

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setattr(Server, "run", serve)

    adapter = Adapter()
    asyncio.run(shim._proxy(
        "http://fixture.invalid", "fixture-token", "process-session",
        coordination_adapter=adapter,
        coordination_hint=lambda: "Coordination context changed; reattach it."))
    assert seen["calls"] == ["memory_stats"]
    assert len(seen["headers"]) == 2  # instructions + ordinary call
    assert all(not any(name in headers for name in Adapter.instance_headers)
               for headers in seen["headers"])


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
    assert seen["registry_options"]["provider"] is seen["proxy"]["provider"]
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

    assert seen.pop("codex_metadata") is True
    assert repr(seen.pop("provider")) == "CredentialProvider(source='static')"
    assert seen == {}


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

    assert seen.pop("codex_metadata") is True
    assert repr(seen.pop("provider")) == "CredentialProvider(source='static')"
    assert seen == {}
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
        def deliver_hint(self): return None
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


class _BoardAdapter:
    """A registered adapter; records that the shim built one."""
    built = []

    def __init__(self, *args, **kwargs):
        _BoardAdapter.built.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    instance_headers = {"X-PL-Agent": "a1", "X-PL-Agent-Key": "private-fixture"}

    def deliver_hint(self):
        return None


def _board_env(monkeypatch, value, available=True):
    """``available`` is the daemon's answer to the default-mode probe."""
    from pseudolife_memory import coordination_adapter
    _BoardAdapter.built = []
    seen = {"probes": []}

    async def proxy(*args, **kwargs):
        seen.update(kwargs)

    def probe(url, provider):
        seen["probes"].append(url)
        return available

    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", _BoardAdapter)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setattr(shim, "_board_available", probe)
    monkeypatch.delenv("PSEUDOLIFE_WRITER_ID", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_AGENT_STATE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_AGENT_STATE_DIR", raising=False)
    if value is None:
        monkeypatch.delenv("PSEUDOLIFE_AGENT_COORDINATION", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", value)
    return seen


def test_board_adapter_is_on_by_default_where_the_daemon_serves_the_board(monkeypatch):
    seen = _board_env(monkeypatch, None)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token", "s"))
    assert seen["probes"] == ["http://fixture.invalid"]
    assert len(_BoardAdapter.built) == 1
    assert seen["coordination_adapter"] is not None
    assert seen["board_checkin"] is True


def test_board_default_stays_quiet_where_the_daemon_refuses_it(monkeypatch, capsys):
    """Disabled, an unlisted principal or file mode: the default asks the
    daemon once and builds nothing, so no refused register, no warning."""
    seen = _board_env(monkeypatch, None, available=False)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token", "s"))
    assert seen["probes"] == ["http://fixture.invalid"]
    assert _BoardAdapter.built == []
    assert "coordination_adapter" not in seen and "board_checkin" not in seen
    assert "coordination" not in capsys.readouterr().err


def test_board_default_stays_quiet_without_a_bearer(monkeypatch, capsys):
    """An open install cannot use the board (bearer auth stays required), so
    the default neither asks, nor builds an adapter, nor warns."""
    seen = _board_env(monkeypatch, None)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", None, "s"))
    assert seen["probes"] == [] and _BoardAdapter.built == []
    assert "coordination_adapter" not in seen and "board_checkin" not in seen
    assert "coordination" not in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_explicit_opt_out_keeps_the_board_adapter_off(monkeypatch, value):
    seen = _board_env(monkeypatch, value)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token", "s"))
    assert seen["probes"] == [] and _BoardAdapter.built == []
    assert "coordination_adapter" not in seen and "board_checkin" not in seen


def test_explicit_opt_in_builds_the_adapter_without_asking(monkeypatch):
    """An explicit opt-in keeps its diagnostics: the adapter's own refusal
    says why, where a probe would only say no."""
    seen = _board_env(monkeypatch, "1", available=False)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token", "s"))
    assert seen["probes"] == [] and len(_BoardAdapter.built) == 1
    assert seen["board_checkin"] is True


def test_failed_board_adapter_leaves_the_checkin_out(monkeypatch):
    from pseudolife_memory import coordination_adapter
    seen = _board_env(monkeypatch, None)

    class Refused(_BoardAdapter):
        async def __aenter__(self):
            raise coordination_adapter.AdapterError("coordination register refused (HTTP 403)")

    monkeypatch.setattr(coordination_adapter, "CoordinationAdapter", Refused)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token", "s"))
    assert "coordination_adapter" not in seen and "board_checkin" not in seen


def _codex_board_env(monkeypatch, value, available):
    from pseudolife_memory import codex_coordination
    seen = {"probes": []}

    class Registry:
        def __init__(self, *args, **kwargs):
            seen["registry"] = True

        async def aclose(self):
            pass

    async def proxy(*args, **kwargs):
        seen.update(kwargs)

    def probe(url, provider):
        seen["probes"].append(url)
        return available

    monkeypatch.setattr(codex_coordination, "CodexCoordinationRegistry", Registry)
    monkeypatch.setattr(shim, "_proxy", proxy)
    monkeypatch.setattr(shim, "_board_available", probe)
    monkeypatch.setenv("PSEUDOLIFE_WRITER_ID", "codex")
    monkeypatch.delenv("PSEUDOLIFE_AGENT_STATE", raising=False)
    if value is None:
        monkeypatch.delenv("PSEUDOLIFE_AGENT_COORDINATION", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", value)
    return seen


@pytest.mark.parametrize("available", [True, False])
def test_codex_default_builds_its_registry_only_where_the_daemon_serves_the_board(
        monkeypatch, available):
    """A registry the daemon refuses would retry every tool call and append
    a coordination error hint to each result, so the default asks first."""
    seen = _codex_board_env(monkeypatch, None, available)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token", "s"))
    assert seen["probes"] == ["http://fixture.invalid"]
    if available:
        assert seen["registry"] is True and seen["coordination_registry"] is not None
        assert seen["board_checkin"] is True
    else:
        assert "registry" not in seen and "coordination_registry" not in seen
        assert "board_checkin" not in seen


def test_codex_explicit_opt_in_keeps_its_registry_and_probes_for_the_checkin(monkeypatch):
    seen = _codex_board_env(monkeypatch, "1", True)
    asyncio.run(shim._run_session_proxy("http://fixture.invalid", "fixture-token", "s"))
    assert seen["registry"] is True and seen["coordination_registry"] is not None
    # The registry attaches per thread later; the instructions ask the daemon.
    assert callable(seen["board_checkin"]) and seen["probes"] == []
    assert asyncio.run(seen["board_checkin"]()) is True
    assert seen["probes"] == ["http://fixture.invalid"]


@pytest.mark.parametrize("status,body,expected", [
    (200, b"Pseudolife coordination: check in.\n", True),
    (200, b"", False),
    (401, b"unauthorized", False),
    (None, None, False),
])
def test_board_probe_reads_the_startup_checkin_route(monkeypatch, status, body, expected):
    import io
    import urllib.error
    from pseudolife_memory.credentials import CredentialProvider
    requests = []
    openers = []

    class Opener:
        def open(self, req, timeout):
            requests.append((req.full_url, req.get_header("Authorization"), timeout))
            if status is None:
                raise OSError("connection refused")
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(body))
            return io.BytesIO(body)

    def build_opener(*handlers):
        openers.append(handlers)
        return Opener()

    monkeypatch.setattr(shim.urllib.request, "build_opener", build_opener)
    provider = CredentialProvider(token="fixture-token")
    assert shim._board_available("http://fixture.invalid", provider) is expected
    assert requests == [("http://fixture.invalid/api/hook/coordination-start",
                         "Bearer fixture-token", 2)]
    # A redirect must never carry the bearer to another host.
    assert openers == [(shim._NoRedirectHandler,)]


@pytest.mark.parametrize("ready", [True, False])
def test_board_checkin_is_appended_only_when_ready(ready):
    from pseudolife_memory.coordination import CHECKIN_INSTRUCTION
    from pseudolife_memory.mcp_server import _MCP_INSTRUCTIONS
    composed = shim._with_board_checkin(_MCP_INSTRUCTIONS, ready)
    assert (CHECKIN_INSTRUCTION in composed) is ready
    assert composed.startswith(_MCP_INSTRUCTIONS)
    # Codex needs the first 512 characters to stand alone.
    assert len(composed) <= 512
    if not ready:
        assert composed == _MCP_INSTRUCTIONS
    assert shim._with_board_checkin(None, ready) == (CHECKIN_INSTRUCTION if ready else None)


@pytest.mark.parametrize("board_checkin,expected", [(True, True), (False, False)])
def test_proxy_serves_the_board_checkin_it_was_told_to(monkeypatch, board_checkin, expected):
    from mcp.client import session, streamable_http
    from mcp.server import stdio
    from pseudolife_memory.coordination import CHECKIN_INSTRUCTION
    import sys

    seen = {}

    @asynccontextmanager
    async def http_client(**kwargs):
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

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setitem(sys.modules, "pseudolife_memory.channel",
                        SimpleNamespace(serve_channel=serve_channel))
    asyncio.run(shim._proxy("http://fixture.invalid", "fixture-token", "fixture-session",
                            channel_inbox=inbox, board_checkin=board_checkin))
    assert seen["instructions"].startswith("Fixture daemon instructions")
    assert (CHECKIN_INSTRUCTION in seen["instructions"]) is expected
