"""Slow service-lock telemetry (2026-09-01).

The 2026-08-31/09-01 hook-timeout forensics burned two sessions because
nothing in the daemon names a long lock hold: the sweep's phases log only
completions, and the service lock is silent. ``MonitoredLock`` wraps the
coarse service lock and warns — with the holder's function name and the
duration — whenever a hold or a wait crosses the reporting threshold, so
the next stall is a one-line diagnosis instead of a probe campaign.

Contracts:

* drop-in for ``threading.Lock`` under ``with`` (mutual exclusion holds);
* a hold longer than ``slow_seconds`` logs one warning naming the holder;
* a wait longer than ``slow_seconds`` logs one warning naming the waiter;
* fast holds/waits log nothing (the steady state stays silent);
* ``MemoryService`` actually uses it for ``_lock`` — the telemetry must
  cover the real lock, not exist beside it.
"""
from __future__ import annotations

import logging
import threading
import time


def test_monitored_lock_is_a_working_mutex():
    from pseudolife_memory.utils.locks import MonitoredLock

    lock = MonitoredLock("test")
    hits: list[int] = []

    def worker():
        with lock:
            n = len(hits)
            time.sleep(0.01)
            hits.append(n)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # With exclusion, each worker read the list length before anyone else
    # appended — so the recorded values are exactly 0..3.
    assert sorted(hits) == [0, 1, 2, 3]


def test_slow_hold_warns_with_holder_name(caplog):
    from pseudolife_memory.utils.locks import MonitoredLock

    lock = MonitoredLock("test", slow_seconds=0.05)

    def slow_holder():
        with lock:
            time.sleep(0.08)

    with caplog.at_level(logging.WARNING):
        slow_holder()
    msgs = [r.message for r in caplog.records if "test lock" in r.message]
    assert msgs, "a slow hold must warn"
    assert "slow_holder" in msgs[0], "the warning must name the holder"
    assert "held" in msgs[0]


def test_slow_wait_warns_with_waiter_name(caplog):
    from pseudolife_memory.utils.locks import MonitoredLock

    lock = MonitoredLock("test", slow_seconds=0.05)
    entered = threading.Event()

    def holder():
        with lock:
            entered.set()
            time.sleep(0.1)

    def slow_waiter():
        with lock:
            pass

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(1.0)
    with caplog.at_level(logging.WARNING):
        slow_waiter()
    t.join()
    msgs = [r.message for r in caplog.records if "waited" in r.message]
    assert msgs, "a slow wait must warn"
    assert "slow_waiter" in msgs[0], "the warning must name the waiter"


def test_fast_holds_stay_silent(caplog):
    from pseudolife_memory.utils.locks import MonitoredLock

    lock = MonitoredLock("test", slow_seconds=0.5)
    with caplog.at_level(logging.WARNING):
        for _ in range(50):
            with lock:
                pass
    assert not [r for r in caplog.records if "lock" in r.message]


def test_service_lock_is_monitored(tmp_path):
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.utils.locks import MonitoredLock

    svc = MemoryService(data_dir=tmp_path)
    assert isinstance(svc._lock, MonitoredLock), (
        "the service lock must be the monitored one — telemetry beside "
        "the real lock covers nothing")
