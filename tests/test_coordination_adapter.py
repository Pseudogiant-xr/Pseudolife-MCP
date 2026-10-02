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


def test_heartbeat_reports_only_a_live_listener_and_expires_a_crashed_waiter(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("pseudolife_memory.coordination_adapter.time.time", lambda: now[0])

    async def drive():
        daemon = FakeDaemon()
        digest = tmp_path / "digest.txt"
        client, instance = adapter(daemon, digest_path=digest)
        async with client, instance:
            await instance._heartbeat()
            assert daemon.calls[-1][1]["ring_armed_until"] == 0
            digest.with_suffix(".wake").write_text("owner\n")
            digest.with_suffix(".wake-armed").write_text(f"owner\n{now[0] + 60}\n")
            await instance._heartbeat()
            assert daemon.calls[-1][1]["ring_armed_until"] == now[0] + 60
            now[0] += 61
            await instance._heartbeat()
            assert daemon.calls[-1][1]["ring_armed_until"] == 0
    asyncio.run(drive())


def test_an_enabled_doorbell_disarms_after_failure(tmp_path):
    from pseudolife_memory.codex_doorbell import CodexDoorbell
    thread = "aaaaaaaa-1111-4111-8111-111111111111"

    async def drive():
        daemon = FakeDaemon()
        client, instance = adapter(daemon, ring_path=True)
        bell = CodexDoorbell(["fixture-cli"])
        async with client, instance:
            bell.watch(thread, instance)
            await instance._heartbeat()
            assert daemon.calls[-1][1]["ring_armed_until"] > time.time()
            bell._disable("fixture failure")
            await instance._heartbeat()
            assert daemon.calls[-1][1]["ring_armed_until"] == 0
            await bell.aclose()
    asyncio.run(drive())


def test_a_waiter_beside_a_disabled_doorbell_still_counts_as_a_listener(tmp_path):
    """Review finding 2026-10-02: once the doorbell watched a Codex thread,
    only its liveness was reported, so a ``wait-mail --session-id <thread>``
    lease beside a doorbell that later turned itself off was ignored, the
    daemon answered no_path and the waiter slept to its timeout. Either
    listener arms the ring: the marker is written for every rung ring."""
    from pseudolife_memory.codex_doorbell import CodexDoorbell
    from pseudolife_memory.wake_liveness import WaitListener
    thread = "aaaaaaaa-1111-4111-8111-111111111111"

    async def drive():
        daemon = FakeDaemon()
        digest = tmp_path / "digest.txt"
        client, instance = adapter(daemon, digest_path=digest, ring_path=True)
        bell = CodexDoorbell(["fixture-cli"])
        waiter = WaitListener(digest, 100)
        async with client, instance:
            bell.watch(thread, instance)
            bell._disable("fixture failure")
            await instance._heartbeat()
            assert daemon.calls[-1][1]["ring_armed_until"] == 0
            waiter.renew()
            await instance._heartbeat()
            assert daemon.calls[-1][1]["ring_armed_until"] > time.time()
            waiter.close()
            await bell.aclose()
    asyncio.run(drive())


def adapter(daemon, **kwargs):
    from pseudolife_memory.coordination_adapter import CoordinationAdapter
    client = httpx.AsyncClient(transport=httpx.MockTransport(daemon))
    return client, CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer",
                                       client=client, **kwargs)


def _secure_state(path):
    """Match the private permissions of a state file created by an adapter."""
    from pseudolife_memory.coordination_adapter import _private_fd

    fd = os.open(path, os.O_RDONLY)
    try:
        _private_fd(fd, path)
    finally:
        os.close(fd)


def test_codex_delivery_does_not_advertise_a_claude_channel():
    async def drive():
        daemon = FakeDaemon()
        client, instance = adapter(daemon, wake_enabled=True, delivery_transport="codex")
        async with client, instance:
            capabilities = daemon.calls[0][1]["capabilities"]
            assert capabilities == {"pull": True, "channel": False, "codex": True,
                                    "resumable": False}
    asyncio.run(drive())


