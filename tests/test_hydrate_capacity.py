"""Restore paths must respect band capacity.

``hydrate_cms``, the torch ``load()`` round-trip and the legacy Hopfield
migration all append straight to ``band.entries`` — no ``band.store()``,
no ``max_entries`` check — and ``hydrate_cms`` additionally routes rows
whose band name no longer exists into ``bands[0]``. A preset rename could
therefore pile every row into the 200-slot ``working`` band.

That was survivable while capacity eviction deleted: the resident set
stayed tiny (~212 rows against 5,250 capacity on a realistic corpus).
Once eviction started demoting instead (2026-07-25) the resident set
reaches the summed capacity, so the same rename now lands thousands of
rows in one small band, and draining is one entry per subsequent
``store()`` — each eviction scoring the whole band and cascading a DB
UPDATE per hop.

The restore paths now rebalance once, shallow to deep, spilling each
band's lowest-scoring overflow into the next.

Deliberately NOT enforced: the deepest band may finish over capacity when
the bank holds more rows than the preset can seat. Startup is the wrong
place to destroy memories the user has not asked to lose; it is logged,
and runtime eviction drains it through the normal path.

Pure in-memory — a storage double supplies rows, so no Postgres needed.
"""

from __future__ import annotations

import time

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.storage.sync import hydrate_cms
from pseudolife_memory.utils.config import (
    MemoryConfig, MIRASBandSpec, MIRASConfig,
)


def _unit(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return F.normalize(torch.randn(1024, generator=g), dim=0)


def _row(i: int, band: str, surprise: float | None = None) -> dict:
    return {
        "id": i, "text": f"fact {i}", "embedding": _unit(i + 1).tolist(),
        "surprise": 0.5 if surprise is None else surprise,
        "ts": time.time(), "access_count": 0, "source": "t", "band": band,
        "superseded_at": None, "superseded_by_text": None,
        "last_logical_turn": None, "slots": [], "episode_id": None,
        "episode_title": None, "tags": [], "reinforcements": 0,
    }


class _Storage:
    def __init__(self, rows):
        self.rows = rows
        self.deleted: list[int] = []

    def load_entries(self):
        return list(self.rows)

    def load_episodes(self):
        return []

    def delete_entry_ids(self, ids):
        self.deleted.extend(ids)

    def update_entry(self, db_id, **fields):
        pass


def _cms(*caps: int) -> ContinuumMemorySystem:
    cfg = MemoryConfig()
    cfg.miras = MIRASConfig(preset="custom", bands=[
        MIRASBandSpec(name=chr(ord("a") + i), max_entries=c,
                      update_interval=10 ** 9, promotion_access_count=10 ** 9,
                      promotion_surprise=2.0)
        for i, c in enumerate(caps)
    ])
    return ContinuumMemorySystem(cfg)


def _sizes(cms) -> list[int]:
    return [b.size for b in cms.bands]


def test_hydrate_spills_overflow_into_deeper_bands():
    """The reported defect: every row lands in one small band."""
    cms = _cms(3, 4, 5)
    storage = _Storage([_row(i, "a") for i in range(10)])

    n = hydrate_cms(cms, storage)

    assert n == 10
    assert _sizes(cms) == [3, 4, 3], f"not rebalanced: {_sizes(cms)}"


def test_hydrate_respects_every_band_capacity():
    cms = _cms(3, 4, 5)
    hydrate_cms(cms, _Storage([_row(i, "a") for i in range(12)]))

    for band in cms.bands:
        assert band.size <= band.max_entries, f"{band.name} over capacity"


def test_hydrate_of_an_unknown_band_name_also_rebalances():
    """A preset rename routes rows to bands[0]; they must not stay piled."""
    cms = _cms(2, 3, 4)
    hydrate_cms(cms, _Storage([_row(i, "band-that-no-longer-exists")
                               for i in range(8)]))

    assert _sizes(cms) == [2, 3, 3]


def test_hydrate_loses_nothing_and_deletes_nothing():
    cms = _cms(3, 4, 5)
    storage = _Storage([_row(i, "a") for i in range(10)])

    hydrate_cms(cms, storage)

    texts = {e.text for b in cms.bands for e in b.entries}
    assert texts == {f"fact {i}" for i in range(10)}
    assert storage.deleted == []


def test_hydrate_keeps_the_highest_scoring_entries_shallowest():
    """Spilling picks victims by the band's own retention policy, so the
    entries that matter most stay in the fast tier."""
    cms = _cms(2, 5)
    rows = [_row(i, "a", surprise=i / 10.0) for i in range(6)]
    hydrate_cms(cms, _Storage(rows))

    shallow = {e.text for e in cms.bands[0].entries}
    assert shallow == {"fact 5", "fact 4"}, f"kept the wrong two: {shallow}"


def test_an_overfull_bank_is_not_silently_truncated_at_startup():
    """More rows than the preset can seat: the deepest band absorbs the
    remainder rather than hydrate destroying user memories on boot."""
    cms = _cms(2, 3)
    storage = _Storage([_row(i, "a") for i in range(12)])

    n = hydrate_cms(cms, storage)

    assert n == 12
    assert sum(_sizes(cms)) == 12
    assert storage.deleted == []


def test_load_state_dict_round_trip_rebalances(tmp_path):
    """``load()`` replaces band entries wholesale; a preset that shrank
    between save and load leaves a band over capacity. Total capacity here
    (3+6) still seats all 8, so nothing should end up over its cap."""
    big = _cms(10, 10)
    for i in range(8):
        big.bands[0].entries.append(MemoryEntry(
            text=f"fact {i}", embedding=_unit(i + 1),
            surprise_score=0.5, source="t", bank="a"))
    big.save(tmp_path)

    small = _cms(3, 6)
    small.load(tmp_path)

    for band in small.bands:
        assert band.size <= band.max_entries, f"{band.name} over capacity"
    assert sum(_sizes(small)) == 8
