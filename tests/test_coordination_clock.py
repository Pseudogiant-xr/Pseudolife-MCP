"""A mailbox-only clock history survives restart and message retention."""
from types import SimpleNamespace

import pytest

from pseudolife_memory.memory.hlc import HybridLogicalClock
from pseudolife_memory.service import MemoryService


def test_reseed_hlc_uses_mailbox_highwater_without_slot_records(tmp_path):
    service = MemoryService(data_dir=tmp_path)
    service._hlc = HybridLogicalClock(now_ms=lambda: 100)
    service._storage = SimpleNamespace(get_meta=lambda key: [10000, 7])
    service._reseed_hlc()
    assert service._hlc.tick() > (10000, 7)


def test_reseed_hlc_preserves_later_slot_history(tmp_path):
    service = MemoryService(data_dir=tmp_path)
    service._hlc = HybridLogicalClock(now_ms=lambda: 100)
    service._storage = SimpleNamespace(get_meta=lambda key: [10000, 7])
    service._cortex = SimpleNamespace(records=[SimpleNamespace(hlc_phys=20000, hlc_logical=2)])
    service._reseed_hlc()
    assert service._hlc.tick() > (20000, 2)


def test_failed_highwater_read_retries_without_reinitializing(tmp_path):
    calls = []

    def get_meta(_key):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient metadata read failure")
        if len(calls) == 2:
            return [10000, 7]
        raise AssertionError("healthy steady state reread coordination metadata")

    service = MemoryService(data_dir=tmp_path)
    service._hlc = HybridLogicalClock(now_ms=lambda: 100)
    service._storage = SimpleNamespace(get_meta=get_meta)
    cms = service._cms = object()
    embedder = service._embedder = object()

    # Models and resident state were built before the late reseed failed.
    with pytest.raises(RuntimeError, match="transient metadata read failure"):
        service._reseed_hlc()

    # The next write's initialization gate must retry the clock read first.
    service._ensure_init()
    assert service._hlc.tick() > (10000, 7)
    assert service._cms is cms
    assert service._embedder is embedder

    service._ensure_init()
    assert len(calls) == 2


def test_malformed_highwater_stays_fail_closed_until_corrected(tmp_path):
    stored = {"value": "malformed", "reads": 0}

    def get_meta(_key):
        stored["reads"] += 1
        return stored["value"]

    service = MemoryService(data_dir=tmp_path)
    service._hlc = HybridLogicalClock(now_ms=lambda: 100)
    service._storage = SimpleNamespace(get_meta=get_meta)
    service._cms = object()

    with pytest.raises(ValueError, match="invalid coordination clock"):
        service._reseed_hlc()
    with pytest.raises(ValueError, match="invalid coordination clock"):
        service._ensure_init()

    stored["value"] = [20000, 2]
    service._ensure_init()
    assert service._hlc.tick() > (20000, 2)

    service._ensure_init()
    assert stored["reads"] == 3