def test_codex_delivery_can_downgrade_same_identity_to_pull():
    async def drive():
        daemon = FakeDaemon()
        client, instance = adapter(
            daemon, wake_enabled=True, delivery_transport="codex")
        async with client, instance:
            identity = dict(instance.instance_headers)
            assert await instance.downgrade_to_pull() is True
            assert instance.wake_enabled is False
            assert instance.instance_headers == identity
            attaches = [body for action, body, _ in daemon.calls if action == "attach"]
            assert [body["wake_enabled"] for body in attaches] == [True, False]
            assert attaches[0]["attachment_id"] == attaches[1]["attachment_id"]
            assert instance._heartbeat_task is not None
            assert not instance._heartbeat_task.done()

    asyncio.run(drive())


def test_failed_pull_downgrade_stops_old_renewal_and_recovers_wake_false(monkeypatch):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter

        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", ())
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 100)
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0.01,))
        daemon = FakeDaemon()
        attach_count = 0

        def fail_downgrade(action, body):
            nonlocal attach_count
            if action == "attach":
                attach_count += 1
                if attach_count == 2:
                    raise httpx.ReadTimeout("private-fixture")

        daemon.hook = fail_downgrade
        client, instance = adapter(
            daemon, wake_enabled=True, delivery_transport="codex")
        async with client, instance:
            old_heartbeat = instance._heartbeat_task
            assert await instance.downgrade_to_pull() is False
            assert instance.wake_enabled is False
            assert old_heartbeat.done()
            while attach_count < 3 or instance._failure is not None:
                await asyncio.sleep(0.005)
            # The failed downgrade never resumes wake-capable heartbeats.  The
            # recovery attach reads the local False value.
            assert "heartbeat" not in daemon.actions()
            attaches = [body for action, body, _ in daemon.calls if action == "attach"]
            assert [body["wake_enabled"] for body in attaches] == [True, False, False]
            assert instance._heartbeat_task is not old_heartbeat
            assert not instance._heartbeat_task.done()
            assert instance._failure is None

    asyncio.run(drive())


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


def test_page_is_fenced_across_yields_when_generation_changes(monkeypatch):
    """A page remains owned by the generation that received it even while the
    async generator is suspended at a yield.  A new generation must replay
    from its reset cursor before another message from the old page is used."""
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0,))
        daemon = FakeDaemon()
        generation = {"value": 3}
        one = {"message_id": "mail-1", "sender_agent_id": "agent-b",
               "recipient_agent_id": "agent-a", "text": "one"}
        two = {**one, "message_id": "mail-2", "text": "two"}
        daemon.pages = [{"messages": [one, two], "after": "agent-a:2"}]

        def current_generation(action, body):
            if action in {"attach", "heartbeat"}:
                return httpx.Response(200, json={"generation": generation["value"]})

        daemon.hook = current_generation
        client, instance = adapter(daemon, wake_enabled=True)
        async with client, instance:
            async with aclosing(instance.inbox()) as inbox:
                assert (await anext(inbox)).content.endswith("\n\none")
                generation["value"] = 4
                await instance._reattach()
                daemon.pages = [{"messages": [one, two], "after": "agent-a:2"}]
                replay = await asyncio.wait_for(anext(inbox), 2)
                assert replay.content.endswith("\n\none")
                assert instance._after is None
        attempts = [(c[1]["message_id"], c[1]["generation"])
                    for c in daemon.calls if c[0] == "attempt"]
        assert attempts[:2] == [("mail-1", 3), ("mail-1", 4)]
        receives = [c[1] for c in daemon.calls if c[0] == "receive"]
        assert receives[1]["generation"] == 4 and receives[1]["after"] is None

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_saved_address_unknown_to_the_bank_is_retired_and_replaced(tmp_path, capsys):
    """An address proven absent (pruned after long idleness or missing from a
    restored snapshot) is retired and replaced. Credential rejection remains
    operator-recoverable state and is covered separately."""
    async def drive():
        state = tmp_path / "agent.json"
        state.write_text(json.dumps({"bank_url": "http://127.0.0.1:8099",
                                     "agent_id": "agent-old", "credential": "old-key"}),
                         encoding="utf-8")
        _secure_state(state)
        daemon = FakeDaemon()

        def hook(action, body):
            if action == "attach" and daemon.calls[-1][2].get("x-pl-agent") == "agent-old":
                return httpx.Response(404, json={"error": "instance_not_found"})

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


