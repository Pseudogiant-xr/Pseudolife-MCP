"""Existing Codex authority checks recover without replaying message operations."""
import asyncio
from contextlib import asynccontextmanager, nullcontext
import gc
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from tests.test_coordination_credentials import Bank, Provider, THREAD, URL


class ContextBank(Bank):
    def __init__(self):
        super().__init__()
        self.context_hook = None
        self.context_checks = 0
        self.context_bodies = []

    async def __call__(self, request):
        if request.url.path.endswith("/context"):
            self.context_checks += 1
            self.context_bodies.append(json.loads(request.content))
            if self.context_hook is not None:
                response = await self.context_hook(request)
                if response is not None:
                    return response
        return await super().__call__(request)


def registry_for(tmp_path, bank, provider, client):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry
    from pseudolife_memory.coordination_adapter import CoordinationAdapter

    def factory(*args, **kwargs):
        return CoordinationAdapter(*args, client=client, **kwargs)

    return CodexCoordinationRegistry(URL, provider.token, provider=provider,
        state_dir=tmp_path / "agents", digest_dir=tmp_path / "digests",
        adapter_factory=factory)


def proxy_calls(monkeypatch, registry, provider, serve, dispatch, *, dispatch_error=None):
    from mcp import types
    from mcp.client import session, streamable_http
    from mcp.server import Server, stdio
    from pseudolife_memory import shim

    @asynccontextmanager
    async def http_client(**kwargs):
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
            dispatch.append((name, arguments))
            if dispatch_error is not None:
                raise dispatch_error
            return types.CallToolResult(content=[], is_error=False)

    monkeypatch.setattr(streamable_http, "create_mcp_http_client", http_client)
    monkeypatch.setattr(streamable_http, "streamable_http_client", transport)
    monkeypatch.setattr(session, "ClientSession", Remote)
    monkeypatch.setattr(stdio, "stdio_server", transport)
    monkeypatch.setattr(Server, "run", serve)
    return shim._proxy(URL, None, "fixture-session", provider=provider,
                       codex_metadata=True, coordination_registry=registry)


@pytest.mark.parametrize("action", ["send", "ack"])
@pytest.mark.parametrize("failure", ["transport", "http_timeout", "deadline", "503", "slow"])
def test_ordinary_message_recovers_existing_authority_check(tmp_path, monkeypatch, action, failure):
    from mcp import types

    async def run():
        provider, bank, dispatch = Provider(), ContextBank(), []
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            original = await registry.get(THREAD)
            assert original is not None
            saved = original.state_path.read_bytes()
            attachment = original._attachment()
            checks = bank.context_checks
            attempts = 0
            if failure == "deadline":
                registry._validation_seconds = 0.03

            async def fail_once(request):
                nonlocal attempts
                attempts += 1
                if attempts != 1:
                    return None
                if failure == "transport":
                    raise httpx.ConnectError("private fixture transport detail")
                if failure == "http_timeout":
                    raise httpx.ReadTimeout("private fixture timeout detail")
                if failure == "503":
                    return httpx.Response(503, json={"error": "private fixture response"})
                if failure == "deadline":
                    await asyncio.Event().wait()
                # The production startup deadline is 3s, below context's 5s.
                # A valid context returned after 3.05s must not be abandoned.
                await asyncio.sleep(3.05)
                return None

            bank.context_hook = fail_once
            arguments = ({"action": "send", "to": "22222222222222222222222222222222",
                          "text": "fixture", "request_id": "fixture-request"}
                         if action == "send" else
                         {"action": "ack", "message_id": "33333333333333333333333333333333"})

            async def serve(server, *args, **kwargs):
                handler = server.get_request_handler("tools/call")
                result = await handler.handler(None, types.CallToolRequestParams(
                    name="memory_message", arguments=arguments, _meta={"threadId": THREAD}))
                assert result.is_error is False

            try:
                await proxy_calls(monkeypatch, registry, provider, serve, dispatch)
                assert dispatch == [("memory_message", arguments)]
                assert bank.context_checks - checks == (1 if failure == "slow" else 2)
                assert all(body == {"read_only": True}
                           for body in bank.context_bodies[checks:])
                assert registry._adapters[THREAD] is original
                assert original.state_path.read_bytes() == saved
                assert original._attachment() == attachment
                assert bank.registered == 1
            finally:
                bank.context_hook = None
                await registry.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("unavailable", ["missing_identity", "cold_storage"])
