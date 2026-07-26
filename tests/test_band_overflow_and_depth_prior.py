"""Two measured defects in the band machinery (2026-07-25 audit).

**Overflow destroyed data while the store was near-empty.** Capacity
eviction deleted the entry and its storage row, but the only way out of a
band is promotion (``access_count >= N or surprise > theta``), so an
unsurprising, never-retrieved entry died in the 200-slot ``working`` band
while the other seven held ~5,050 free slots. Measured on the LongMemEval
``s`` replay: 31.1% of stored turns discarded at **6.4%** total capacity
utilisation, with ``working`` saturated in 78/78 questions and ``forever``
empty in all of them. Evidence turns fared *worse* than average (37.5%
evicted vs a 31.1% base rate) because eviction ranks on novelty and
knowledge-update evidence is a restatement, hence unsurprising. A band
must hand its evictee to the next band; only the deepest band may drop,
which is what makes total capacity the real bound.

**Band depth was used as a proxy for age.** The retrieval recency boost
ramps ``0.4 -> 0.0`` over band depth, but depth is set by promotion
history — which, absent retrieval, is driven by surprise, not age. So the
prior keys on a variable that does not track recency and can invert
similarity ordering. Off by default now; ``recency_boost_enabled`` opts
back in.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import (
    MemoryConfig, MIRASBandSpec, MIRASConfig,
)


def _unit(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return F.normalize(torch.randn(384, generator=g), dim=0)


def _cfg(*caps: int) -> MemoryConfig:
    """Bands with the given capacities and consolidation switched off, so a
    test observes eviction alone rather than eviction racing promotion."""
    cfg = MemoryConfig()
    cfg.miras = MIRASConfig(
        preset="custom",
        bands=[
            MIRASBandSpec(
                name=chr(ord("a") + i), max_entries=c,
                update_interval=10 ** 9,          # destination never fires
                promotion_access_count=10 ** 9,   # nothing promotes
                promotion_surprise=1.0,           # surprise > 1.0 is never true
                retention_policy="balanced",
            )
            for i, c in enumerate(caps)
        ],
    )
    return cfg


class _Storage:
    """Minimal storage double recording the calls eviction makes."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.deleted: list[int] = []
        self.updates: list[tuple[int, dict]] = []
        self._next = 1

    def insert_entry(self, row):
        db_id, self._next = self._next, self._next + 1
        self.rows[db_id] = dict(row)
        return db_id

    def update_entry(self, db_id, **fields):
        self.updates.append((db_id, fields))
        self.rows.get(db_id, {}).update(fields)

    def delete_entry_ids(self, ids):
        self.deleted.extend(ids)
        for i in ids:
            self.rows.pop(i, None)


def test_overflow_demotes_to_the_next_band_instead_of_dropping():
    """The whole 31.1%-loss-at-6.4%-occupancy defect in one assertion."""
    cms = ContinuumMemorySystem(_cfg(2, 3))
    for i in range(3):
        cms.store(f"e{i}", _unit(i + 1), source="t")

    sizes = [b.size for b in cms.bands]
    assert sum(sizes) == 3, f"an entry was destroyed: {sizes}"
    assert sizes == [2, 1]


def test_demotion_relocates_the_storage_row_rather_than_deleting_it():
    """Demotion is a relocation, like promotion — the row moves bands."""
    cms = ContinuumMemorySystem(_cfg(2, 3))
    cms.storage = _Storage()
    for i in range(3):
        cms.store(f"e{i}", _unit(i + 1), source="t")

    assert cms.storage.deleted == []
    assert ("b" in [f.get("band") for _, f in cms.storage.updates]), \
        f"no row was moved to band b: {cms.storage.updates}"


def test_demotion_preserves_the_entry_it_moves():
    """A demoted entry keeps its identity — text, provenance, db row."""
    cms = ContinuumMemorySystem(_cfg(1, 2))
    cms.storage = _Storage()
    cms.store("first", _unit(1), source="orig")
    first_id = cms.bands[0].entries[0].db_id
    cms.store("second", _unit(2), source="t")

    moved = cms.bands[1].entries[0]
    assert moved.text == "first"
    assert moved.source == "orig"
    assert moved.db_id == first_id


def test_demotion_carries_the_reinforcement_count():
    """``reinforcements`` feeds the MTT retention term that is supposed to
    make a reinforced entry resist eviction
    (``retention_boost * log1p(reinforcements)``, protocols.py). Losing it
    on the move would make ``memory_reinforce`` a no-op for retention on
    the daemon, which ships ``retention_boost=1.0``."""
    cms = ContinuumMemorySystem(_cfg(1, 2))
    cms.store("first", _unit(1), source="t")
    cms.bands[0].entries[0].reinforcements = 12
    cms.store("second", _unit(2), source="t")

    assert cms.bands[1].entries[0].reinforcements == 12


def test_a_storage_failure_during_demotion_does_not_lose_the_write():
    """The delete path this replaced logged and carried on. Eviction runs
    *before* the append (band.py), so letting the storage error escape
    aborts ``store`` and drops the incoming memory entirely."""
    class _Flaky(_Storage):
        def update_entry(self, db_id, **fields):
            raise ConnectionError("server closed the connection unexpectedly")

    cms = ContinuumMemorySystem(_cfg(1, 2))
    cms.storage = _Flaky()
    cms.store("first", _unit(1), source="t")
    cms.store("second", _unit(2), source="t")

    texts = {e.text for b in cms.bands for e in b.entries}
    assert texts == {"first", "second"}


def test_the_deepest_band_still_drops_so_capacity_stays_bounded():
    """Total capacity must remain a real bound, not become unbounded growth."""
    cms = ContinuumMemorySystem(_cfg(1, 1))
    cms.storage = _Storage()
    for i in range(4):
        cms.store(f"e{i}", _unit(i + 1), source="t")

    assert sum(b.size for b in cms.bands) == 2
    assert len(cms.storage.deleted) == 2


def test_band_depth_does_not_outrank_similarity():
    """The depth ramp could make a weaker match in band 0 beat a stronger
    match in a deeper band. Similarity must decide."""
    cms = ContinuumMemorySystem(_cfg(10, 10))
    q = _unit(1)
    far = F.normalize(q + 0.9 * _unit(2), dim=0)    # cos ~0.74
    close = F.normalize(q + 0.15 * _unit(3), dim=0)  # cos ~0.99
    cms.bands[0].store("far-but-shallow", far, source="t", surprise=1.0)
    cms.bands[1].store("close-but-deep", close, source="t", surprise=1.0)

    result = cms.retrieve(q, top_k=2)

    assert result.entries[0].text == "close-but-deep"


def test_depth_recency_prior_can_be_re_enabled():
    """Neutralised by default, not deleted — the opt-in still works."""
    cfg = _cfg(10, 10)
    cfg.recency_boost_enabled = True
    cms = ContinuumMemorySystem(cfg)
    q = _unit(1)
    far = F.normalize(q + 0.9 * _unit(2), dim=0)
    close = F.normalize(q + 0.15 * _unit(3), dim=0)
    cms.bands[0].store("far-but-shallow", far, source="t", surprise=1.0)
    cms.bands[1].store("close-but-deep", close, source="t", surprise=1.0)

    result = cms.retrieve(q, top_k=2)

    assert result.entries[0].text == "far-but-shallow"