@pytest.mark.parametrize("status,code", [
    (401, "unauthorized"),
    (403, "invalid_credential"),
])
def test_auth_rejection_does_not_retire_a_saved_address(tmp_path, status, code):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        state = tmp_path / "agent.json"
        saved = {"bank_url": "http://127.0.0.1:8099",
                 "agent_id": "agent-old", "credential": "old-key"}
        state.write_text(json.dumps(saved), encoding="utf-8")
        _secure_state(state)
        original = state.read_bytes()
        daemon = FakeDaemon()
        daemon.hook = lambda action, body: httpx.Response(
            status, json={"error": code})
        client, instance = adapter(daemon, state_path=state)
        async with client:
            with pytest.raises(AdapterError) as caught:
                await instance.__aenter__()
        assert caught.value.code == code
        assert daemon.actions() == ["attach"]
        assert state.read_bytes() == original
        assert not state.with_name(state.name + ".stale").exists()

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_malformed_error_code_keeps_sanitized_http_classification(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError
        state = tmp_path / "agent.json"
        state.write_text(json.dumps({"bank_url": "http://127.0.0.1:8099",
                                     "agent_id": "agent-old", "credential": "old-key"}),
                         encoding="utf-8")
        _secure_state(state)
        original = state.read_bytes()
        daemon = FakeDaemon()
        daemon.hook = lambda action, body: httpx.Response(401, json={"error": []})
        client, instance = adapter(daemon, state_path=state)
        async with client:
            with pytest.raises(AdapterError) as caught:
                await instance.__aenter__()
        assert caught.value.status == 401 and caught.value.code is None
        assert state.read_bytes() == original
        assert not state.with_name(state.name + ".stale").exists()

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
        _secure_state(state)
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
        lock = state.with_name(state.name + ".lock")
        assert lock.is_file()
        if os.name != "nt":
            assert lock.stat().st_mode & 0o077 == 0
        else:
            _assert_windows_owner_only(lock)

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_stale_reservation_has_one_atomic_claimant(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError, CoordinationAdapter
        state = tmp_path / "agent.json"
        state.write_bytes(b"")
        _secure_state(state)
        old = time.time() - CoordinationAdapter.STALE_RESERVATION_SECONDS - 1
        os.utime(state, (old, old))
        daemon = FakeDaemon()
        entered = asyncio.Event()
        release = asyncio.Event()
        registrations = 0

        async def hold_first_registration(request):
            nonlocal registrations
            if request.url.path.endswith("/register"):
                registrations += 1
                if registrations == 1:
                    entered.set()
                    await release.wait()
            return await daemon(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(hold_first_registration)) as client:
            first = CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer",
                                        client=client, state_path=state)
            pending = asyncio.create_task(first.__aenter__())
            await entered.wait()
            second = CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer",
                                         client=client, state_path=state)
            with pytest.raises(AdapterError, match="another process"):
                await asyncio.wait_for(second.__aenter__(), 1)
            assert registrations == 1
            release.set()
            await pending
            await first.__aexit__(None, None, None)
        assert json.loads(state.read_text(encoding="utf-8"))["agent_id"] == "agent-a"

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_stale_reservation_lock_releases_after_cancelled_registration(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        state = tmp_path / "agent.json"
        state.write_bytes(b"")
        _secure_state(state)
        old = time.time() - CoordinationAdapter.STALE_RESERVATION_SECONDS - 1
        os.utime(state, (old, old))

        class SlowDaemon(FakeDaemon):
            async def __call__(self, request):
                if request.url.path.endswith("/register"):
                    await asyncio.sleep(10)
                return await super().__call__(request)

        client, cancelled = adapter(SlowDaemon(), state_path=state)
        async with client:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(cancelled.__aenter__(), 0.05)
        assert not state.exists()

        state.write_bytes(b"")
        _secure_state(state)
        os.utime(state, (old, old))
        daemon = FakeDaemon()
        client, recovered = adapter(daemon, state_path=state)
        async with client, recovered:
            pass
        assert daemon.actions()[:2] == ["register", "attach"]

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_stale_reservation_lock_hardlink_is_refused(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError, CoordinationAdapter
        state = tmp_path / "agent.json"
        state.write_bytes(b"")
        _secure_state(state)
        old = time.time() - CoordinationAdapter.STALE_RESERVATION_SECONDS - 1
        os.utime(state, (old, old))
        lock = state.with_name(state.name + ".lock")
        other = tmp_path / "other.lock"
        other.write_bytes(b"0")
        os.link(other, lock)
        daemon = FakeDaemon()
        client, instance = adapter(daemon, state_path=state)
        async with client:
            with pytest.raises(AdapterError, match="private regular file"):
                await instance.__aenter__()
        assert not daemon.calls
        assert state.exists() and other.read_bytes() == b"0"

    asyncio.run(asyncio.wait_for(drive(), 4))


def test_stale_reservation_lock_symlink_is_refused(tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import AdapterError, CoordinationAdapter
        state = tmp_path / "agent.json"
        state.write_bytes(b"")
        _secure_state(state)
        old = time.time() - CoordinationAdapter.STALE_RESERVATION_SECONDS - 1
        os.utime(state, (old, old))
        lock = state.with_name(state.name + ".lock")
        other = tmp_path / "other.lock"
        other.write_bytes(b"0")
        try:
            lock.symlink_to(other)
        except OSError as error:
            pytest.skip(f"symlinks unavailable: {error}")
        daemon = FakeDaemon()
        client, instance = adapter(daemon, state_path=state)
        async with client:
            with pytest.raises(AdapterError, match="private regular file"):
                await instance.__aenter__()
        assert not daemon.calls
        assert state.exists() and other.read_bytes() == b"0"

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


# An auth failure is fixed by correcting access or rebinding the saved
# identity. A missing address is not: rebind refuses a missing row, and the
# next start retires the state and registers a new address, so the advice
# names a restart and never suggests rebind.
@pytest.mark.parametrize("status,code,advice,never", [
    (401, "unauthorized", "restore/rebind", "restart"),
    (403, "invalid_credential", "restore/rebind", "restart"),
    (404, "instance_not_found", "restart the session", "rebind"),
])
def test_permanent_background_identity_failure_stops_retrying_and_preserves_state(
        monkeypatch, tmp_path, capsys, status, code, advice, never):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(CoordinationAdapter, "RETRY_DELAYS", (0, 0))
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0,))
        daemon = FakeDaemon()
        state = tmp_path / "agent.json"
        client, instance = adapter(daemon, state_path=state)
        await instance.__aenter__()
        original = state.read_bytes()

        def reject_identity(action, body):
            if action in {"heartbeat", "attach"}:
                return httpx.Response(status, json={"error": code})

        daemon.hook = reject_identity
        await asyncio.wait_for(instance._heartbeat_task, 1)
        calls = len(daemon.calls)
        await asyncio.sleep(0.05)
        assert len(daemon.calls) == calls
        assert instance._failure.code == code
        assert advice in instance.unread_hint
        assert never not in instance.unread_hint
        assert state.read_bytes() == original
        assert not state.with_name(state.name + ".stale").exists()
        await instance.__aexit__(None, None, None)

    asyncio.run(asyncio.wait_for(drive(), 4))
    stderr = capsys.readouterr().err
    assert stderr.count("background delivery stopped") == 1
    stopped = next(line for line in stderr.splitlines() if "background delivery stopped" in line)
    assert advice in stopped
    assert never not in stopped
    assert "fixture-key" not in stderr


def test_permanent_receive_failure_wakes_and_stops_heartbeat(monkeypatch, tmp_path):
    async def drive():
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        monkeypatch.setattr(CoordinationAdapter, "HEARTBEAT_SECONDS", 10)
        monkeypatch.setattr(CoordinationAdapter, "REATTACH_DELAYS", (0,))
        daemon = FakeDaemon()
        state = tmp_path / "agent.json"
        client, instance = adapter(daemon, state_path=state, wake_enabled=True)
        async with client, instance:
            original = state.read_bytes()
            daemon.hook = lambda action, body: (httpx.Response(
                403, json={"error": "invalid_credential"}) if action == "receive" else None)
            async with aclosing(instance.inbox()) as inbox:
                pending = asyncio.create_task(anext(inbox))
                await asyncio.wait_for(instance._heartbeat_task, 0.5)
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            assert instance._permanent_failure is True
            assert state.read_bytes() == original
            assert daemon.actions().count("attach") == 1

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
                "Coordination: 2 addressed messages pending (agent-origin, not user authority); "
                "read with memory_message receive, then ack each message_id.")
            assert instance.unread_hint == instance.unread_hint
            assert len(daemon.calls) == calls
            await instance._heartbeat()
            assert instance.unread_hint == (
                "Coordination: 4 addressed messages pending (agent-origin, not user authority); "
                "read with memory_message receive, then ack each message_id.")
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


