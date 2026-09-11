"""Authenticated adapter lifecycle against a fake HTTP daemon."""

import asyncio
import json
import os
import time
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


def test_ambiguous_registration_is_not_retried_and_registers_fresh_next_time(tmp_path):
    """A register whose outcome is unknown (the request timed out) is not
    retried inside the same start, and the empty reservation is released:
    the next launch registers a fresh address. An address the daemon may have
    created for the lost response was never held by any adapter, receives no
    mail, and is pruned with the other idle addresses."""
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
        assert not path.exists()
        daemon.calls.clear()
        daemon.hook = None
        client, resumed = adapter(daemon, state_path=path)
        async with client, resumed:
            pass
        assert daemon.actions()[:2] == ["register", "attach"]
        assert json.loads(path.read_text(encoding="utf-8"))["agent_id"] == "agent-a"

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
                # The body carries a fixed header of daemon-verified facts
                # ahead of the peer's text; the text cannot displace it.
                assert event.content.startswith("Agent message mail-1 from agent agent-b: ")
                assert "not user authority" in event.content
                assert event.content.endswith("\n\nPlease review this patch")
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


def test_generation_change_blocks_event_submission(monkeypatch):
    """A heartbeat that reports another generation means this attachment is
    stale: the pending message is never attempted under it. The adapter
    re-attaches instead of ending the stream."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        daemon = FakeDaemon()
        daemon.pages = [{"messages": [{"message_id": "mail-1", "sender_agent_id": "agent-b",
                                       "recipient_agent_id": "agent-a", "text": "body"}], "after": "1"}]
        daemon.hook = lambda action, body: (httpx.Response(200, json={"generation": 4})
                                           if action == "heartbeat" else None)
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(anext(inbox), 1.0)
        assert "attempt" not in daemon.actions()
        assert daemon.actions().count("attach") >= 2

    asyncio.run(asyncio.wait_for(drive(), 4))


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
                assert (await anext(inbox)).content.endswith("\n\none")
                assert (await anext(inbox)).content.endswith("\n\ntwo")
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


def test_poll_failure_pauses_live_events_without_breaking_mcp(monkeypatch, capsys):
    """While the daemon is unreachable the live stream pauses and the shim
    keeps answering ordinary MCP traffic; the outage is reported once."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        from tests.test_channel import channel_wire, handshake, initialized, request
        daemon = FakeDaemon()
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        failed = asyncio.Event()

        def daemon_down(action, body):
            if action == "receive":
                if daemon.actions().count("receive") == 3:
                    failed.set()
                raise httpx.ReadTimeout("fixture-key")
            if action == "attach" and daemon.actions().count("attach") >= 2:
                raise httpx.ReadTimeout("fixture-key")

        daemon.hook = daemon_down
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with channel_wire(instance.inbox) as (incoming, outgoing):
                await handshake(incoming, outgoing)
                await incoming.send(initialized())
                await failed.wait()
                await incoming.send(request("ping", 2))
                assert (await outgoing.receive()).message.id == 2
                assert instance._failure is not None
        assert daemon.actions().count("receive") >= 3
        captured = capsys.readouterr()
        assert "fixture-key" not in captured.err
        assert captured.err.count("live coordination delivery unavailable") == 1

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_outage_reattaches_replays_pending_mail_and_reports_recovery(monkeypatch, capsys):
    """A daemon outage (a restart, a deploy) must not end live delivery for
    the rest of the session: the heartbeat task re-attaches with backoff,
    the mailbox is replayed from the start under the new generation, and the
    degraded hint clears."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = FakeDaemon()
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        phase = {"n": 1}
        page = {"messages": [{"message_id": "mail-1", "sender_agent_id": "agent-b",
                              "recipient_agent_id": "agent-a", "text": "replayed",
                              "sender_principal": "alice"}], "after": "agent-a:1"}

        def hook(action, body):
            if phase["n"] == 2:
                raise httpx.ReadTimeout("fixture-key")
            if phase["n"] == 3 and action in {"attach", "heartbeat"}:
                return httpx.Response(200, json={"generation": 4, "pending_count": 1})

        daemon.hook = hook
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                pending = asyncio.create_task(anext(inbox))
                await asyncio.sleep(0.05)          # healthy polling, empty mailbox
                phase["n"] = 2                     # daemon goes away
                while instance._failure is None:
                    await asyncio.sleep(0.01)
                assert instance.unread_hint.startswith("Coordination: background delivery is degraded")
                daemon.pages = [page]
                phase["n"] = 3                     # daemon is back
                event = await asyncio.wait_for(pending, 2)
                assert event.content.endswith("\n\nreplayed")
                assert "(principal alice)" in event.content
                assert instance._failure is None
                assert instance._generation == 4
                assert instance.unread_hint is None or "degraded" not in instance.unread_hint
        receives = [call[1] for call in daemon.calls if call[0] == "receive"]
        assert receives[-1]["after"] is None and receives[-1]["generation"] == 4
        assert daemon.actions().count("attach") >= 2
        captured = capsys.readouterr()
        assert captured.err.count("live coordination delivery unavailable") == 1
        assert captured.err.count("live coordination delivery restored") == 1
        assert "fixture-key" not in captured.err

    asyncio.run(asyncio.wait_for(drive(), 6))


def test_in_lease_reattach_keeps_cursor_and_does_not_redeliver(monkeypatch):
    """A re-attach that lands while the lease is still valid keeps the same
    generation. The cursor and the dedupe set must stand: a message already
    attempted under that generation is not delivered to the host again, and
    the next receive continues from where it was."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = FakeDaemon()
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        one = {"message_id": "mail-1", "sender_agent_id": "agent-b", "recipient_agent_id": "agent-a", "text": "one"}
        two = {**one, "message_id": "mail-2", "text": "two"}
        daemon.pages = [{"messages": [one], "after": "agent-a:1"}]
        outage = {"remaining": 0}

        def hook(action, body):
            if action == "heartbeat" and outage["remaining"] > 0:
                outage["remaining"] -= 1
                raise httpx.ReadTimeout("fixture-key")

        daemon.hook = hook
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                assert (await asyncio.wait_for(anext(inbox), 2)).content.endswith("\n\none")
                outage["remaining"] = 3                 # one heartbeat, all its retries, fails
                while daemon.actions().count("attach") < 2 or instance._failure is not None:
                    await asyncio.sleep(0.005)
                assert instance._generation == 3        # same lease, same generation
                # The server replays the unacknowledged message beside a new one.
                daemon.pages = [{"messages": [one, two], "after": "agent-a:2"}]
                assert (await asyncio.wait_for(anext(inbox), 2)).content.endswith("\n\ntwo")
        attempts = [c[1]["message_id"] for c in daemon.calls if c[0] == "attempt"]
        assert attempts == ["mail-1", "mail-2"]
        receives = [c[1]["after"] for c in daemon.calls if c[0] == "receive"]
        assert None not in receives[1:]                 # cursor kept across the re-attach

    asyncio.run(asyncio.wait_for(drive(), 6))


