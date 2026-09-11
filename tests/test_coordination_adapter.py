"""Authenticated adapter lifecycle against a fake HTTP daemon."""

import asyncio
import json
import os
from contextlib import aclosing, suppress

import httpx
import pytest


class FakeDaemon:
    def __init__(self):
        self.calls = []
        self.pages = []
        self.hook = None

    async def __call__(self, request):
        action = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((action, body, dict(request.headers)))
        if self.hook:
            response = self.hook(action, body)
            if response is not None:
                return response
        result = {"register": {"agent_id": "agent-a", "credential": "fixture-key"},
                  "attach": {"generation": 3, "lease_until": "later"},
                  "heartbeat": {"generation": 3, "lease_until": "later"},
                  "detach": {}, "attempt": {}}.get(action)
        if action == "receive":
            result = self.pages.pop(0) if self.pages else {"messages": [], "after": None}
        return httpx.Response(200, json=result)

    def actions(self):
        return [call[0] for call in self.calls]


def adapter(daemon, **kwargs):
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    client = httpx.AsyncClient(transport=httpx.MockTransport(daemon))
    return client, CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer",
                                       client=client, **kwargs)


def _assert_windows_owner_only(path):
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    get = advapi.GetFileSecurityW
    get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    get(str(path), 4, None, 0, ctypes.byref(needed))
    descriptor = ctypes.create_string_buffer(needed.value)
    assert get(str(path), 4, descriptor, needed.value, ctypes.byref(needed))
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    sddl = wintypes.LPWSTR()
    assert convert(descriptor, 1, 4, ctypes.byref(sddl), None)
    try:
        assert bool(sddl.value == "D:P(A;;FA;;;OW)"), "state DACL must be protected and owner-only"
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(sddl, ctypes.c_void_p))


def test_register_state_resume_and_detach(tmp_path):
    async def drive():
        daemon = FakeDaemon()
        path = tmp_path / "agent.json"
        client, first = adapter(daemon, state_path=path)
        async with client, first:
            assert first.instance_headers == {"X-PL-Agent": "agent-a", "X-PL-Agent-Key": "fixture-key"}
            assert "fixture-key" not in repr(first)
            assert json.loads(path.read_text())["agent_id"] == "agent-a"
        client, resumed = adapter(daemon, state_path=path)
        async with client, resumed:
            assert resumed.instance_headers["X-PL-Agent"] == "agent-a"
        assert daemon.actions() == ["register", "attach", "detach", "attach", "detach"]
        assert daemon.calls[0][1]["wake_enabled"] is False
        assert daemon.calls[1][2]["x-pl-agent-key"] == "fixture-key"
        assert daemon.calls[1][1]["attachment_id"] != daemon.calls[3][1]["attachment_id"]
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0
        else:
            _assert_windows_owner_only(path)

    asyncio.run(drive())