def test_existing_validation_never_initializes_missing_bank_or_cold_storage(
        tmp_path, monkeypatch, unavailable):
    from mcp import types
    from pseudolife_memory.coordination import dispatch as context_dispatch
    from pseudolife_memory.storage.coordination import CoordinationError, CoordinationStore

    async def run():
        provider, bank, dispatch = Provider(), ContextBank(), []
        writes, initializations = [], []

        def execute(sql, parameters):
            writes.append(sql)
            return SimpleNamespace(rowcount=0)

        storage = SimpleNamespace(conn=SimpleNamespace(execute=execute), _txn=nullcontext)
        store = CoordinationStore(storage)
        monkeypatch.setattr(store, "_one", lambda *args: None)

        def initialize():
            initializations.append(True)
            raise ValueError("coordination_requires_postgres")

        cold = SimpleNamespace(config=SimpleNamespace(coordination=SimpleNamespace(
            enabled=True, allowed_principals=[bank.principal])), _storage=None,
            _lock=threading.Lock(), _ensure_init=initialize)

        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            original = await registry.get(THREAD)
            saved = original.state_path.read_bytes()
            checks = bank.context_checks

            async def unavailable_context(request):
                body = json.loads(request.content)
                try:
                    if unavailable == "missing_identity":
                        store.context(bank.principal, **body)
                    else:
                        context_dispatch(cold, "context", body,
                                         principal=bank.principal, headers={})
                except (CoordinationError, ValueError):
                    return httpx.Response(503, json={"error": "coordination_unavailable"})
                pytest.fail("unavailable context was initialized")

            bank.context_hook = unavailable_context

            async def serve(server, *args, **kwargs):
                handler = server.get_request_handler("tools/call")
                with pytest.raises(Exception) as caught:
                    await handler.handler(None, types.CallToolRequestParams(
                        name="memory_message", arguments={"action": "ack",
                            "message_id": "33333333333333333333333333333333"},
                        _meta={"threadId": THREAD}))
                assert caught.value.data["operation_outcome"] == "not_dispatched"

            try:
                await proxy_calls(monkeypatch, registry, provider, serve, dispatch)
                assert not dispatch and not writes and not initializations
                assert bank.context_bodies[checks:] == [{"read_only": True}] * 2
                assert original.state_path.read_bytes() == saved
                assert registry._adapters[THREAD] is original
                assert bank.registered == 1
            finally:
                bank.context_hook = None
                await registry.aclose()
    asyncio.run(run())


