"""Codex host metadata keeps coordination identities session-scoped."""
from __future__ import annotations

import asyncio
from pathlib import Path


THREAD_A = "aaaaaaaa-1111-4111-8111-111111111111"
THREAD_B = "22222222-2222-4222-8222-222222222222"


def test_thread_id_accepts_only_canonical_uuid_metadata():
    from pseudolife_memory.codex_coordination import thread_id_from_meta

    assert thread_id_from_meta({"threadId": THREAD_A}) == THREAD_A
    assert thread_id_from_meta({"threadId": THREAD_A.upper()}) is None
    assert thread_id_from_meta({"threadId": "../../shared.json"}) is None
    assert thread_id_from_meta({"threadId": 7}) is None
    assert thread_id_from_meta(None) is None


def test_state_path_is_stable_isolated_and_contains_no_identity(tmp_path):
    from pseudolife_memory.codex_coordination import state_path_for_thread

    first = state_path_for_thread(tmp_path, "http://127.0.0.1:8765", "token-a", THREAD_A)
    resumed = state_path_for_thread(tmp_path, "http://127.0.0.1:8765/", "token-a", THREAD_A)
    other_thread = state_path_for_thread(
        tmp_path, "http://127.0.0.1:8765", "token-a", THREAD_B)
    other_token = state_path_for_thread(
        tmp_path, "http://127.0.0.1:8765", "token-b", THREAD_A)

    assert first == resumed
    assert len({first, other_thread, other_token}) == 3
    rendered = str(first)
    assert THREAD_A not in rendered
    assert "token-a" not in rendered
    assert first.suffix == ".json"


def test_registry_registers_once_for_concurrent_first_calls_and_closes(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    entered = asyncio.Event()
    release = asyncio.Event()
    instances = []

    class Adapter:
        def __init__(self, *args, **kwargs):
            self.options = kwargs
            self.instance_headers = {
                "X-PL-Agent": "fixture-agent",
                "X-PL-Agent-Key": "private-fixture",
            }
            self.unread_hint = None
            self.closed = False
            instances.append(self)

        async def __aenter__(self):
            entered.set()
            await release.wait()
            return self

        async def __aexit__(self, *exc):
            self.closed = True

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=1)
        first = asyncio.create_task(registry.get(THREAD_A))
        await entered.wait()
        second = asyncio.create_task(registry.get(THREAD_A))
        await asyncio.sleep(0)
        assert len(instances) == 1
        release.set()
        assert await first is await second
        assert instances[0].options["episode"] == THREAD_A
        await registry.aclose()

    asyncio.run(drive())
    assert instances[0].closed is True


def test_registry_uses_distinct_paths_and_restart_reuses_path(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    paths: list[Path] = []

    class Adapter:
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = None

        def __init__(self, *args, **kwargs):
            paths.append(Path(kwargs["state_path"]))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

    async def one_run(*thread_ids):
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=1)
        for thread_id in thread_ids:
            await registry.get(thread_id)
        await registry.aclose()

    asyncio.run(one_run(THREAD_A, THREAD_B))
    asyncio.run(one_run(THREAD_A))
    assert paths[0] != paths[1]
    assert paths[0] == paths[2]


def test_registry_failure_is_safe_and_retryable(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    attempts = 0

    class Adapter:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("secret response")
            self.instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
            self.unread_hint = None
            return self

        async def __aexit__(self, *exc):
            pass

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=1, retry_seconds=0)
        assert await registry.get(THREAD_A) is None
        assert await registry.get(THREAD_A) is not None
        await registry.aclose()

    asyncio.run(drive())
    assert attempts == 2


def test_registry_failure_has_per_thread_retry_cooldown(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    attempts = 0
    now = [100.0]

    class Adapter:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("unavailable")

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=1, retry_seconds=5,
            clock=lambda: now[0])
        assert await registry.get(THREAD_A) is None
        assert await registry.get(THREAD_A) is None
        assert attempts == 1
        now[0] += 5
        assert await registry.get(THREAD_A) is None
        assert attempts == 2

    asyncio.run(drive())


def test_registry_bounds_thread_and_lock_entries(tmp_path, capsys):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    instances = []

    class Adapter:
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = None
        def __init__(self, *args, **kwargs): instances.append(self)
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=1, max_threads=1)
        assert await registry.get(THREAD_A) is not None
        assert await registry.get(THREAD_B) is None
        assert len(registry._locks) == 1
        assert "registry capacity" in registry.unread_hint(THREAD_B, None)
        await registry.aclose()

    asyncio.run(drive())
    assert len(instances) == 1
    assert "registry capacity" in capsys.readouterr().err


def test_registry_rejects_invalid_thread_before_allocating_lock(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    class Adapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError("invalid host metadata must not reach an adapter")

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter)
        assert await registry.get("not-a-thread") is None
        assert registry._locks == {}

    asyncio.run(drive())


def test_registry_rejects_symlinked_state_root_before_adapter(tmp_path, monkeypatch):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    linked = tmp_path / "linked"
    linked.mkdir()
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink",
        lambda path: path == linked or original_is_symlink(path))

    class Adapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError("symlinked credential roots must fail before adapter startup")

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=linked,
            adapter_factory=Adapter, startup_seconds=1, retry_seconds=5)
        assert await registry.get(THREAD_A) is None

    asyncio.run(drive())


