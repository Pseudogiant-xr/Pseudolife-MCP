"""Band movement must not leave an entry in two bands at once.

``_consolidate`` promotes entries in a loop but pruned the source only
*after* it finished (``source.entries = remaining``). A raise partway
through therefore left every already-moved entry in BOTH the source and
the destination, sharing one ``db_id``. Retrieval hides this (dedup is by
``entry.text``), but ``memory_stats`` over-counts and the next
``_consolidate`` relocates the source copy again onto the same row.

Found by review on 2026-07-25 with a ``ConnectionError`` armed on
``storage.update_entry``. That specific trigger is gone — the
write-through is wrapped now — but ``destination.store`` can still raise,
and ``_relocate`` became a hot path when capacity eviction started
routing through it, so the window is worth closing rather than narrowing.

Two invariants, tested separately because they fail independently:
``_relocate`` is all-or-nothing, and ``_consolidate`` prunes whatever it
actually moved even on the way out.
"""

from __future__ import annotations

import pytest
import torch
import torch.nn.functional as F

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import (
    MemoryConfig, MIRASBandSpec, MIRASConfig,
)


def _unit(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return F.normalize(torch.randn(1024, generator=g), dim=0)


def _cfg() -> MemoryConfig:
    """Two roomy bands where everything qualifies for promotion, so a
    ``_consolidate`` call moves several entries and a mid-loop failure has
    something to strand."""
    cfg = MemoryConfig()
    cfg.surprise_threshold = -1.0          # store everything
    cfg.miras = MIRASConfig(preset="custom", bands=[
        MIRASBandSpec(name="head", max_entries=50, update_interval=10 ** 9,
                      promotion_access_count=10 ** 9, promotion_surprise=0.0),
        MIRASBandSpec(name="tail", max_entries=50, update_interval=10 ** 9,
                      promotion_access_count=10 ** 9, promotion_surprise=0.0),
    ])
    return cfg


def _seeded(n: int = 4) -> ContinuumMemorySystem:
    cms = ContinuumMemorySystem(_cfg())
    for i in range(n):
        cms.store(f"fact {i}", _unit(i + 1), source="t")
    assert cms.bands[0].size == n, "setup: entries should still be in head"
    return cms


def _all_texts(cms: ContinuumMemorySystem) -> list[str]:
    return [e.text for b in cms.bands for e in b.entries]


def _arm_failure_on_nth_store(band, n: int) -> None:
    """Make ``band.store`` raise on its n-th call (1-indexed)."""
    real, calls = band.store, {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            raise RuntimeError("simulated allocation failure")
        return real(*args, **kwargs)

    band.store = flaky


def test_a_failed_consolidation_leaves_no_entry_in_two_bands():
    """The reported defect: moved entries stranded in the source too."""
    cms = _seeded(4)
    _arm_failure_on_nth_store(cms.bands[1], 3)

    with pytest.raises(RuntimeError):
        cms._consolidate(0, 1)

    texts = _all_texts(cms)
    assert len(texts) == len(set(texts)), f"entry in two bands: {sorted(texts)}"


def test_a_failed_consolidation_loses_nothing():
    """Pruning the source must not overshoot into data loss either."""
    cms = _seeded(4)
    _arm_failure_on_nth_store(cms.bands[1], 3)

    with pytest.raises(RuntimeError):
        cms._consolidate(0, 1)

    assert set(_all_texts(cms)) == {f"fact {i}" for i in range(4)}


def test_stats_do_not_overcount_after_a_failed_consolidation():
    """``memory_stats`` reads band sizes, so a duplicate inflates it."""
    cms = _seeded(4)
    _arm_failure_on_nth_store(cms.bands[1], 3)

    with pytest.raises(RuntimeError):
        cms._consolidate(0, 1)

    assert cms.stats()["total_memories"] == 4


def test_relocate_rolls_back_a_partial_move():
    """``_relocate`` is the shared move primitive for promotion AND for
    capacity demotion, and both callers prune the source on the strength of
    it returning — so a half-applied move is a duplicate.

    The reachable post-append failure is in carrying provenance across
    (``band.store`` itself cannot raise after its append: only attribute
    assignment follows). A malformed ``slots`` value gets us there without
    monkeypatching the method under test."""
    cms = _seeded(1)
    entry = cms.bands[0].entries[0]
    entry.slots = None  # list(None) raises inside the identity copy

    with pytest.raises(TypeError):
        cms._relocate(entry, cms.bands[1])

    assert cms.bands[1].size == 0, "destination kept a half-applied entry"
    assert [e.text for e in cms.bands[0].entries] == ["fact 0"]