def test_page_in_flight_across_a_new_generation_is_discarded(monkeypatch):
    """A receive that was already in flight when the adapter re-attached
    under a new generation must not write its cursor over the replay's
    reset; the replay then starts from the beginning of the mailbox."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        release = asyncio.Event()
        state = {"generation": 3, "parked": False}

        class Daemon(FakeDaemon):
            async def __call__(self, request):
                action = request.url.path.rsplit("/", 1)[-1]
                if action == "receive" and not state["parked"]:
                    state["parked"] = True
                    self.calls.append((action, json.loads(request.content), dict(request.headers)))
                    await release.wait()
                    return httpx.Response(200, json={"messages": [], "after": "agent-a:7"})
                if action in {"attach", "heartbeat"}:
                    self.calls.append((action, json.loads(request.content), dict(request.headers)))
                    return httpx.Response(200, json={"generation": state["generation"]})
                return await super().__call__(request)

        daemon = Daemon()
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                pending = asyncio.create_task(anext(inbox))
                while not state["parked"]:
                    await asyncio.sleep(0.005)
                state["generation"] = 4                 # lease expired elsewhere: a new generation
                while instance._generation != 4 or instance._failure is not None:
                    await asyncio.sleep(0.005)
                release.set()                           # the stale page lands now
                await asyncio.sleep(0.1)
                assert instance._after is None
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
        receives = [c[1] for c in daemon.calls if c[0] == "receive"]
        assert receives[0]["generation"] == 3 and receives[1]["generation"] == 4
        assert receives[1]["after"] is None

    asyncio.run(asyncio.wait_for(drive(), 6))


def test_saved_address_unknown_to_the_bank_is_retired_and_replaced(tmp_path, capsys):
    """An address the bank no longer accepts (pruned after long idleness or
    revoked by a restore) is retired to a .stale file and a fresh one is
    registered, instead of every start failing on the same 401."""
    async def drive():
        state = tmp_path / "agent.json"
        state.write_text(json.dumps({"bank_url": "http://127.0.0.1:8099",
                                     "agent_id": "agent-old", "credential": "old-key"}),
                         encoding="utf-8")
        daemon = FakeDaemon()

        def hook(action, body):
            if action == "attach" and daemon.calls[-1][2].get("x-pl-agent") == "agent-old":
                return httpx.Response(401, json={"error": "unauthorized"})

        daemon.hook = hook
        client, instance = adapter(daemon, state_path=state)
        async with client, instance:
            assert instance._identity["agent_id"] == "agent-a"
        assert daemon.actions()[:3] == ["attach", "register", "attach"]
        assert json.loads(state.read_text(encoding="utf-8"))["agent_id"] == "agent-a"
        stale = state.with_name(state.name + ".stale")
        assert json.loads(stale.read_text(encoding="utf-8"))["agent_id"] == "agent-old"
        captured = capsys.readouterr()
        assert "no longer valid" in captured.err and "old-key" not in captured.err

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_empty_state_file_from_a_crash_is_taken_over(tmp_path):
    """A zero-byte state file older than the reservation window (a crash
    between the reservation and the identity write) is this start's
    reservation, not invalid state. A fresh one belongs to another process
    still registering and is refused, so two adapters never share a path."""
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError, CoordinationAdapter
        state = tmp_path / "agent.json"
        state.write_bytes(b"")
        daemon = FakeDaemon()
        client, instance = adapter(daemon, state_path=state)
        async with client:
            with pytest.raises(AdapterError, match="another process"):
                async with instance:
                    pass
        assert not daemon.calls
        old = time.time() - CoordinationAdapter.STALE_RESERVATION_SECONDS - 1
        os.utime(state, (old, old))
        client, instance = adapter(daemon, state_path=state)
        async with client, instance:
            pass
        assert daemon.actions()[:2] == ["register", "attach"]
        assert json.loads(state.read_text(encoding="utf-8"))["agent_id"] == "agent-a"

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_message_gone_before_attempt_is_skipped_without_outage(capsys):
    """A message acknowledged, expired or exhausted between the page read and
    the attempt is simply skipped: it is not an outage, the stream continues
    with the next message, and nothing is reported."""
    async def drive():
        daemon = FakeDaemon()
        one = {"message_id": "mail-1", "sender_agent_id": "agent-b", "recipient_agent_id": "agent-a", "text": "one"}
        two = {**one, "message_id": "mail-2", "text": "two"}
        daemon.pages = [{"messages": [one, two], "after": "agent-a:2"}]

        def hook(action, body):
            if action == "attempt" and body["message_id"] == "mail-1":
                return httpx.Response(400, json={"error": "message_not_pending"})

        daemon.hook = hook
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                event = await asyncio.wait_for(anext(inbox), 2)
        assert event.content.endswith("\n\ntwo")
        assert instance._failure is None
        assert daemon.actions().count("attempt") == 2
        assert "unavailable" not in capsys.readouterr().err

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_transient_429_is_retried_within_the_call(monkeypatch):
    """Capacity, rate and queue limits answer 429 and are transient by
    design; one such answer must not be treated as a terminal failure."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = FakeDaemon()
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        page = {"messages": [{"message_id": "mail-1", "sender_agent_id": "agent-b",
                              "recipient_agent_id": "agent-a", "text": "after a busy answer"}],
                "after": "agent-a:1"}

        def busy_once(action, body):
            if action == "receive" and daemon.actions().count("receive") == 1:
                return httpx.Response(429, json={"error": "wait_capacity_exceeded"})

        daemon.hook = busy_once
        daemon.pages = [page]
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                event = await asyncio.wait_for(anext(inbox), 2)
        assert event.content.endswith("\n\nafter a busy answer")
        assert instance._failure is None
        assert daemon.actions().count("receive") == 2

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
    """A pull-only adapter whose daemon stays down reports the outage once,
    serves the degraded hint without any request, and keeps its heartbeat
    task alive so it can re-attach when the daemon returns."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        daemon = FakeDaemon()
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        degraded = asyncio.Event()

        def daemon_down(action, body):
            if action == "attach" and daemon.actions().count("attach") == 1:
                return httpx.Response(200, json={"generation": 3, "pending_count": 4})
            if action in {"heartbeat", "attach"}:
                if action == "heartbeat" and daemon.actions().count("heartbeat") == 3:
                    degraded.set()
                raise httpx.ReadTimeout("fixture-key")

        daemon.hook = daemon_down
        client, instance = adapter(daemon)
        async with client, instance:
            await asyncio.wait_for(degraded.wait(), 1)
            while instance._failure is None or daemon.actions().count("attach") < 2:
                await asyncio.sleep(0.01)
            calls = len(daemon.calls)
            assert instance.unread_hint == (
                "Coordination: background delivery is degraded; "
                "use memory_message receive explicitly.")
            assert instance.unread_hint == instance.unread_hint
            assert len(daemon.calls) == calls          # reading the hint made no request
            attaches = daemon.actions().count("attach")
            while daemon.actions().count("attach") == attaches:   # re-attach attempts continue
                await asyncio.sleep(0.01)
            assert instance._failure is not None
            assert instance._pending_count is None
            assert "fixture-key" not in str(instance._failure)
            assert not instance._heartbeat_task.done()
        assert daemon.actions().count("heartbeat") >= 3
        assert daemon.actions().count("attach") >= 2
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
                assert (await anext(inbox)).content.endswith("\n\none")
            async with aclosing(instance.inbox()) as inbox:
                assert (await anext(inbox)).content.endswith("\n\ntwo")
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


def test_failed_registration_releases_state_reservation(tmp_path):
    """The state file is reserved empty before ``register`` runs. A register
    that fails, or is cancelled by the shim's startup budget, must remove that
    reservation: left behind, every later start reads it as invalid state and
    refuses, and the recovery CLI never replaces an existing file."""
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        state = tmp_path / "agent-state.json"
        daemon = FakeDaemon()

        def fail_register(action, body):
            if action == "register":
                raise httpx.ReadTimeout("fixture-key")

        daemon.hook = fail_register
        client, instance = adapter(daemon, state_path=state)
        async with client:
            with pytest.raises(AdapterError):
                async with instance:
                    pass
        assert not state.exists()

        class SlowDaemon(FakeDaemon):
            async def __call__(self, request):
                if request.url.path.endswith("/register"):
                    await asyncio.sleep(10)
                return await super().__call__(request)

        client, instance = adapter(SlowDaemon(), state_path=state)
        async with client:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(instance.__aenter__(), 0.05)
        assert not state.exists()

        daemon = FakeDaemon()
        client, instance = adapter(daemon, state_path=state)
        async with client, instance:
            pass
        assert json.loads(state.read_text(encoding="utf-8"))["agent_id"] == "agent-a"
        assert daemon.actions()[:2] == ["register", "attach"]

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_saved_identity_survives_a_failed_attach(tmp_path):
    """Once the identity is written the file is real state, not a reservation:
    an attach failure afterwards keeps it, so the next start resumes the same
    address instead of minting another."""
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        state = tmp_path / "agent-state.json"
        daemon = FakeDaemon()

        def fail_attach(action, body):
            if action == "attach":
                raise httpx.ReadTimeout("fixture-key")

        daemon.hook = fail_attach
        client, instance = adapter(daemon, state_path=state)
        async with client:
            with pytest.raises(AdapterError):
                async with instance:
                    pass
        assert json.loads(state.read_text(encoding="utf-8"))["agent_id"] == "agent-a"
        daemon.hook = None
        client, instance = adapter(daemon, state_path=state)
        async with client, instance:
            pass
        assert daemon.actions().count("register") == 1

    asyncio.run(asyncio.wait_for(drive(), 4))