def test_verified_codex_delivery_enables_wake_before_adapter_registration(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    order = []
    options = {}

    class Delivery:
        def __init__(self, url, token, thread_id):
            order.append(("delivery", thread_id))
        async def __aenter__(self):
            order.append("delivery-enter")
            return self
        async def verify(self):
            order.append("verify")
        async def deliver(self, event):
            raise AssertionError("fixture inbox stays idle")
        async def __aexit__(self, *exc):
            order.append("delivery-exit")

    class Adapter:
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = None
        def __init__(self, *args, **kwargs):
            options.update(kwargs)
            order.append("adapter")
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        async def inbox(self):
            await asyncio.Event().wait()
            if False: yield

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=1,
            delivery_url="ws://127.0.0.1:9999", delivery_token="host-token",
            delivery_factory=Delivery)
        assert await registry.get(THREAD_A) is not None
        await registry.aclose()

    asyncio.run(drive())
    assert order.index("verify") < order.index("adapter")
    assert options["wake_enabled"] is True
    assert options["delivery_transport"] == "codex"
    assert order[-1] == "delivery-exit"


def test_unverified_codex_delivery_registers_pull_only(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    options = {}
    closed = []

    class Delivery:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def verify(self): raise RuntimeError("host refused")
        async def __aexit__(self, *exc): closed.append(True)

    class Adapter:
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = None
        def __init__(self, *args, **kwargs): options.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=1,
            delivery_url="ws://127.0.0.1:9999", delivery_token="host-token",
            delivery_factory=Delivery)
        assert await registry.get(THREAD_A) is not None
        await registry.aclose()

    asyncio.run(drive())
    assert closed == [True]
    assert options["wake_enabled"] is False
    assert options["delivery_transport"] == "codex"


def test_runtime_delivery_failure_stops_push_and_exposes_pull_hint(tmp_path, capsys):
    from pseudolife_memory.channel import ChannelEvent
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    attempted = asyncio.Event()
    downgraded = asyncio.Event()
    inbox_closed = []

    class Delivery:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def verify(self): pass
        async def deliver(self, event):
            attempted.set()
            raise RuntimeError("secret bridge detail")
        async def __aexit__(self, *exc): pass

    class Adapter:
        instance_headers = {"X-PL-Agent": "a", "X-PL-Agent-Key": "k"}
        unread_hint = None
        def __init__(self, *args, **kwargs): self.wake_enabled = True
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        async def downgrade_to_pull(self):
            self.wake_enabled = False
            downgraded.set()
            return True
        async def inbox(self):
            try:
                yield ChannelEvent("message", {"message_id": "m1"})
                raise AssertionError("delivery must stop after an uncertain failure")
            finally:
                inbox_closed.append(True)

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=1,
            delivery_url="ws://127.0.0.1:9999", delivery_token="host-token",
            delivery_factory=Delivery)
        adapter = await registry.get(THREAD_A)
        await asyncio.wait_for(attempted.wait(), 1)
        await asyncio.wait_for(downgraded.wait(), 1)
        await asyncio.sleep(0)
        hint = registry.unread_hint(THREAD_A, adapter)
        assert adapter.wake_enabled is False
        await registry.aclose()
        return hint

    hint = asyncio.run(drive())
    assert "live Codex delivery stopped" in hint
    assert "memory_message receive" in hint
    assert inbox_closed == [True]
    err = capsys.readouterr().err
    assert "secret bridge detail" not in err
    assert "pull remains available" in err


def test_cancellation_during_delivery_verification_closes_bridge(tmp_path):
    import pytest
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    verifying = asyncio.Event()
    closed = []

    class Delivery:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def verify(self):
            verifying.set()
            await asyncio.Event().wait()
        async def __aexit__(self, *exc): closed.append(True)

    class Adapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError("adapter must wait for verified bridge")

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=30,
            delivery_url="ws://127.0.0.1:9999", delivery_token="host-token",
            delivery_factory=Delivery)
        pending = asyncio.create_task(registry.get(THREAD_A))
        await verifying.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(drive())
    assert closed == [True]


def test_aclose_cancels_inflight_startup_and_closes_partial_adapter(tmp_path):
    import pytest
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    started = asyncio.Event()
    closed = asyncio.Event()
    lease_workers = []

    class Adapter:
        def __init__(self, *args, **kwargs):
            self.lease_worker = None
        async def __aenter__(self):
            self.lease_worker = asyncio.create_task(asyncio.Event().wait())
            lease_workers.append(self.lease_worker)
            started.set()
            await asyncio.Event().wait()
        async def __aexit__(self, *exc):
            self.lease_worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await self.lease_worker
            closed.set()

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=30)
        startup = asyncio.create_task(registry.get(THREAD_A))
        await started.wait()
        await asyncio.wait_for(registry.aclose(), 1)
        with pytest.raises(asyncio.CancelledError):
            await startup
        assert registry._adapters == {}
        assert registry._delivery_tasks == {}
        assert closed.is_set()
        assert all(worker.done() for worker in lease_workers)

    asyncio.run(drive())


def test_get_after_registry_close_never_starts_adapter(tmp_path):
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    class Adapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError("closed registry must not start adapters")

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter)
        await registry.aclose()
        assert await registry.get(THREAD_A) is None

    asyncio.run(drive())


def test_cancellation_during_adapter_attach_closes_verified_bridge(tmp_path):
    import pytest
    from pseudolife_memory.codex_coordination import CodexCoordinationRegistry

    attaching = asyncio.Event()
    closed = []

    class Delivery:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def verify(self): pass
        async def __aexit__(self, *exc): closed.append(True)

    class Adapter:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self):
            attaching.set()
            await asyncio.Event().wait()

    async def drive():
        registry = CodexCoordinationRegistry(
            "http://127.0.0.1:8765", "fixture-token", state_dir=tmp_path,
            adapter_factory=Adapter, startup_seconds=30,
            delivery_url="ws://127.0.0.1:9999", delivery_token="host-token",
            delivery_factory=Delivery)
        pending = asyncio.create_task(registry.get(THREAD_A))
        await attaching.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(drive())
    assert closed == [True]