def test_startup_establishes_authority_before_read_only_validation(tmp_path):
    async def run():
        provider, bank = Provider(), ContextBank()
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            try:
                assert await registry.get(THREAD) is not None
                assert bank.context_bodies[0] == {}
                assert bank.registered == 1
                assert bank.context_bodies[1:]
                assert all(body == {"read_only": True} for body in bank.context_bodies[1:])
            finally:
                await registry.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("failure,expected_checks,hint", [
    ("transport", 2, "transport_unavailable"),
    ("timeout", 2, "timeout"),
    ("503", 2, "context_unavailable"),
    ("401", 1, "authentication"),
    ("403", 1, "authentication"),
    ("mismatch", 1, "bank or principal"),
    ("malformed", 1, "bank or principal"),
    ("credential_changed", 1, "credential"),
    ("credential_changed_retry", 2, "credential"),
    ("credential_changed_failed_retry", 2, "credential"),
    ("credential_unavailable", 1, "credential"),
    ("unexpected", 1, "unavailable"),
])
def test_unresolved_authority_never_dispatches_or_replaces_mailbox(
        tmp_path, monkeypatch, failure, expected_checks, hint):
    from mcp import types

    async def run():
        provider, bank, dispatch = Provider(), ContextBank(), []
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            original = await registry.get(THREAD)
            assert original is not None
            saved = original.state_path.read_bytes()
            checks = bank.context_checks
            registry._validation_seconds = 0.03

            async def refuse(request):
                if failure in {"credential_changed_retry", "credential_changed_failed_retry"}:
                    if bank.context_checks - checks == 1:
                        raise httpx.ConnectError("fixture")
                    await asyncio.sleep(0)
                    provider.rotate(provider.token)
                    if failure == "credential_changed_failed_retry":
                        raise httpx.ConnectError("fixture")
                    return None
                if failure == "credential_unavailable":
                    provider.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("private fixture"))
                    raise httpx.ConnectError("fixture")
                if failure in {"transport", "credential_changed"}:
                    if failure == "credential_changed":
                        provider.rotate("fixture-changed")
                    raise httpx.ConnectError("private fixture transport detail")
                if failure == "timeout":
                    await asyncio.Event().wait()
                if failure == "mismatch":
                    bank.principal = "different-fixture-principal"
                    return None
                if failure == "malformed":
                    return httpx.Response(200, json={"private": "fixture"})
                if failure == "unexpected":
                    raise RuntimeError("private fixture unexpected detail")
                return httpx.Response(int(failure), json={"error": "private fixture response"})

            bank.context_hook = refuse

            async def serve(server, *args, **kwargs):
                handler = server.get_request_handler("tools/call")
                with pytest.raises(Exception) as caught:
                    await handler.handler(None, types.CallToolRequestParams(
                        name="memory_message", arguments={"action": "ack", "message_id": "fixture"},
                        _meta={"threadId": THREAD}))
                data = caught.value.data
                assert data["classification"] == "coordination_unavailable"
                assert data["operation_outcome"] == "not_dispatched"
                assert hint in data["hint"]
                assert "private fixture" not in repr(data)
                assert "prior attachment lease" not in data["hint"]

            try:
                await proxy_calls(monkeypatch, registry, provider, serve, dispatch)
                assert not dispatch
                assert bank.context_checks - checks == expected_checks
                assert registry._adapters[THREAD] is original
                assert original.state_path.read_bytes() == saved
                assert bank.registered == 1
            finally:
                bank.context_hook = None
                bank.principal = "fixture-user"
                bank.token = provider.token
                if failure == "credential_unavailable":
                    del provider.snapshot
                await registry.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["send", "ack"])
@pytest.mark.parametrize("outcome", ["transport", "success"])
@pytest.mark.parametrize("cancel_attempt", [1, 2])
def test_cancel_racing_validation_completion_never_dispatches(
        tmp_path, monkeypatch, action, outcome, cancel_attempt):
    from mcp import types

    async def run():
        provider, bank, dispatch = Provider(), ContextBank(), []
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            original = await registry.get(THREAD)
            saved = original.state_path.read_bytes()
            attachment = original._attachment()
            checks = bank.context_checks
            cancel_requested = []
            pending = None

            async def race_completion(request):
                attempt = bank.context_checks - checks
                if attempt < cancel_attempt:
                    raise httpx.ConnectError("fixture first read")
                if attempt == cancel_attempt:
                    # Finish the child in the same loop turn as the caller's
                    # cancellation, exposing wait_for's completed-child race.
                    asyncio.get_running_loop().call_soon(
                        lambda: cancel_requested.append(pending.cancel()))
                    if outcome == "transport":
                        raise httpx.ConnectError("fixture raced read")
                return None

            bank.context_hook = race_completion
            arguments = ({"action": "send", "to": "2" * 32, "text": "fixture",
                          "request_id": "fixture-request"} if action == "send" else
                         {"action": "ack", "message_id": "3" * 32})

            async def serve(server, *args, **kwargs):
                nonlocal pending
                handler = server.get_request_handler("tools/call")

                async def message():
                    return await handler.handler(None, types.CallToolRequestParams(
                        name="memory_message", arguments=arguments,
                        _meta={"threadId": THREAD}))

                pending = asyncio.create_task(message())
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert cancel_requested == [True]
                assert pending.cancelled()
                assert not dispatch
                assert bank.context_checks - checks == cancel_attempt
                assert bank.context_bodies[checks:] == [{"read_only": True}] * cancel_attempt
                assert registry._adapters[THREAD] is original
                assert original.state_path.read_bytes() == saved
                assert original._attachment() == attachment
                bank.context_hook = None
                result = await message()
                assert result.is_error is False
                assert dispatch == [("memory_message", arguments)]
                assert bank.context_checks - checks == cancel_attempt + 1
                assert bank.registered == 1

            try:
                await proxy_calls(monkeypatch, registry, provider, serve, dispatch)
            finally:
                bank.context_hook = None
                await registry.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("cleanup", ["timeout", "close"])
