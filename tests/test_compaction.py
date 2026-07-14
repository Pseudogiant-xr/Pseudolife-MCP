"""Superseded-row compaction (spec 2026-07-14).

Policy: per slot, pool non-live records (superseded/retired), keep the
newest ``keep_per_slot``, purge the rest when older than ``min_age_days``.
current/contested are never touched.
"""
from __future__ import annotations

import torch

from pseudolife_memory.memory.compaction import compact_store
from pseudolife_memory.memory.cortex import CortexStore, CortexRecord
from pseudolife_memory.memory.lessons import LessonStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.memory.world_cortex import WorldCortexStore

EMB = torch.zeros(8)
T0 = 1_000_000.0
DAY = 86400.0


def _facts_store(n_versions: int = 6) -> CortexStore:
    """One slot, n_versions successive user-tier values -> 1 current +
    (n_versions - 1) superseded, superseded_at = T0+10 ... T0+(n-1)*10."""
    s = CortexStore()
    for i in range(n_versions):
        s.write_fact(Slot("proj", "version", f"v{i}"), EMB,
                     support="user", now=T0 + i * 10)
    return s


def _non_live(store):
    return [r for r in store.records if r.status in ("superseded", "retired")]


# ── policy ──────────────────────────────────────────────────────────────

def test_keeps_newest_n_purges_older():
    s = _facts_store(6)                       # 5 superseded
    n = compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 3
    kept = sorted(r.value for r in _non_live(s))
    assert kept == ["v3", "v4"]               # the two newest priors
    assert s.lookup("proj", "version").value == "v5"   # current untouched


def test_min_age_guard_blocks_recent_purge():
    s = _facts_store(6)
    # Everything superseded within the last day -> nothing qualifies.
    n = compact_store(s, keep_per_slot=0, min_age_days=1.0, now=T0 + 100)
    assert n == 0
    assert len(_non_live(s)) == 5


def test_min_age_partial_window():
    s = _facts_store(6)
    # now such that only superseded_at <= T0+30 are older than 1 day:
    now = T0 + 30 + DAY + 1
    n = compact_store(s, keep_per_slot=0, min_age_days=1.0, now=now)
    assert n == 3                              # v0(T0+10), v1(+20), v2(+30)
    assert sorted(r.value for r in _non_live(s)) == ["v3", "v4"]


def test_never_touches_current_or_contested():
    s = CortexStore()
    s.write_fact(Slot("proj", "owner", "alice"), EMB, support="user", now=T0)
    s.write_fact(Slot("proj", "owner", "bob"), EMB, support="user", now=T0 + 10)
    # Weaker tier -> parked as contender (status='contested').
    s.write_fact(Slot("proj", "owner", "carol"), EMB, support="agent", now=T0 + 20)
    n = compact_store(s, keep_per_slot=0, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 1                              # only the superseded "alice"
    assert s.lookup("proj", "owner").value == "bob"
    assert [r.value for r in s.contenders_for("proj", "owner")] == ["carol"]


def test_retired_records_pool_with_superseded():
    s = CortexStore()
    s.write_fact(Slot("proj", "owner", "alice"), EMB, support="user", now=T0)
    s.write_fact(Slot("proj", "owner", "carol"), EMB, support="agent", now=T0 + 10)
    s.resolve("proj", "owner", accept=False, now=T0 + 20)   # -> retired
    assert any(r.status == "retired" for r in s.records)
    n = compact_store(s, keep_per_slot=0, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 1
    assert all(r.status != "retired" for r in s.records)


def test_legacy_none_superseded_at_purged_first():
    s = _facts_store(3)                        # superseded at T0+10, T0+20
    legacy = CortexRecord(entity="proj", attribute="version", value="v-legacy",
                          status="superseded", asserted_at=0.0,
                          superseded_at=None)
    s.records.insert(0, legacy)
    s._reindex_current()
    n = compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 1
    assert all(r.value != "v-legacy" for r in s.records)


def test_tie_break_is_deterministic_by_insertion_order():
    s = CortexStore()
    for v in ("a", "b", "c"):
        s.records.append(CortexRecord(
            entity="proj", attribute="version", value=v,
            status="superseded", asserted_at=T0, superseded_at=T0 + 10))
    s._reindex_current()
    n = compact_store(s, keep_per_slot=1, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 2
    # Identical timestamps: the LAST-inserted record is newest.
    assert [r.value for r in _non_live(s)] == ["c"]


def test_keep_zero_and_negative_inputs_clamped():
    s = _facts_store(4)
    n = compact_store(s, keep_per_slot=-5, min_age_days=-1.0, now=T0 + 10 * DAY)
    assert n == 3                              # keep clamps to 0, age to 0
    assert _non_live(s) == []


# ── invariants ──────────────────────────────────────────────────────────

def test_current_index_rebuilt_and_lookup_survives():
    s = _facts_store(6)
    s.write_fact(Slot("proj", "license", "MIT"), EMB, support="user", now=T0)
    compact_store(s, keep_per_slot=1, min_age_days=0.0, now=T0 + 10 * DAY)
    assert s.lookup("proj", "version").value == "v5"
    assert s.lookup("proj", "license").value == "MIT"


def test_purged_slots_marked_dirty():
    s = _facts_store(6)
    s.dirty_slots.clear()
    compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    assert ("proj", "version") in s.dirty_slots


def test_untouched_slots_not_marked_dirty():
    s = _facts_store(6)
    s.write_fact(Slot("proj", "license", "MIT"), EMB, support="user", now=T0)
    s.dirty_slots.clear()
    compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    assert ("proj", "license") not in s.dirty_slots


def test_survivor_order_preserved():
    s = _facts_store(6)
    before = [r.value for r in s.records]
    compact_store(s, keep_per_slot=2, min_age_days=0.0, now=T0 + 10 * DAY)
    after = [r.value for r in s.records]
    assert after == [v for v in before if v in set(after)]


# ── other store types ───────────────────────────────────────────────────

def test_world_store_compaction():
    w = WorldCortexStore()
    for i in range(4):
        w.write_fact("pkg", "version", f"{i}.0", None, now=T0 + i * 10)
    n = compact_store(w, keep_per_slot=1, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 2
    assert w.lookup("pkg", "version").value == "3.0"
    assert [r.value for r in _non_live(w)] == ["2.0"]
    assert ("pkg", "version") in w.dirty_slots


def test_lesson_store_compaction():
    ls = LessonStore()
    for i in range(4):
        ls.write_fact("deploy", "pitfall", f"lesson {i}", None, now=T0 + i * 10)
    n = compact_store(ls, keep_per_slot=1, min_age_days=0.0, now=T0 + 10 * DAY)
    assert n == 2
    assert ls.lookup("deploy", "pitfall").value == "lesson 3"
    assert ("deploy", "pitfall") in ls.dirty_slots