def test_resume_refusal_never_registers_or_overwrites_state(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        path = tmp_path / "agent.json"
        daemon = FakeDaemon()
        client, first = adapter(daemon, state_path=path)
        async with client, first:
            pass
        original = path.read_bytes()
        daemon.calls.clear()
        daemon.hook = lambda action, body: httpx.Response(409, text="fixture-key")
        client, resumed = adapter(daemon, state_path=path)
        async with client:
            with pytest.raises(AdapterError) as error:
                async with resumed:
                    pass
        assert "fixture-key" not in str(error.value)
        assert daemon.actions() == ["attach"]
        assert path.read_bytes() == original

    asyncio.run(drive())


@pytest.mark.parametrize("state", ["not json", '{"bank_url":"http://other.invalid"}'])
def test_invalid_or_foreign_state_does_not_register(tmp_path, state):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        path = tmp_path / "agent.json"
        path.write_text(state)
        daemon = FakeDaemon()
        client, instance = adapter(daemon, state_path=path)
        async with client:
            with pytest.raises(AdapterError):
                async with instance:
                    pass
        assert not daemon.calls
        assert path.read_text() == state

    asyncio.run(drive())


def test_ambiguous_registration_is_not_retried_and_blocks_reregistration(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        path = tmp_path / "agent.json"
        daemon = FakeDaemon()

        def timeout(action, body):
            raise httpx.ReadTimeout("fixture-bearer")

        daemon.hook = timeout
        client, instance = adapter(daemon, state_path=path)
        async with client:
            with pytest.raises(AdapterError) as error:
                async with instance:
                    pass
        assert "fixture-bearer" not in str(error.value)
        assert daemon.actions() == ["register"]
        assert path.exists()
        daemon.calls.clear()
        client, resumed = adapter(daemon, state_path=path)
        async with client:
            with pytest.raises(AdapterError):
                async with resumed:
                    pass
        assert not daemon.calls

    asyncio.run(drive())


def test_explicit_wake_and_emission_recheck_without_acknowledgment():
    async def drive():
        daemon = FakeDaemon()
        message = {"message_id": "mail-1", "sender_agent_id": "agent-b", "recipient_agent_id": "agent-a",
                   "text": "Please review this patch", "source": "user", "meta": {"sender_id": "forged"}}
        daemon.pages = [{"messages": [message], "after": "page-1"}]
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                event = await anext(inbox)
                assert event.meta["sender_id"] == "agent-b"
                assert event.meta["origin"] == "agent"
                assert "source" not in event.meta
                assert event.content == "Please review this patch"
            assert daemon.actions() == ["register", "attach", "receive", "heartbeat", "attempt"]
            assert daemon.calls[0][1]["wake_enabled"] is True
        assert "ack" not in daemon.actions()

    asyncio.run(drive())


def test_pull_only_adapter_does_not_receive_live_events():
    async def drive():
        daemon = FakeDaemon()
        client, instance = adapter(daemon)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                task = asyncio.create_task(anext(inbox))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert daemon.actions() == ["register", "attach", "detach"]

    asyncio.run(drive())


def test_generation_change_blocks_event_submission():
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        daemon = FakeDaemon()
        daemon.pages = [{"messages": [{"message_id": "mail-1", "sender_agent_id": "agent-b",
                                       "recipient_agent_id": "agent-a", "text": "body"}], "after": "1"}]
        daemon.hook = lambda action, body: (httpx.Response(200, json={"generation": 4})
                                           if action == "heartbeat" else None)
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            with pytest.raises(StopAsyncIteration):
                await anext(instance.inbox())
        assert "attempt" not in daemon.actions()

    asyncio.run(drive())


def test_page_cursor_advances_and_duplicate_ids_are_not_reemitted():
    async def drive():
        daemon = FakeDaemon()
        one = {"message_id": "mail-1", "sender_agent_id": "agent-b", "recipient_agent_id": "agent-a", "text": "one"}
        two = {**one, "message_id": "mail-2", "text": "two"}
        daemon.pages = [{"messages": [one], "after": "page-1"},
                        {"messages": [one, two], "after": "page-2"}]
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                assert (await anext(inbox)).content == "one"
                assert (await anext(inbox)).content == "two"
        receives = [call for call in daemon.calls if call[0] == "receive"]
        assert receives[1][1]["after"] == "page-1"
        assert receives[0][1]["attachment_id"] == daemon.calls[1][1]["attachment_id"]
        assert receives[0][1]["generation"] == 3
        assert daemon.actions().count("attempt") == 2
        assert "ack" not in daemon.actions()

    asyncio.run(drive())


def test_reserved_state_prevents_concurrent_registration(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError, CoordinationAdapter
        daemon = FakeDaemon()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed(request):
            if request.url.path.endswith("/register"):
                entered.set()
                await release.wait()
            return await daemon(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(delayed)) as client:
            path = tmp_path / "agent.json"
            first = CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer", client=client, state_path=path)
            pending = asyncio.create_task(first.__aenter__())
            await entered.wait()
            second = CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer", client=client, state_path=path)
            with pytest.raises(AdapterError):
                await second.__aenter__()
            release.set()
            await pending
            await first.__aexit__(None, None, None)
        assert daemon.actions().count("register") == 1

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_state_hardlink_is_refused_before_network(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        original = tmp_path / "original.json"
        original.write_text("{}")
        linked = tmp_path / "agent.json"
        os.link(original, linked)
        daemon = FakeDaemon()
        client, instance = adapter(daemon, state_path=linked)
        async with client:
            with pytest.raises(AdapterError):
                await instance.__aenter__()
        assert not daemon.calls
        assert original.read_text() == "{}"

    asyncio.run(drive())


def test_poll_failure_stops_live_events_without_breaking_mcp(monkeypatch, capsys):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        from tests.test_channel import channel_wire, handshake, initialized, request
        daemon = FakeDaemon()
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        failed = asyncio.Event()

        def fail_receive(action, body):
            if action == "receive":
                if daemon.actions().count("receive") == 3:
                    failed.set()
                raise httpx.ReadTimeout("fixture-key")

        daemon.hook = fail_receive
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with channel_wire(instance.inbox) as (incoming, outgoing):
                await handshake(incoming, outgoing)
                await incoming.send(initialized())
                await failed.wait()
                await incoming.send(request("ping", 2))
                assert (await outgoing.receive()).message.id == 2
        assert daemon.actions().count("receive") == 3
        captured = capsys.readouterr()
        assert "fixture-key" not in captured.err
        assert "delivery unavailable" in captured.err

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_periodic_renewal_and_context_exit_detach(monkeypatch):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = FakeDaemon()
        renewed = asyncio.Event()
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)

        def seen(action, body):
            if action == "heartbeat":
                renewed.set()

        daemon.hook = seen
        client, instance = adapter(daemon)
        async with client, instance:
            await renewed.wait()
        assert daemon.actions()[-1] == "detach"
        assert instance._heartbeat_task.done()
        assert daemon.calls[-1][1]["generation"] == 3

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_pull_adapter_renew_failure_reports_degraded_hint_once(monkeypatch, capsys):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = FakeDaemon()
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))

        def fail_renew(action, body):
            if action == "attach":
                return httpx.Response(200, json={"generation": 3, "pending_count": 4})
            if action == "heartbeat":
                raise httpx.ReadTimeout("fixture-key")

        daemon.hook = fail_renew
        client, instance = adapter(daemon)
        async with client, instance:
            await asyncio.wait_for(instance._heartbeat_task, 1)
            calls = len(daemon.calls)
            assert instance.unread_hint == (
                "Coordination: background delivery is degraded; "
                "use memory_message receive explicitly.")
            assert instance.unread_hint == instance.unread_hint
            assert len(daemon.calls) == calls
            assert instance._pending_count is None
            assert "fixture-key" not in str(instance._failure)
        assert daemon.actions().count("heartbeat") == 3
        assert "receive" not in daemon.actions()
        captured = capsys.readouterr()
        assert captured.err.count("live coordination delivery unavailable") == 1
        assert "fixture-key" not in captured.err

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_exit_detaches_and_closes_after_unexpected_heartbeat_error():
    async def drive():
        daemon = FakeDaemon()
        client, instance = adapter(daemon)
        await instance.__aenter__()
        original_heartbeat = instance._heartbeat_task
        original_heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await original_heartbeat

        async def heartbeat_bug():
            raise RuntimeError("heartbeat cleanup bug")

        instance._heartbeat_task = asyncio.create_task(heartbeat_bug())
        await asyncio.sleep(0)

        instance._owns_client = True
        with pytest.raises(RuntimeError, match="heartbeat cleanup bug"):
            await instance.__aexit__(None, None, None)
        assert daemon.actions()[-1] == "detach"
        assert client.is_closed

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_exit_closes_owned_client_after_unexpected_detach_error():
    async def drive():
        daemon = FakeDaemon()
        client, instance = adapter(daemon)
        await instance.__aenter__()

        def detach_bug(action, body):
            if action == "detach":
                raise RuntimeError("detach cleanup bug")

        daemon.hook = detach_bug
        instance._owns_client = True
        with pytest.raises(RuntimeError, match="detach cleanup bug"):
            await instance.__aexit__(None, None, None)
        assert daemon.actions()[-1] == "detach"
        assert client.is_closed

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_cancelled_poll_runs_async_cleanup_and_detaches():
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = FakeDaemon()
        polling = asyncio.Event()
        cleaned = asyncio.Event()

        async def waiting(request):
            if request.url.path.endswith("/receive"):
                polling.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    cleaned.set()
            return await daemon(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(waiting)) as client:
            instance = CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer", client=client, wake_enabled=True)
            async with instance:
                async with aclosing(instance.inbox()) as inbox:
                    pending = asyncio.create_task(anext(inbox))
                    await polling.wait()
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
            assert cleaned.is_set()
            assert daemon.actions()[-1] == "detach"

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_reopening_inbox_keeps_attachment_deduplication():
    async def drive():
        daemon = FakeDaemon()
        one = {"message_id": "mail-1", "sender_agent_id": "agent-b", "recipient_agent_id": "agent-a", "text": "one"}
        two = {**one, "message_id": "mail-2", "text": "two"}
        page = {"messages": [one, two], "after": "page-2"}
        daemon.pages = [page, page]
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                assert (await anext(inbox)).content == "one"
            async with aclosing(instance.inbox()) as inbox:
                assert (await anext(inbox)).content == "two"
        assert daemon.actions().count("attempt") == 2

    asyncio.run(drive())


def test_channel_eof_allows_http_async_cleanup_before_detach():
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        from tests.test_channel import channel_wire, handshake, initialized
        daemon = FakeDaemon()
        polling = asyncio.Event()
        cleaned = asyncio.Event()

        async def waiting(request):
            if request.url.path.endswith("/receive"):
                polling.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    cleaned.set()
            return await daemon(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(waiting)) as client:
            instance = CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer", client=client, wake_enabled=True)
            async with instance:
                async with channel_wire(instance.inbox) as (incoming, outgoing):
                    await handshake(incoming, outgoing)
                    await incoming.send(initialized())
                    await polling.wait()
                assert cleaned.is_set()
            assert daemon.actions()[-1] == "detach"

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_cached_unread_hint_updates_without_extra_requests_or_ack():
    async def drive():
        daemon = FakeDaemon()

        def counts(action, body):
            if action in {"attach", "heartbeat"}:
                return httpx.Response(200, json={"generation": 3, "pending_count": 2 if action == "attach" else 4,
                                                 "snippet": "must not reach the hint"})

        daemon.hook = counts
        client, instance = adapter(daemon)
        async with client, instance:
            calls = len(daemon.calls)
            assert instance.unread_hint == (
                "Coordination: at last check 2 addressed messages were pending; use memory_message receive.")
            assert instance.unread_hint == instance.unread_hint
            assert len(daemon.calls) == calls
            await instance._heartbeat()
            assert instance.unread_hint == (
                "Coordination: at last check 4 addressed messages were pending; use memory_message receive.")
            assert "must not reach" not in instance.unread_hint
        assert "ack" not in daemon.actions()
        assert "receive" not in daemon.actions()

    asyncio.run(drive())


@pytest.mark.parametrize("count", [0, -1, True, False, 1.5, "2", None])
def test_unread_hint_suppresses_zero_or_invalid_latest_count(count):
    async def drive():
        daemon = FakeDaemon()

        def counts(action, body):
            if action in {"attach", "heartbeat"}:
                return httpx.Response(200, json={"generation": 3, "pending_count": 2 if action == "attach" else count})

        daemon.hook = counts
        client, instance = adapter(daemon)
        async with client, instance:
            assert instance.unread_hint is not None
            await instance._heartbeat()
            assert instance.unread_hint is None

    asyncio.run(drive())
