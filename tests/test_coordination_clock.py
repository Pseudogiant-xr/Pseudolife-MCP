"""A mailbox-only clock history survives restart and message retention."""
from types import SimpleNamespace

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
