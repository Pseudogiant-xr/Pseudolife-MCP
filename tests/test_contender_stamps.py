"""Contender temporal stamps: park carries the write's stamps; promote stamps.

A parked contender is a first-class record, so it must carry the hlc /
tx_time / valid_time / freshness_class its write arrived with — write_fact
computes all four and the contend path must not drop them. Promotion via
resolve(accept=True) is itself a transaction: it stamps tx_time with the
promotion time and takes a fresh HLC from the caller (the service owns the
clock) so the promoted fact can defend itself in _should_supersede. Without
that stamp a promoted fact reads as HLC (0,0) and any later write carrying a
stale, pre-promotion HLC silently supersedes the explicit resolution — the
same failure class as the 2026-07-02 _promote_slots fix. valid_time is never
moved by park-confirm or promotion: when a fact became true is not when it
was re-stated or accepted.

Store-level tests inject embeddings + time (mirrors test_cortex.py); the
service-level test constructs a real offline MemoryService (mirrors
test_cortex_contenders.py).
"""
from __future__ import annotations

import tempfile

import torch

from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.slots import Slot


def _unit(seed: int, dim: int = 8) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(dim, generator=g)
    return v / v.norm()


def _park_rust(s: CortexStore) -> None:
    """user 'go' current, agent 'rust' parked with full stamps."""
    s.write_fact(Slot("project", "language", "go"), _unit(1), support="user",
                 now=1000.0, hlc=(1000, 0))
    r = s.write_fact(Slot("project", "language", "rust"), _unit(2),
                     support="agent", now=2000.0, hlc=(2000, 0),
                     valid_time=1500.0, freshness_class="volatile")
    assert r.action == "contested"


def test_parked_contender_carries_write_stamps():
    s = CortexStore()
    _park_rust(s)
    c = s.contenders_for("project", "language")[0]
    assert (c.hlc_phys, c.hlc_logical) == (2000, 0)
    assert c.tx_time == 2000.0
    assert c.valid_time == 1500.0
    assert c.freshness_class == "volatile"


def test_currentless_park_carries_write_stamps():
    # The quarantine's empty-slot park (force_contend) is the routine
    # automated path (PR #126) — it must stamp identically.
    s = CortexStore()
    r = s.write_fact(Slot("daemon", "state", "v26 live"), _unit(3),
                     support="agent", now=2000.0, hlc=(2000, 0),
                     valid_time=1500.0, freshness_class="volatile",
                     force_contend=True)
    assert r.action == "contested"
    c = r.record
    assert (c.hlc_phys, c.hlc_logical) == (2000, 0)
    assert c.tx_time == 2000.0
    assert c.valid_time == 1500.0
    assert c.freshness_class == "volatile"


def test_occupied_slot_force_contend_park_carries_stamps():
    # force_contend against a standing current is the quarantine's PRIMARY
    # route (park/hold/hold_ordinary all hit it) — stamp it like the rest.
    s = CortexStore()
    s.write_fact(Slot("project", "language", "go"), _unit(1), support="user",
                 now=1000.0, hlc=(1000, 0))
    r = s.write_fact(Slot("project", "language", "rust"), _unit(2),
                     support="agent", now=2000.0, hlc=(2000, 0),
                     valid_time=1500.0, freshness_class="volatile",
                     force_contend=True)
    assert r.action == "contested"
    c = r.record
    assert (c.hlc_phys, c.hlc_logical) == (2000, 0)
    assert c.tx_time == 2000.0
    assert c.valid_time == 1500.0
    assert c.freshness_class == "volatile"


def test_aggregate_guard_park_carries_stamps():
    # The fourth _contend caller: add_member against a protected number-led
    # aggregate scalar parks the incoming member as a contender — it must
    # carry the add's HLC and times too, or resolve() later promotes a
    # record whose valid_time is permanently None.
    s = CortexStore()
    s.write_fact(Slot("garage", "bikes", "3 bikes total"), _unit(5),
                 support="user", now=1000.0, hlc=(1000, 0))
    r = s.add_member(Slot("garage", "bikes", "the red Ducati"), _unit(6),
                     support="agent", now=2000.0, hlc=(2000, 0))
    assert r.action == "contested"
    c = r.record
    assert (c.hlc_phys, c.hlc_logical) == (2000, 0)
    assert c.tx_time == 2000.0
    assert c.valid_time == 2000.0


def test_contender_reconfirm_advances_tx_and_hlc_not_valid_time():
    s = CortexStore()
    _park_rust(s)
    r = s.write_fact(Slot("project", "language", "rust"), _unit(2),
                     support="agent", now=3000.0, hlc=(3000, 0))
    assert r.action == "contested"          # confirmed the existing contender
    c = r.record
    assert (c.hlc_phys, c.hlc_logical) == (3000, 0)
    assert c.tx_time == 3000.0
    assert c.valid_time == 1500.0           # first-became-true never moves


def test_resolve_accept_stamps_promotion_time_and_hlc():
    s = CortexStore()
    _park_rust(s)
    res = s.resolve("project", "language", True, now=5000.0, hlc=(5000, 0))
    assert res is not None and res.action == "superseded"
    rec = s.lookup("project", "language")
    assert rec is not None and rec.value == "rust"
    assert rec.tx_time == 5000.0
    assert (rec.hlc_phys, rec.hlc_logical) == (5000, 0)
    assert rec.valid_time == 1500.0         # promotion is not when it held
    assert rec.freshness_class == "volatile"


def test_promoted_fact_defends_against_stale_hlc():
    # The teeth: after an explicit resolution at HLC (5000,0), a delayed
    # write replaying a PRE-promotion HLC must not walk over it — even at
    # user tier, where only the HLC gate stands between them.
    s = CortexStore()
    _park_rust(s)
    s.resolve("project", "language", True, now=5000.0, hlc=(5000, 0))
    r = s.write_fact(Slot("project", "language", "haskell"), _unit(4),
                     support="user", now=6000.0, hlc=(3000, 0))
    assert r.action == "contested"
    assert s.lookup("project", "language").value == "rust"


def test_resolve_without_hlc_keeps_parked_stamp():
    # Legacy/no-clock callers: the parked stamp stands (never regress to
    # None), and tx_time still records the promotion touch.
    s = CortexStore()
    _park_rust(s)
    s.resolve("project", "language", True, now=5000.0)
    rec = s.lookup("project", "language")
    assert (rec.hlc_phys, rec.hlc_logical) == (2000, 0)
    assert rec.tx_time == 5000.0


def test_service_resolve_ticks_hlc():
    # End-to-end: the service owns the clock, so cortex_resolve must hand
    # resolve() a fresh tick — the promoted fact carries an HLC strictly
    # after the parked one.
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        parked = svc._cortex.contenders_for("project", "language")[0]
        assert parked.hlc_phys, "park must carry the write's HLC"
        parked_hlc = (parked.hlc_phys, parked.hlc_logical)
        svc.cortex_resolve("project", "language", accept=True)
        rec = svc._cortex.lookup("project", "language")
        assert rec is not None and rec.value == "rust"
        assert (rec.hlc_phys, rec.hlc_logical) > parked_hlc
        assert rec.tx_time is not None