# --- the daemon decides, the shim rings (v49) ------------------------------

def _preview(*ids):
    return [{"message_id": message_id, "sender_agent_id": "f" * 32, "sender_label": "peer",
             "created_at": 1789900000.0 + index, "excerpt": "note"}
            for index, message_id in enumerate(ids)]


def _mailbox_daemon(answers):
    """A daemon whose attach and heartbeat answers come from ``answers``:
    ``(count, preview, wake)`` per call."""
    daemon = FakeDaemon()
    answers = iter(answers)

    def hook(action, body):
        if action not in {"attach", "heartbeat"}:
            return None
        count, preview, wake = next(answers)
        return httpx.Response(200, json={"generation": 3, "lease_until": "later",
                                         "pending_count": count, "pending_preview": preview,
                                         "wake": wake})
    daemon.hook = hook
    return daemon


def test_a_daemon_ring_becomes_a_marker_beside_the_digest(tmp_path):
    """A heartbeat carrying ``wake`` writes ``<key>.ring`` for the Stop hook
    (the digest watermark, then the decision and its reason) and offers the
    same ring to the Codex doorbell once; ``<key>.agent`` names the address
    from attach on, so the hook can ask the park gate; both leave with the
    digest. A mailbox update without ``wake`` writes no marker."""
    daemon = _mailbox_daemon([(0, [], None),
                              (1, _preview("m1"), None),
                              (2, _preview("m1", "m2"),
                               {"decision": "rung", "reason": "anyone", "ring_at": 0.0}),
                              (2, _preview("m1", "m2"), None)])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                assert (tmp_path / "digest.agent").read_text() == "agent-a\n"
                assert coordination.ring_due() is None
                await coordination._heartbeat()
                assert not (tmp_path / "digest.ring").exists()
                assert coordination.ring_due() is None
                await coordination._heartbeat()
                await asyncio.sleep(0.05)      # a due ring is written at once
                assert (tmp_path / "digest.ring").read_text() == (
                    f"{coordination.digest_watermark}\nrung anyone\n")
                assert coordination.ring_due() == ("rung", "anyone")
                assert coordination.ring_due() is None
                await coordination._heartbeat()  # nothing new: the marker stands
                assert (tmp_path / "digest.ring").read_text().startswith(
                    f"{coordination.digest_watermark}\n")
            for suffix in (".txt", ".seen", ".ring", ".agent"):
                assert not (tmp_path / f"digest{suffix}").exists(), suffix

    asyncio.run(drive())
    ledger = (tmp_path / "ledger.log").read_text().splitlines()
    assert [line.split("\t")[1:2] + line.split("\t")[5:] for line in ledger] == [
        ["ring", "rung anyone"]]


