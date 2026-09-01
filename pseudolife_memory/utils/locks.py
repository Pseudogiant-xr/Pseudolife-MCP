"""Slow-hold telemetry for the coarse service lock.

The 2026-08-31/09-01 hook-timeout forensics burned two sessions because
nothing in the daemon names a long lock hold: phase completions get logged,
the lock itself is silent, so a stall can only be localized by external
probing. ``MonitoredLock`` is a drop-in ``threading.Lock`` wrapper that
warns — with the holder's function name and the duration — whenever a hold
or a wait crosses a threshold, turning the next stall into a one-line
diagnosis.

Overhead per acquisition is two ``perf_counter`` calls and one frame peek
(~1 µs), negligible against the storage round-trips every hold contains.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

logger = logging.getLogger("pseudolife-mcp")

# Reporting threshold. 1.0s chosen from the 2026-09-01 live-bank profile:
# every legitimate service-lock hold measured there totalled <=0.43s (the
# deep-dream bulk reads, at 1,134 entries / 4,935 entities), so anything
# over 1s is either a regression or the unexplained stall class this
# telemetry exists to name.
SLOW_LOCK_SECONDS = 1.0


def _caller_name(depth: int) -> str:
    """Qualified name of the frame ``depth`` levels above the caller —
    best-effort; telemetry must never raise into the locked path."""
    try:
        code = sys._getframe(depth + 1).f_code
        return getattr(code, "co_qualname", None) or code.co_name
    except Exception:  # noqa: BLE001 — valid on any interpreter state
        return "<unknown>"


class MonitoredLock:
    """``threading.Lock`` wrapper reporting slow holds and slow waits.

    Context-manager use only (the service code has no bare
    ``acquire``/``release`` call sites). ``_holder`` and ``_t_acquired``
    are written after ``acquire()`` returns and read before ``release()``
    — both strictly inside the critical section, so no cross-thread
    access exists; the wait warning names the WAITER from its own frame.
    """

    def __init__(self, name: str = "service",
                 slow_seconds: float = SLOW_LOCK_SECONDS) -> None:
        self._inner = threading.Lock()
        self._name = name
        self._slow = slow_seconds
        self._holder = "<none>"
        self._t_acquired = 0.0

    def __enter__(self) -> "MonitoredLock":
        t0 = time.perf_counter()
        self._inner.acquire()
        waited = time.perf_counter() - t0
        if waited >= self._slow:
            # The blocking holder has already released by now; its own
            # slow-hold line (below) is the authoritative attribution.
            logger.warning(
                "%s lock: %s waited %.2fs to acquire",
                self._name, _caller_name(1), waited)
        self._holder = _caller_name(1)
        self._t_acquired = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        held = time.perf_counter() - self._t_acquired
        holder = self._holder
        self._holder = "<none>"
        self._inner.release()
        if held >= self._slow:
            logger.warning("%s lock held %.2fs by %s",
                           self._name, held, holder)

    def locked(self) -> bool:
        return self._inner.locked()
