"""File-mode snapshot round-trips the v11 temporal stamp + freshness_class.

CortexStore.save()/load() is the persistence path when MemoryService runs
without a storage backend (file mode). The PG path (storage/sync.py
_stamp_to_row/_stamp_from_row) round-trips the v11 stamp fields; the .pt
snapshot must too, or every file-mode restart strips every fact's HLC —
records reload as (0,0) in _should_supersede and freshness resets to
evergreen, reopening the failure class fixed for contenders (see
test_contender_stamps.py). Legacy snapshots written before this fix carry
none of the keys and must load with the dataclass defaults, not KeyError.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import torch

from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.slots import Slot


def _unit(seed: int, dim: int = 8) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(dim, generator=g)
    return v / v.norm()


STAMP_FIELDS = ("tx_time", "valid_time", "hlc_phys", "hlc_logical",
                "writer_id", "session_id", "version", "freshness_class")


def _stamped_store() -> CortexStore:
    """Current fact with full stamps, plus a stamped parked contender."""
    s = CortexStore()
    r = s.write_fact(Slot("project", "language", "go"), _unit(1),
                     support="user", now=1000.0, hlc=(1000, 0),
                     valid_time=900.0, freshness_class="volatile",
                     writer_id="w1", session_id="sess-a")
    assert r.action == "inserted"
    r2 = s.write_fact(Slot("project", "language", "rust"), _unit(2),
                      support="agent", now=2000.0, hlc=(2000, 0),
                      valid_time=1500.0, freshness_class="volatile",
                      writer_id="w2", session_id="sess-b")
    assert r2.action == "contested"
    return s


def _roundtrip(store: CortexStore) -> CortexStore:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cortex_state.pt"
        store.save(path)
        loaded = CortexStore()
        loaded.load(path)
    return loaded


def test_snapshot_roundtrip_preserves_stamps_and_freshness():
    loaded = _roundtrip(_stamped_store())

    cur = loaded.lookup("project", "language")
    assert cur is not None and cur.value == "go"
    assert (cur.hlc_phys, cur.hlc_logical) == (1000, 0)
    assert cur.tx_time == 1000.0
    assert cur.valid_time == 900.0
    assert cur.writer_id == "w1"
    assert cur.session_id == "sess-a"
    assert cur.version == 1
    assert cur.freshness_class == "volatile"

    parked = loaded.contenders_for("project", "language")
    assert parked, "contested record must survive the snapshot"
    c = parked[0]
    assert (c.hlc_phys, c.hlc_logical) == (2000, 0)
    assert c.tx_time == 2000.0
    assert c.valid_time == 1500.0
    assert c.writer_id == "w2"
    assert c.session_id == "sess-b"
    assert c.freshness_class == "volatile"


def test_reloaded_fact_defends_against_stale_hlc():
    # The teeth: after a restart, a delayed write replaying a PRE-restart
    # HLC must not walk over the standing fact. Before the fix the reload
    # dropped the stamp, the current read as (0,0), and the stale write won.
    loaded = _roundtrip(_stamped_store())
    r = loaded.write_fact(Slot("project", "language", "haskell"), _unit(3),
                          support="user", now=3000.0, hlc=(500, 0))
    assert r.action == "contested"
    assert loaded.lookup("project", "language").value == "go"


def test_legacy_snapshot_without_stamp_keys_loads_defaults():
    # A pre-fix .pt has none of the stamp keys — load with the dataclass
    # defaults (None / version 1 / evergreen), never KeyError.
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cortex_state.pt"
        store = CortexStore()
        store.write_fact(Slot("user", "city", "Sydney"), _unit(4), now=1.0)
        store.save(path)
        state = torch.load(str(path), weights_only=True)
        for rec in state["records"]:
            for k in STAMP_FIELDS:
                rec.pop(k, None)
        torch.save(state, str(path))

        loaded = CortexStore()
        loaded.load(path)
    cur = loaded.lookup("user", "city")
    assert cur is not None
    assert cur.tx_time is None and cur.valid_time is None
    assert cur.hlc_phys is None and cur.hlc_logical is None
    assert cur.writer_id is None and cur.session_id is None
    assert cur.version == 1
    assert cur.freshness_class == "evergreen"