def test_a_staggered_ring_waits_for_its_time(tmp_path):
    """The daemon staggers a fan-out burst by setting ``ring_at`` ahead; the
    marker and the doorbell's offer both wait for it."""
    daemon = _mailbox_daemon([(0, [], None),
                              (1, _preview("m1"),
                               {"decision": "rung", "reason": "anyone", "ring_at": time.time() + 0.6})])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                await coordination._heartbeat()
                await asyncio.sleep(0.1)
                assert not (tmp_path / "digest.ring").exists()
                assert coordination.ring_due() is None
                await asyncio.sleep(0.8)
                assert (tmp_path / "digest.ring").read_text().endswith("\nrung anyone\n")
                assert coordination.ring_due() == ("rung", "anyone")

    asyncio.run(drive())


def test_a_nudged_wake_rings_nothing(tmp_path):
    """Regular mail never wakes (maintainer decision 2026-10-02): a daemon
    from before it may still serve a ``nudged`` ring for an idle session
    that never parked. The shim writes no ``.ring`` marker for it, offers
    nothing to the Codex doorbell and logs no ring."""
    daemon = _mailbox_daemon([(0, [], None),
                              (1, _preview("m1"),
                               {"decision": "nudged", "reason": "no_park", "ring_at": time.time()})])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                await coordination._heartbeat()
                await asyncio.sleep(0.05)
                assert not (tmp_path / "digest.ring").exists()
                assert coordination.ring_due() is None

    asyncio.run(drive())
    ledger = tmp_path / "ledger.log"
    lines = ledger.read_text().splitlines() if ledger.exists() else []
    assert not [line for line in lines if line.split("\t")[1] == "ring"]


