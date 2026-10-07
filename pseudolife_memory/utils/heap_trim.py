"""Hand freed heap memory back to the OS between request bursts (glibc).

glibc keeps much of what a burst of large allocations frees: its dynamic
mmap threshold rises after the first large free, later buffers are carved
from the arenas instead, and free space inside an arena goes back to the
OS only through ``malloc_trim`` or when a later allocation pattern happens
to let the arena shrink. The embedder's encodes are exactly such bursts.
Measured 2026-09-23 in throwaway containers from the 0.15.0 image, four
persistent worker threads (the daemon's threadpool shape) running
concurrent encodes left up to 3,158 MiB of freed memory resident with the
fp32 embedder (1,693 MiB bf16), and 1,007-1,433 MiB of it (bf16
897-1,476 MiB) was still there at idle with MALLOC_ARENA_MAX=2;
2,616-2,739 MiB with glibc's default arenas
(``evals/results/allocator-trim-pool-20260923.json``).

A daemon thread calls ``malloc_trim(0)`` every
``PSEUDOLIFE_MALLOC_TRIM_SECONDS``. That returns what glibc is holding
free after a burst; it does not lower the peak the burst itself needs.

Linux/glibc only; elsewhere (Windows, macOS, musl) there is no trim
function and nothing starts.
"""
from __future__ import annotations

import ctypes
import logging
import math
import os
import sys
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

INTERVAL_ENV = "PSEUDOLIFE_MALLOC_TRIM_SECONDS"
# How often the thread trims; every check trims (a growth gate was tried
# and failed review: a check landing mid-burst, when the memory is still
# live, set its baseline so high that the retention left afterwards was
# never trimmed). Measured 2026-09-23 in throwaway containers from the
# 0.15.0 image (allocator-trim-probe-20260923.json, trimming after every
# burst): a trim took a median 7 ms, 58 ms at p95 and at most 141 ms
# (returning 1,381 MiB). With four persistent workers
# (allocator-trim-pool-20260923.json), trims returning 1,387-3,030 MiB
# after a concurrent burst took 21-54 ms. Across all runs, the 137 trims
# with under 64 MiB to return took a median 1.2 ms, at most 19 ms. In
# paired within-process runs (allocator-trim-latency-20260923.json) the
# fp32 4 x 512-token encode right after a trim was no slower (+0.01 s
# inside a 0.23 s noise floor; ~16.8k more minor faults, ~14 ms of fault
# handling), nor was the first query. glibc holds each arena's lock while
# trimming it, so an allocation racing a large trim can wait that long, at
# most once a minute.
DEFAULT_INTERVAL_SECONDS = 60.0
# A trim that returned less than this is not logged. Measured 2026-09-23:
# after each trim, resident anon memory moved at most 53 MiB from burst to
# burst with MALLOC_ARENA_MAX=2 (97 MiB with glibc's default arenas), while
# the trims that followed a concurrent burst returned 1,387-3,030 MiB, so
# smaller returns are mostly jitter.
LOG_MIN_BYTES = 64 * 1024 * 1024
_MIB = 1024 * 1024


def load_malloc_trim() -> Callable[[int], int] | None:
    """glibc's ``malloc_trim``, or None where there is no glibc."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


def read_anon_bytes() -> int | None:
    """This process's resident anonymous memory (``RssAnon``)."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("RssAnon:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def trim_once(malloc_trim: Callable[[int], int],
              read_anon: Callable[[], int | None] = read_anon_bytes) -> int | None:
    """Trim, and return how much resident anon memory dropped (None when it
    cannot be read). Negative when other threads allocated meanwhile."""
    before = read_anon()
    malloc_trim(0)
    after = read_anon()
    if before is None or after is None:
        return None
    return before - after


def _trim_loop(interval: float, malloc_trim: Callable[[int], int]) -> None:
    while True:
        time.sleep(interval)
        try:
            released = trim_once(malloc_trim)
        except Exception as exc:  # noqa: BLE001 — must never kill the daemon
            logger.warning("heap trim error: %s", exc)
            continue
        if released is not None and released >= LOG_MIN_BYTES:
            logger.info("heap trim: returned %d MiB of freed heap to the OS",
                        released // _MIB)


_started = False


def start() -> bool:
    """Idempotent: start the trim thread. Daemon-only. False when it is
    disabled (interval 0), already running, or there is no glibc."""
    global _started
    if _started:
        return False
    # Empty = unset: compose passes an ops/.env value it does not find as "".
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    try:
        interval = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        interval = math.nan
    if not math.isfinite(interval):
        raise ValueError(
            f"{INTERVAL_ENV} must be a number of seconds (0 disables), "
            f"got {raw!r}")
    if interval <= 0:
        return False
    malloc_trim = load_malloc_trim()
    if malloc_trim is None:
        return False
    _started = True
    threading.Thread(
        target=_trim_loop, args=(interval, malloc_trim),
        daemon=True, name="pl-heap-trim",
    ).start()
    logger.info("heap trim: returning freed heap to the OS every %.0fs",
                interval)
    return True
