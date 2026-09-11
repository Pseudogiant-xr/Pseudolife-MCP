"""Codex thread metadata to private, resumable coordination identities."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import hashlib
import os
from pathlib import Path
import sys
import time
import uuid


def thread_id_from_meta(meta) -> str | None:
    """Return an authoritative-looking canonical Codex thread UUID.

    Codex injects ``_meta.threadId`` at the MCP transport boundary.  Rejecting
    every other shape keeps model-controlled arguments and inherited process
    environment out of coordination identity selection.
    """
    if not isinstance(meta, dict):
        return None
    candidate = meta.get("threadId")
    if not isinstance(candidate, str):
        return None
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    return canonical if candidate == canonical else None


def default_state_dir() -> Path:
    configured = os.environ.get("PSEUDOLIFE_AGENT_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "pseudolife" / "agents"


def state_path_for_thread(state_dir: Path, url: str, token: str,
                          thread_id: str) -> Path:
    """Build an opaque path scoped by bank, principal token, and thread."""
    token_hash = hashlib.sha256(token.encode("utf-8")).digest()
    namespace = hashlib.sha256(
        url.rstrip("/").encode("utf-8") + b"\0" + token_hash).hexdigest()[:32]
    thread_hash = hashlib.sha256(thread_id.encode("ascii")).hexdigest()
    return Path(state_dir) / namespace / f"{thread_hash}.json"


def _reject_symlink_ancestors(path: Path) -> None:
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise OSError("coordination state path has a symbolic-link ancestor")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _prepare_private_dir(root: Path, namespace: Path) -> None:
    _reject_symlink_ancestors(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlink_ancestors(root)
    namespace.mkdir(mode=0o700, parents=False, exist_ok=True)
    _reject_symlink_ancestors(namespace)
    if not root.is_dir() or not namespace.is_dir():
        raise OSError("coordination state directory is not a private directory")
    if os.name != "nt":
        root.chmod(0o700)
        namespace.chmod(0o700)


class CodexCoordinationRegistry:
    """Lazily attach one adapter for each validated Codex thread ID."""

    def __init__(self, url: str, token: str, *, state_dir=None,
                 adapter_factory=None, startup_seconds: float = 3.0,
                 retry_seconds: float = 5.0,
                 max_threads: int = 128, clock=None, delivery_url=None,
                 delivery_token=None, delivery_factory=None):
        self.url = url
        self.token = token
        self.state_dir = Path(state_dir) if state_dir is not None else default_state_dir()
        self._adapter_factory = adapter_factory
        self._startup_seconds = startup_seconds
        self._retry_seconds = retry_seconds
        self._max_threads = max_threads
        self._clock = clock or time.monotonic
        self._delivery_url = delivery_url
        self._delivery_token = delivery_token
        self._delivery_factory = delivery_factory
        self._adapters: dict[str, object] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._retry_after: dict[str, float] = {}
        self._deliveries: dict[str, object] = {}
        self._delivery_tasks: dict[str, asyncio.Task] = {}
        self._delivery_failed: set[str] = set()
        self._entry_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._startups: set[asyncio.Task] = set()
        self._closing = False
        self._closed = False
        self._failure_reported = False
        self._capacity_reported = False
        self._delivery_setup_reported = False

    def _factory(self):
        if self._adapter_factory is not None:
            return self._adapter_factory
        from pseudolife_memory.coordination_adapter import CoordinationAdapter
        return CoordinationAdapter

    def _delivery_class(self):
        if self._delivery_factory is not None:
            return self._delivery_factory
        from pseudolife_memory.codex_delivery import CodexDelivery
        return CodexDelivery

    async def _verified_delivery(self, thread_id: str):
        if not self._delivery_url or not self._delivery_token:
            return None
        delivery = None
        try:
            candidate = self._delivery_class()(
                self._delivery_url, self._delivery_token, thread_id)
            delivery = await asyncio.wait_for(
                candidate.__aenter__(), timeout=self._startup_seconds)
            await asyncio.wait_for(
                delivery.verify(), timeout=self._startup_seconds)
            return delivery
        except asyncio.CancelledError:
            if delivery is not None:
                try:
                    await delivery.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 - preserve cancellation
                    pass
            raise
        except Exception:  # noqa: BLE001 - verified push is optional
            if delivery is not None:
                try:
                    await delivery.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 - best-effort failed setup cleanup
                    pass
            if not self._delivery_setup_reported:
                print("pseudolife-mcp: Codex live delivery unavailable; using pull "
                      "coordination.", file=sys.stderr)
                self._delivery_setup_reported = True
            return None

    async def _pump_delivery(self, thread_id: str, adapter, delivery) -> None:
        failed = False
        try:
            async with aclosing(adapter.inbox()) as inbox:
                async for event in inbox:
                    await delivery.deliver(event)
            failed = True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - stop after an uncertain host result
            failed = True
        finally:
            if failed:
                self._delivery_failed.add(thread_id)
                print("pseudolife-mcp: live Codex delivery stopped; pull remains "
                      "available through memory_message receive.", file=sys.stderr)
                try:
                    await adapter.downgrade_to_pull()
                except Exception:  # noqa: BLE001 - local pull mode is already set
                    pass
            try:
                await delivery.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
            if self._deliveries.get(thread_id) is delivery:
                self._deliveries.pop(thread_id, None)
            if not failed:
                self._delivery_failed.discard(thread_id)

    def unread_hint(self, thread_id: str, adapter) -> str | None:
        if thread_id in self._delivery_failed:
            return ("Coordination: live Codex delivery stopped; use memory_message "
                    "receive for pull delivery.")
        if adapter is None:
            if thread_id not in self._locks and len(self._locks) >= self._max_threads:
                return ("Coordination: registry capacity reached; ordinary memory "
                        "remains available.")
            return ("Coordination: identity attachment unavailable; ordinary memory "
                    "remains available.")
        return adapter.unread_hint

    async def get(self, thread_id: str):
        canonical = thread_id_from_meta({"threadId": thread_id})
        if canonical is None:
            return None
        startup = asyncio.current_task()
        async with self._entry_lock:
            if self._closing:
                return None
            self._startups.add(startup)
        try:
            return await self._get_started(canonical)
        finally:
            async with self._entry_lock:
                self._startups.discard(startup)

    async def _get_started(self, thread_id: str):
        existing = self._adapters.get(thread_id)
        if existing is not None:
            return existing
        if self._clock() < self._retry_after.get(thread_id, 0):
            return None
        async with self._entry_lock:
            existing = self._adapters.get(thread_id)
            if existing is not None:
                return existing
            if self._clock() < self._retry_after.get(thread_id, 0):
                return None
            lock = self._locks.get(thread_id)
            if lock is None:
                if len(self._locks) >= self._max_threads:
                    if not self._capacity_reported:
                        print("pseudolife-mcp: coordination registry capacity reached; "
                              "memory proxy remains active.", file=sys.stderr)
                        self._capacity_reported = True
                    return None
                lock = self._locks[thread_id] = asyncio.Lock()
        async with lock:
            existing = self._adapters.get(thread_id)
            if existing is not None:
                return existing
            if self._clock() < self._retry_after.get(thread_id, 0):
                return None
            delivery = None
            candidate_adapter = None
            try:
                path = state_path_for_thread(
                    self.state_dir, self.url, self.token, thread_id)
                _prepare_private_dir(self.state_dir, path.parent)
                delivery = await self._verified_delivery(thread_id)
                candidate_adapter = self._factory()(
                    self.url, self.token, state_path=path,
                    wake_enabled=delivery is not None,
                    delivery_transport="codex",
                    label=os.environ.get("PSEUDOLIFE_AGENT_LABEL", "codex"),
                    project=os.environ.get("PSEUDOLIFE_AGENT_PROJECT", ""),
                    task=os.environ.get("PSEUDOLIFE_AGENT_TASK", ""),
                    episode=thread_id)
                adapter = await asyncio.wait_for(
                    candidate_adapter.__aenter__(), timeout=self._startup_seconds)
            except asyncio.CancelledError:
                if candidate_adapter is not None:
                    try:
                        await candidate_adapter.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001 - preserve cancellation
                        pass
                if delivery is not None:
                    try:
                        await delivery.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001 - preserve cancellation
                        pass
                raise
            except Exception:  # noqa: BLE001 - memory must survive optional coordination
                if candidate_adapter is not None:
                    try:
                        await candidate_adapter.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001 - failed startup cleanup
                        pass
                if delivery is not None:
                    try:
                        await delivery.__aexit__(None, None, None)
                    except Exception:  # noqa: BLE001 - best-effort failed attach cleanup
                        pass
                self._retry_after[thread_id] = self._clock() + self._retry_seconds
                if not self._failure_reported:
                    print("pseudolife-mcp: coordination unavailable; memory proxy remains "
                          "active. Check daemon opt-in, authentication and private adapter "
                          "state.", file=sys.stderr)
                    self._failure_reported = True
                return None
            self._adapters[thread_id] = adapter
            if delivery is not None:
                self._deliveries[thread_id] = delivery
                self._delivery_tasks[thread_id] = asyncio.create_task(
                    self._pump_delivery(thread_id, adapter, delivery))
            self._retry_after.pop(thread_id, None)
            self._failure_reported = False
            return adapter

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            async with self._entry_lock:
                self._closing = True
                current = asyncio.current_task()
                startups = [task for task in self._startups if task is not current]
            for task in startups:
                task.cancel()
            if startups:
                await asyncio.gather(*startups, return_exceptions=True)

            delivery_tasks = list(self._delivery_tasks.values())
            self._delivery_tasks.clear()
            for task in delivery_tasks:
                task.cancel()
            if delivery_tasks:
                await asyncio.gather(*delivery_tasks, return_exceptions=True)
            deliveries = list(self._deliveries.values())
            self._deliveries.clear()
            for delivery in deliveries:
                try:
                    await delivery.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 - shutdown is best effort
                    pass
            adapters = list(self._adapters.values())
            self._adapters.clear()
            self._locks.clear()
            self._retry_after.clear()
            self._delivery_failed.clear()
            for adapter in reversed(adapters):
                try:
                    await adapter.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 - shutdown is best effort
                    pass
            self._closed = True