def test_a_malformed_wake_answer_rings_nothing(tmp_path):
    daemon = _mailbox_daemon([(0, [], None), (1, _preview("m1"), "ring!"),
                              (1, _preview("m1"), {"decision": "rung"}),
                              (1, _preview("m1"), {"decision": 7, "reason": "x", "ring_at": 0})])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                for _ in range(3):
                    await coordination._heartbeat()
                await asyncio.sleep(0.05)
                assert not (tmp_path / "digest.ring").exists()
                assert coordination.ring_due() is None

    asyncio.run(drive())


PARENT_THREAD = "01a0ec35-a19d-7043-9336-ac6b9863afd7"


def test_a_codex_child_names_its_parent_thread_at_register():
    """v50: a native Codex child registers with the parent thread the shim
    read from Codex's turn metadata; a root thread sends none."""
    daemon = FakeDaemon()

    async def drive(**kwargs):
        client, coordination = adapter(daemon, delivery_transport="codex", **kwargs)
        async with client:
            async with coordination:
                pass

    asyncio.run(drive(parent_thread=PARENT_THREAD))
    asyncio.run(drive())
    registers = [body for action, body, _ in daemon.calls if action == "register"]
    assert registers[0]["parent_thread"] == PARENT_THREAD
    assert "parent_thread" not in registers[1]


def test_a_daemon_older_than_v50_still_registers_the_child():
    """A shim updated before its daemon: the daemon refuses the unknown
    parameter once, and the child registers without the link rather than
    lose its board address."""
    daemon = FakeDaemon()
    refused = []

    def hook(action, body):
        if action == "register" and "parent_thread" in body:
            refused.append(body)
            return httpx.Response(400, json={"error": "unexpected_parameter"})
        return None
    daemon.hook = hook

    async def drive():
        client, coordination = adapter(daemon, delivery_transport="codex",
                                       parent_thread=PARENT_THREAD)
        async with client:
            async with coordination:
                assert coordination.instance_headers["X-PL-Agent"] == "agent-a"

    asyncio.run(drive())
    registers = [body for action, body, _ in daemon.calls if action == "register"]
    assert len(refused) == 1 and len(registers) == 2
    assert "parent_thread" not in registers[1]


def test_only_a_codex_adapter_takes_a_parent_thread():
    from pseudolife_memory.coordination_adapter import AdapterError, CoordinationAdapter
    with pytest.raises(AdapterError, match="parent thread"):
        CoordinationAdapter("http://127.0.0.1:8099", "fixture-bearer",
                            parent_thread=PARENT_THREAD)


