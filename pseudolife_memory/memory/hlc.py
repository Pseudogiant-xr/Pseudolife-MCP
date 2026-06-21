"""Hybrid Logical Clock (lite) — the ordering authority for canonical writes.

Wall clocks are for *display*; they can step backwards (NTP) and tick at coarse
resolution, so they must never decide which write supersedes which. An HLC packs
a physical millisecond with a logical counter so the stamp is **monotonic** (it
never decreases even when the wall clock does) and **wall-clock-anchored** (it
tracks real time when real time advances). The supersession order is
``(hlc_phys, hlc_logical, writer_id)`` — a total order across writers.

In shared-daemon mode (``write_mode='snapshot'``) only :meth:`tick` is used: one
daemon-held clock, trivially monotonic. :meth:`observe` is the standard HLC
receive rule for the multi-writer future (``write_mode='occ'``) — advance the
local clock past any remote stamp seen on read. It is unit-tested but only called
under ``occ``. See docs/specs/2026-06-21-writer-aware-temporal-memory-design.md.
"""
from __future__ import annotations

import time


def _wall_ms() -> int:
    return int(time.time() * 1000)


class HybridLogicalClock:
    """Injectable wall-ms source keeps it deterministic in tests."""

    def __init__(self, now_ms=_wall_ms) -> None:
        self._now = now_ms
        self._phys = 0
        self._logical = 0

    def tick(self) -> tuple[int, int]:
        """Stamp the next event. Monotonic regardless of wall-clock direction."""
        now = self._now()
        if now > self._phys:
            self._phys, self._logical = now, 0
        else:
            self._logical += 1            # same-or-backwards ms -> bump counter
        return (self._phys, self._logical)

    def observe(self, phys: int, logical: int) -> None:
        """Receive rule (Phase 2 / write_mode='occ'): on reading a remote stamp,
        advance the local clock past it so the next local tick outranks it."""
        if phys > self._phys:
            self._phys, self._logical = phys, logical
        elif phys == self._phys:
            self._logical = max(self._logical, logical)
