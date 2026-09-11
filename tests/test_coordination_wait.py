"""Async mailbox waiting releases workers and closes the subscribe/read race."""
import asyncio
from types import SimpleNamespace


def test_wait_sees_send_between_subscription_and_read(monkeypatch):
    from pseudolife_memory.web import coordination
    calls = []
    async def drive():
        hub = coordination.CoordinationHub(SimpleNamespace())
        def dispatch(service, action, params, **kw):
            calls.append(action)
            if len(calls) == 1:
                hub.notify("recipient")
                return {"messages": [], "after": None}
            return {"messages": [{"message_id": "m1"}], "after": "recipient:1"}
        monkeypatch.setattr(coordination, "dispatch", dispatch)
        result = await hub.handle("receive", {"wait_seconds": 1, "attachment_id": "att", "generation": 1},
                                  {"x-pl-agent": "recipient"}, "default")
        assert result["messages"] == [{"message_id": "m1"}]
        assert not hub.waiters
    asyncio.run(asyncio.wait_for(drive(), 2))


def test_cancelled_wait_releases_subscription(monkeypatch):
    from pseudolife_memory.web import coordination
    async def drive():
        hub = coordination.CoordinationHub(SimpleNamespace())
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        def dispatch(*a, **kw):
            loop.call_soon_threadsafe(started.set)
            return {"messages": [], "after": None}
        monkeypatch.setattr(coordination, "dispatch", dispatch)
        task = asyncio.create_task(hub.handle("receive", {"wait_seconds": 30, "attachment_id": "att", "generation": 1},
                                              {"x-pl-agent": "recipient"}, "default"))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert not hub.waiters
    asyncio.run(asyncio.wait_for(drive(), 2))


def test_long_wait_requires_attachment_generation():
    import pytest
    from pseudolife_memory.web.coordination import CoordinationHub
    async def drive():
        hub = CoordinationHub(SimpleNamespace())
        with pytest.raises(ValueError, match="attachment_required"):
            await hub.handle("receive", {"wait_seconds": 1}, {"x-pl-agent": "a"}, "default")
    asyncio.run(drive())


def test_repeated_cancellation_keeps_worker_capacity_until_thread_exits(monkeypatch):
    import threading
    from pseudolife_memory.web import coordination

    async def drive():
        hub = coordination.CoordinationHub(SimpleNamespace())
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))

        def dispatch(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            release.wait(2)
            raise RuntimeError("cancelled worker fixture failure")

        monkeypatch.setattr(coordination, "dispatch", dispatch)
        task = asyncio.create_task(hub.handle("receive", {
            "wait_seconds": 30, "attachment_id": "one", "generation": 1,
        }, {"x-pl-agent": "recipient"}, "default"))
        try:
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert not hub.waiters and not hub.attachments
            assert hub.pending_calls == 1
            assert hub.workers._value == 15
        finally:
            release.set()
            while hub.pending_calls:
                await asyncio.sleep(0.001)
            await asyncio.sleep(0)
        assert hub.workers._value == 16
        assert not unhandled

    asyncio.run(asyncio.wait_for(drive(), 3))