def test_an_adapter_with_a_ring_path_says_so_at_attach(tmp_path):
    """A Claude shim with a digest (the Stop hook reads its .ring) or a
    Codex adapter the doorbell watches declares ``ring: true`` at attach;
    a daemon older than v49 refuses the parameter once, and the adapter
    attaches without it from then on."""
    daemon = FakeDaemon()
    refused = []

    def hook(action, body):
        if action == "attach" and "ring" in body:
            refused.append(body)
            return httpx.Response(400, json={"error": "unexpected_parameter"})
        return None
    daemon.hook = hook

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                pass

    asyncio.run(drive())
    attaches = [body for action, body, _ in daemon.calls if action == "attach"]
    assert attaches[0]["ring"] is True
    assert "ring_armed_until" in attaches[0]
    assert "ring_armed_until" not in attaches[1]
    assert "ring" not in attaches[2]
    register = next(body for action, body, _ in daemon.calls if action == "register")
    assert register["capabilities"]["ring"] is True

    daemon = FakeDaemon()

    async def plain():
        client, coordination = adapter(daemon)   # no digest, no doorbell: no ring path
        async with client:
            async with coordination:
                pass

    asyncio.run(plain())
    # Declared false, so a resumed address drops a ring path it once had.
    attach = next(body for action, body, _ in daemon.calls if action == "attach")
    assert attach["ring"] is False
    register = next(body for action, body, _ in daemon.calls if action == "register")
    assert "ring" not in register["capabilities"]


def test_the_agent_file_stays_fresh_with_the_digest(tmp_path):
    """Another shim's sweep removes marker files a day old; a live
    session's .agent is touched with its digest, as .seen is."""
    daemon = _mailbox_daemon([(0, [], None), (0, [], None)])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                agent = tmp_path / "digest.agent"
                old = time.time() - 2 * 86400
                os.utime(agent, (old, old))
                coordination._digest_written_at = -1e9     # the hourly rewrite is due
                await coordination._heartbeat()
                assert agent.stat().st_mtime > old + 86400

    asyncio.run(drive())


def test_a_ring_offer_the_session_already_saw_is_dropped(tmp_path):
    """The doorbell's offer carries the watermark it was for: once the
    prompt hook or a hint has shown that mail, the offer is gone, so a
    later withheld message cannot ring on it."""
    daemon = _mailbox_daemon([(0, [], None),
                              (1, _preview("m1"),
                               {"decision": "rung", "reason": "anyone", "ring_at": 0.0})])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                await coordination._heartbeat()
                (tmp_path / "digest.seen").write_text(f"{coordination.digest_watermark}\n")
                assert coordination.ring_due() is None

    asyncio.run(drive())


def test_a_ring_repeated_on_a_retried_heartbeat_is_taken_once(tmp_path):
    ring = {"decision": "rung", "reason": "anyone", "ring_at": 0.0}
    daemon = _mailbox_daemon([(0, [], None), (1, _preview("m1"), ring),
                              (1, _preview("m1"), ring)])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                await coordination._heartbeat()
                assert coordination.ring_due() == ("rung", "anyone")
                await coordination._heartbeat()
                assert coordination.ring_due() is None

    asyncio.run(drive())


def _fail_first_ring_write(coordination):
    """Make the first ``.ring`` write fail, as a Windows sharing violation
    does while a waiter holds the marker open."""
    write = coordination._write_private
    failed = []

    def flaky(path, body):
        if path.suffix == ".ring" and not failed:
            failed.append(path)
            return False
        return write(path, body)
    coordination._write_private = flaky
    return failed


