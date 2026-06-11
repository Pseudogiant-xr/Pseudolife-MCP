"""Atomic weights persistence: tmp+rename, .bak rotation, corrupt recovery."""

from __future__ import annotations

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.atomic_io import (
    WeightsCorrupt,
    atomic_torch_save,
    load_with_backup,
)
from pseudolife_memory.utils.config import MemoryConfig


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(384, generator=g)
    return v / v.norm()


def test_atomic_save_rotates_backup(tmp_path):
    p = tmp_path / "weights.pt"
    atomic_torch_save({"v": 1}, p)
    assert p.exists() and not p.with_suffix(".pt.bak").exists()
    atomic_torch_save({"v": 2}, p)
    bak = p.with_suffix(".pt.bak")
    assert bak.exists()
    assert torch.load(p, weights_only=True)["v"] == 2
    assert torch.load(bak, weights_only=True)["v"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_backup_recovery_on_corrupt_primary(tmp_path):
    p = tmp_path / "weights.pt"
    atomic_torch_save({"v": 1}, p)
    atomic_torch_save({"v": 2}, p)
    p.write_bytes(b"corrupted garbage")
    obj, used_backup = load_with_backup(p)
    assert used_backup is True and obj["v"] == 1


def test_both_corrupt_raises(tmp_path):
    p = tmp_path / "weights.pt"
    atomic_torch_save({"v": 1}, p)
    atomic_torch_save({"v": 2}, p)
    p.write_bytes(b"garbage")
    p.with_suffix(".pt.bak").write_bytes(b"garbage too")
    try:
        load_with_backup(p)
        raise AssertionError("expected WeightsCorrupt")
    except WeightsCorrupt:
        pass


def test_cms_weights_roundtrip_without_entries(tmp_path):
    cms = ContinuumMemorySystem(MemoryConfig())
    cms.store("a fact to train on", _emb(1), source="t")
    cms.store("another fact", _emb(2), source="t")
    assert cms.bands[0].update_count >= 1
    cms.save_weights(tmp_path)

    cms2 = ContinuumMemorySystem(MemoryConfig())
    # Hydration happens before weights load in storage mode — entries
    # already in the bands must survive load_weights untouched.
    cms2.bands[0].store("hydrated entry", _emb(3), source="t")
    ok = cms2.load_weights(tmp_path)
    assert ok is True and cms2.weights_reset is False
    assert cms2.bands[0].update_count == cms.bands[0].update_count
    assert [e.text for e in cms2.bands[0].entries] == ["hydrated entry"]


def test_cms_weights_corrupt_sets_flag(tmp_path):
    cms = ContinuumMemorySystem(MemoryConfig())
    cms.store("fact", _emb(4), source="t")
    cms.save_weights(tmp_path)
    (tmp_path / "weights.pt").write_bytes(b"junk")
    cms2 = ContinuumMemorySystem(MemoryConfig())
    ok = cms2.load_weights(tmp_path)  # .bak doesn't exist -> corrupt
    assert ok is False and cms2.weights_reset is True
    assert cms2.bands[0].update_count == 0  # fresh init
    assert cms2.stats()["weights_reset"] is True
