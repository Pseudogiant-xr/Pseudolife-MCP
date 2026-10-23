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
    v = torch.randn(1024, generator=g)
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


def _write_legacy_weights(cms, directory) -> None:
    """A weights.pt exactly as pre-v57 ``save_weights`` wrote it (counters
    only, no MLP weights since v0.5): the file the v57 import reads."""
    atomic_torch_save({
        "schema_version": 9, "kind": "weights",
        "preset_name": cms.config.miras.preset,
        "interaction_count": cms._interaction_count,
        "logical_turn_count": cms._logical_turn_count,
        "surprise_history": cms._surprise_history,
        "consolidation_events": cms._consolidation_events,
        "tier_hits": cms._tier_hits,
        "tier_queries": cms._tier_queries,
    }, directory / "weights.pt")


def test_cms_weights_roundtrip_counters_only(tmp_path):
    """A pre-v57 weights.pt carries only counters (no MLP weights — bands
    are cosine stores). Counters load; band entries are never touched."""
    cms = ContinuumMemorySystem(MemoryConfig())
    cms.store("a fact", _emb(1), source="t")
    cms.store("another fact", _emb(2), source="t")
    _write_legacy_weights(cms, tmp_path)

    cms2 = ContinuumMemorySystem(MemoryConfig())
    # Hydration happens before weights load in storage mode — entries
    # already in the bands must survive load_weights untouched.
    cms2.bands[0].store("hydrated entry", _emb(3), source="t")
    ok = cms2.load_weights(tmp_path)
    assert ok is True and cms2.weights_reset is False
    assert cms2._interaction_count == cms._interaction_count  # counters restored
    assert cms2.counters_state() == cms.counters_state()
    assert [e.text for e in cms2.bands[0].entries] == ["hydrated entry"]


def test_cms_weights_corrupt_sets_flag(tmp_path):
    cms = ContinuumMemorySystem(MemoryConfig())
    cms.store("fact", _emb(4), source="t")
    _write_legacy_weights(cms, tmp_path)
    (tmp_path / "weights.pt").write_bytes(b"junk")
    cms2 = ContinuumMemorySystem(MemoryConfig())
    ok = cms2.load_weights(tmp_path)  # .bak doesn't exist -> corrupt
    assert ok is False and cms2.weights_reset is True
    assert cms2.stats()["weights_reset"] is True


def test_counters_state_round_trips_and_merges_per_band(tmp_path):
    """``apply_counters`` merges the per-band maps into the current bands'
    entries: a band the state does not name keeps its zero, as
    ``load_weights`` always did. The state is a copy: a later store does
    not change one already taken."""
    cms = ContinuumMemorySystem(MemoryConfig())
    cms.store("a fact", _emb(5), source="t")
    state = cms.counters_state()
    before = {k: (dict(v) if isinstance(v, dict) else v) for k, v in state.items()}
    cms.store("another fact", _emb(6), source="t")
    assert state["interaction_count"] == before["interaction_count"]
    assert state["surprise_history"] == before["surprise_history"]

    band = cms.bands[0].name
    partial = dict(state, tier_hits={}, surprise_history={})
    cms2 = ContinuumMemorySystem(MemoryConfig())
    cms2.apply_counters(partial)
    assert cms2._interaction_count == state["interaction_count"]
    assert cms2._tier_hits[band] == 0 and cms2._surprise_history[band] == []


def test_counters_state_is_plain_json_even_from_a_legacy_payload():
    """An old weights.pt may hold tensor scalars; the state written to the
    jsonb columns is plain numbers, so the write cannot refuse it."""
    import json

    cms = ContinuumMemorySystem(MemoryConfig())
    band = cms.bands[0].name
    cms.apply_counters({
        "interaction_count": torch.tensor(3), "logical_turn_count": 1,
        "tier_queries": 2, "tier_hits": {band: torch.tensor(2)},
        "surprise_history": {band: [torch.tensor(0.5)]},
        "consolidation_events": [{"timestamp": torch.tensor(1.5)}],
    })
    state = cms.counters_state()
    assert json.loads(json.dumps(state)) == state
    assert state["surprise_history"][band] == [0.5]
    assert state["consolidation_events"] == [{"timestamp": 1.5}]