@pytest.mark.parametrize("reoffered", [True, False], ids=["reoffered", "offer-lapsed"])
def test_a_ring_marker_the_filesystem_refused_is_written_at_the_next_heartbeat(
        tmp_path, reoffered):
    """Review finding 2026-10-02: the ring was recorded as taken before its
    marker was written, so a refused write was never retried and the
    daemon's repeated offer was dropped as a duplicate; a waiter slept to
    its timeout. The next heartbeat writes it, whether or not the daemon
    still offers the ring (a non-queued ring repeats for one interval)."""
    ring = {"decision": "rung", "reason": "anyone", "ring_at": 0.0}
    daemon = _mailbox_daemon([(0, [], None), (1, _preview("m1"), ring),
                              (1, _preview("m1"), ring if reoffered else None)])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                failed = _fail_first_ring_write(coordination)
                await coordination._heartbeat()
                await asyncio.sleep(0.05)
                assert failed and not (tmp_path / "digest.ring").exists()
                await coordination._heartbeat()
                await asyncio.sleep(0.05)
                assert (tmp_path / "digest.ring").read_text() == (
                    f"{coordination.digest_watermark}\nrung anyone\n")

    asyncio.run(drive())
    ledger = (tmp_path / "ledger.log").read_text().splitlines()
    assert [line.split("\t")[1] for line in ledger].count("ring") == 1


def test_a_refused_ring_marker_is_dropped_once_the_mail_is_gone(tmp_path):
    """The retry rings only for mail still pending: a mailbox emptied in
    the meantime leaves no marker behind for the next waiter to fire on."""
    ring = {"decision": "rung", "reason": "anyone", "ring_at": 0.0}
    daemon = _mailbox_daemon([(0, [], None), (1, _preview("m1"), ring), (0, [], None),
                              (0, [], None)])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                _fail_first_ring_write(coordination)
                for _ in range(3):
                    await coordination._heartbeat()
                    await asyncio.sleep(0.05)
                assert not (tmp_path / "digest.ring").exists()

    asyncio.run(drive())


def test_a_refused_ring_marker_is_dropped_once_its_mail_was_shown(tmp_path):
    """The retry keeps the watermark the ring was for: once a hint or the
    prompt hook showed that mail, a later digest change (an ack, plain
    mail) must not turn the old ring into a wake."""
    ring = {"decision": "rung", "reason": "anyone", "ring_at": 0.0}
    daemon = _mailbox_daemon([(0, [], None), (2, _preview("m1", "m2"), ring),
                              (1, _preview("m2"), None)])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                _fail_first_ring_write(coordination)
                await coordination._heartbeat()
                await asyncio.sleep(0.05)
                (tmp_path / "digest.seen").write_text(f"{coordination.digest_watermark}\n")
                await coordination._heartbeat()   # m1 acked: the digest moves on
                await asyncio.sleep(0.05)
                assert not (tmp_path / "digest.ring").exists()

    asyncio.run(drive())


def test_a_newer_ring_replaces_a_refused_one_and_keeps_its_stagger(tmp_path):
    """A newer ring replaces an older one, as its timer does: the refused
    marker is not written ahead of the newer ring's ``ring_at``, even when
    the heartbeat that brings the newer ring finds the filesystem writable
    again (review finding R1, 2026-10-02)."""
    daemon = _mailbox_daemon([
        (0, [], None),
        (1, _preview("m1"), {"decision": "rung", "reason": "anyone", "ring_at": 0.0}),
        (2, _preview("m1", "m2"),
         {"decision": "rung", "reason": "clearer", "ring_at": time.time() + 0.6}),
        (2, _preview("m1", "m2"), None)])

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                failed = _fail_first_ring_write(coordination)
                await coordination._heartbeat()
                await asyncio.sleep(0.05)
                assert failed
                await coordination._heartbeat()   # the newer ring; writable again
                await coordination._heartbeat()
                await asyncio.sleep(0.05)
                assert not (tmp_path / "digest.ring").exists()
                await asyncio.sleep(0.8)
                assert (tmp_path / "digest.ring").read_text().endswith("\nrung clearer\n")

    asyncio.run(drive())


def test_turn_and_marker_files_leave_with_the_digest(tmp_path):
    daemon = FakeDaemon()

    async def drive():
        client, coordination = adapter(daemon, digest_path=tmp_path / "digest.txt")
        async with client:
            async with coordination:
                (tmp_path / "digest.turn").write_text("1700000000\n")
        assert not (tmp_path / "digest.turn").exists()

    asyncio.run(drive())