@pytest.mark.parametrize("child_outcome", ["success", "exception"])
def test_cancellation_during_validation_cleanup_propagates(
        tmp_path, monkeypatch, cleanup, child_outcome):
    from mcp import types

    async def run():
        provider, bank, dispatch = Provider(), ContextBank(), []
        entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))
        loop = asyncio.get_running_loop()
        errors, previous_handler = [], loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
                registry = registry_for(tmp_path, bank, provider, client)
                original = await registry.get(THREAD)
                saved = original.state_path.read_bytes()
                checks = bank.context_checks
                if cleanup == "timeout":
                    registry._validation_seconds = 0.01

                async def blocked(request):
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cleaning.set()
                        await release.wait()
                        if child_outcome == "exception":
                            raise RuntimeError("fixture cleanup exception")
                        return None
                    finally:
                        finished.set()

                bank.context_hook = blocked

                async def serve(server, *args, **kwargs):
                    handler = server.get_request_handler("tools/call")
                    pending = asyncio.create_task(handler.handler(None,
                        types.CallToolRequestParams(name="memory_message",
                            arguments={"action": "ack", "message_id": "3" * 32},
                            _meta={"threadId": THREAD})))
                    await entered.wait()
                    closing = (asyncio.create_task(registry.aclose())
                               if cleanup == "close" else None)
                    await cleaning.wait()
                    bank.context_hook = None
                    assert pending.cancel()
                    try:
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                        assert pending.cancelled() and not dispatch
                        assert bank.context_checks - checks == 1
                        assert original.state_path.read_bytes() == saved
                    finally:
                        release.set()
                        await finished.wait()
                        if closing is not None:
                            await closing

                try:
                    await proxy_calls(monkeypatch, registry, provider, serve, dispatch)
                finally:
                    release.set()
                    bank.context_hook = None
                    await registry.aclose()
            # A child can finish after its caller's cleanup was cancelled.
            # Its exception must be retrieved without hiding that cancellation.
            await asyncio.sleep(0)
            gc.collect()
            assert not errors
        finally:
            loop.set_exception_handler(previous_handler)
    asyncio.run(run())


def test_cancelled_validation_does_not_retry_and_next_call_recovers(tmp_path):
    async def run():
        provider, bank = Provider(), ContextBank()
        entered = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            original = await registry.get(THREAD)
            saved = original.state_path.read_bytes()
            checks = bank.context_checks

            async def blocked(request):
                entered.set()
                await asyncio.Event().wait()

            bank.context_hook = blocked
            pending = asyncio.create_task(registry.get(THREAD))
            await entered.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert bank.context_checks - checks == 1
            bank.context_hook = None
            assert await registry.get(THREAD) is original
            assert original.state_path.read_bytes() == saved
            await registry.aclose()
    asyncio.run(run())


