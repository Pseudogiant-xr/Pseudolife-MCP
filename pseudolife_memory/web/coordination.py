"""Bounded async receive waits; database operations remain short and synchronous."""
from __future__ import annotations

import asyncio
import math

from pseudolife_memory.coordination import dispatch


class CoordinationHub:
    # Conservative initial connection budget; no throughput claim is implied.
    MAX_WAITERS = 64
    MAX_WAIT_SECONDS = 30

    def __init__(self, service):
        self.service = service
        self.loop = None
        self.waiters: dict[str, set[asyncio.Event]] = {}
        self.attachments: set[tuple] = set()
        self.workers = asyncio.Semaphore(16)
        self.pending_calls = 0
        self.jobs: set[asyncio.Task] = set()
        service._coordination_notifier = self.notify

    def notify(self, recipient: str) -> None:
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._notify, recipient)

    def _notify(self, recipient):
        for event in self.waiters.get(recipient, ()):
            event.set()

    async def handle(self, action, parameters, headers, principal):
        self.loop = asyncio.get_running_loop()
        parameters = dict(parameters)
        wait = parameters.pop("wait_seconds", 0) if action == "receive" else 0
        if (type(wait) not in (int, float) or not math.isfinite(wait)
                or not 0 <= wait <= self.MAX_WAIT_SECONDS):
            raise ValueError("invalid_wait_seconds")
        async def invoke():
            if self.pending_calls >= self.MAX_WAITERS:
                raise ValueError("wait_capacity_exceeded")
            self.pending_calls += 1

            async def run():
                async with self.workers:
                    return await asyncio.to_thread(
                        dispatch, self.service, action, parameters,
                        headers=headers, principal=principal)

            def finished(job):
                self.jobs.discard(job)
                self.pending_calls -= 1
                # A disconnected caller no longer retrieves the result. Consume
                # failures here without exposing message or credential data.
                if not job.cancelled():
                    job.exception()

            # The independent job owns capacity until its synchronous work ends.
            # Repeated cancellation of an HTTP request cannot release it early.
            job = asyncio.create_task(run())
            self.jobs.add(job)
            job.add_done_callback(finished)
            return await asyncio.shield(job)
        if not wait:
            return await invoke()
        recipient = headers.get("x-pl-agent", "")
        if not recipient:
            raise ValueError("instance_authentication_required")
        if not parameters.get("attachment_id") or type(parameters.get("generation")) is not int:
            raise ValueError("attachment_required")
        attachment = (recipient, parameters["attachment_id"], parameters["generation"])
        if attachment in self.attachments:
            raise ValueError("attachment_already_waiting")
        if sum(map(len, self.waiters.values())) >= self.MAX_WAITERS:
            raise ValueError("wait_capacity_exceeded")
        event = asyncio.Event()
        self.attachments.add(attachment)
        self.waiters.setdefault(recipient, set()).add(event)
        deadline = self.loop.time() + wait
        try:
            while True:
                # Subscribe/clear BEFORE reading durable state. A commit during
                # that read sets the event and cannot disappear into a race gap.
                event.clear()
                result = await invoke()
                if result.get("messages") or result.get("enabled") is False:
                    return result
                remaining = deadline - self.loop.time()
                if remaining <= 0:
                    return result
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except TimeoutError:
                    # A timeout still rereads the authoritative DB, covering
                    # restart or any external writer without this event loop.
                    return await invoke()
        finally:
            self.attachments.discard(attachment)
            subscriptions = self.waiters[recipient]
            subscriptions.discard(event)
            if not subscriptions:
                del self.waiters[recipient]