def test_concurrent_validation_recovery_keeps_calls_independent(tmp_path):
    async def run():
        provider, bank = Provider(), ContextBank()
        entered, release = asyncio.Event(), asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            original = await registry.get(THREAD)
            saved = original.state_path.read_bytes()
            checks = bank.context_checks
            calls = 0

            async def blocked_then_transient(request):
                nonlocal calls
                calls += 1
                if calls == 1:
                    entered.set()
                    await release.wait()
                    raise httpx.ConnectError("fixture")
                return None

            bank.context_hook = blocked_then_transient
            first = asyncio.create_task(registry.get(THREAD))
            await entered.wait()
            assert await registry.get(THREAD) is original
            release.set()
            assert await first is original
            assert bank.context_checks - checks == 3
            assert original.state_path.read_bytes() == saved
            assert bank.registered == 1
            bank.context_hook = None
            await registry.aclose()
    asyncio.run(run())


def test_close_cancels_inflight_recovery_without_another_validation(tmp_path):
    async def run():
        provider, bank = Provider(), ContextBank()
        retry_entered = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            original = await registry.get(THREAD)
            saved = original.state_path.read_bytes()
            checks = bank.context_checks

            async def fail_then_block(request):
                if bank.context_checks - checks == 1:
                    raise httpx.ConnectError("fixture")
                retry_entered.set()
                await asyncio.Event().wait()

            bank.context_hook = fail_then_block
            pending = asyncio.create_task(registry.get(THREAD))
            await retry_entered.wait()
            # Closing also validates the read-only detach authority. Permit
            # that separate check while the already-running retry stays blocked.
            bank.context_hook = None
            await registry.aclose()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not registry._adapters
            assert original.state_path.read_bytes() == saved
            assert bank.context_checks - checks == 3  # two validation attempts + detach
    asyncio.run(run())


def test_recovered_authority_never_replays_an_ambiguous_message(tmp_path, monkeypatch):
    from mcp import types

    async def run():
        provider, bank, dispatch = Provider(), ContextBank(), []
        async with httpx.AsyncClient(transport=httpx.MockTransport(bank)) as client:
            registry = registry_for(tmp_path, bank, provider, client)
            original = await registry.get(THREAD)
            checks = bank.context_checks

            async def transient(request):
                if bank.context_checks - checks == 1:
                    return httpx.Response(503, json={})
                return None

            bank.context_hook = transient

            async def serve(server, *args, **kwargs):
                handler = server.get_request_handler("tools/call")
                with pytest.raises(Exception) as caught:
                    await handler.handler(None, types.CallToolRequestParams(
                        name="memory_message", arguments={"action": "send",
                            "to": "22222222222222222222222222222222", "text": "fixture",
                            "request_id": "fixture-request"},
                        _meta={"threadId": THREAD}))
                assert caught.value.data["operation_outcome"] == "unknown"

            try:
                await proxy_calls(monkeypatch, registry, provider, serve, dispatch,
                                  dispatch_error=httpx.ReadTimeout("private fixture"))
                assert len(dispatch) == 1
                assert bank.context_checks - checks == 2
                assert registry._adapters[THREAD] is original
            finally:
                bank.context_hook = None
                await registry.aclose()
    asyncio.run(run())


def test_startup_attachment_busy_keeps_its_cooldown_and_specific_hint(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry
    from pseudolife_memory.coordination_adapter import AdapterError

    attempts = []

    class Adapter:
        def __init__(self, *args, **kwargs): pass

        async def __aenter__(self):
            attempts.append(True)
            raise AdapterError("private fixture", code="attachment_busy", status=409)

        async def __aexit__(self, *args): pass

    async def run():
        provider = Provider()
        registry = CodexCoordinationRegistry(URL, provider.token, provider=provider,
            state_dir=tmp_path, adapter_factory=Adapter)
        assert await registry.get(THREAD) is None
        assert await registry.get(THREAD) is None
        hint = registry.unread_hint(THREAD, None)
        assert "attachment_busy" in hint and "prior attachment lease" in hint
        assert "private fixture" not in hint
        assert len(attempts) == 1  # validation recovery does not retry startup
        await registry.aclose()
    asyncio.run(run())
