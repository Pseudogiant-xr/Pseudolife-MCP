"""Task 2: CortexStore member model — set-valued slots.

A slot can hold either one scalar current record (the existing model) or
many "member" current records (this feature) — never both at once. A scalar
occupying a slot is converted one-way to a member the first time
``add_member`` targets that slot (the scalar row survives as an audit-visible
superseded record, ``superseded_by_value == "(converted to set)"``; there is
no path back from set to scalar).

Dedup at a set slot is confirm-or-insert, never contest: see
``CortexStore.add_member``'s docstring for the v1 decision this pins down.

Embeddings are injected the same way ``tests/test_cortex.py`` does — a
deterministic unit vector per input, so these run without a
sentence-transformer. Unlike that file's single-seed helper, ``emb(text)``
here is keyed off the *normalised text itself*, so that near-duplicate
strings ("road bike" / "Road Bike") land on the identical vector (exercising
both the norm-equality and the cosine-dedup branches), while distinct
strings land — with overwhelming probability at 64 dimensions — on
low-cosine vectors, so the 100-distinct-tags cap test doesn't spuriously
dedup.
"""
from __future__ import annotations

import zlib

import pytest
import torch

from pseudolife_memory.memory.cortex import (
    CortexRecord,
    CortexStore,
    MAX_CURRENT_MEMBERS,
    MEMBER_DEDUP_COSINE,
)
from pseudolife_memory.memory.slots import Slot


def _unit_for(text: str, dim: int = 64) -> torch.Tensor:
    seed = zlib.crc32((text or "").strip().casefold().encode("utf-8"))
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(dim, generator=g)
    return v / v.norm()


@pytest.fixture
def store() -> CortexStore:
    return CortexStore()


@pytest.fixture
def emb():
    return _unit_for


def test_module_constants():
    assert MEMBER_DEDUP_COSINE == 0.9
    assert MAX_CURRENT_MEMBERS == 100


def test_add_member_creates_set_slot(store, emb):
    r = store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    assert r.action == "member_added" and r.record.kind == "member"
    assert store.slot_kind("user", "bikes owned") == "set"
    assert [m.value for m in store.members("user", "bikes owned")] == ["road bike"]
    # Item 7: _insert_member stamps tx_time/valid_time like _insert does —
    # a member row must not be a temporal blind spot.
    assert r.record.tx_time is not None
    assert r.record.valid_time is not None


def test_add_member_dedup_confirms_not_duplicates(store, emb):
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    r = store.add_member(Slot("user", "bikes owned", "Road Bike"), emb("Road Bike"))
    assert r.action == "member_confirmed"
    assert len(store.members("user", "bikes owned")) == 1


def test_remove_member_keeps_audit_row(store, emb):
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    r = store.remove_member("user", "bikes owned", "road bike")
    assert r.action == "member_removed" and r.record.status == "removed"
    assert store.members("user", "bikes owned") == []
    removed = store.members("user", "bikes owned", include_removed=True)
    assert [m.value for m in removed] == ["road bike"]
    assert removed[0].superseded_at is not None


def test_remove_member_not_found(store, emb):
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    r = store.remove_member("user", "bikes owned", "unicycle")
    assert r.action == "member_not_found"
    assert len(store.members("user", "bikes owned")) == 1


def test_member_add_converts_scalar_slot_with_audit(store, emb):
    store.write_fact(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    r = store.add_member(Slot("user", "bikes owned", "gravel bike"), emb("gravel bike"))
    assert r.action == "member_added"
    assert store.slot_kind("user", "bikes owned") == "set"
    vals = sorted(m.value for m in store.members("user", "bikes owned"))
    assert vals == ["gravel bike", "road bike"]      # scalar re-minted as member
    audit = [x for x in store.records
             if x.status == "superseded" and x.superseded_by_value == "(converted to set)"]
    assert len(audit) == 1


def test_member_cap_drops_beyond_100(store, emb):
    for i in range(100):
        store.add_member(Slot("user", "tags", f"tag-{i:03d}"), emb(f"tag-{i:03d}"))
    store.dirty_slots.clear()
    r = store.add_member(Slot("user", "tags", "tag-overflow"), emb("tag-overflow"))
    assert r.action == "member_capped"
    assert len(store.members("user", "tags")) == 100
    # Item 6: the capped result carries the OFFENDING value, not an unrelated
    # existing member, and is not itself a persisted record.
    assert r.record.value == "tag-overflow"
    assert r.record not in store.records
    # A rejected add must not schedule a slot rewrite — nothing changed.
    assert ("user", "tags") not in store.dirty_slots


def test_removed_member_can_rejoin(store, emb):
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    store.remove_member("user", "bikes owned", "road bike")
    r = store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    assert r.action == "member_added"
    assert len(store.members("user", "bikes owned")) == 1
    assert len(store.members("user", "bikes owned", include_removed=True)) == 2


def test_slot_kind_none_for_unknown_slot(store, emb):
    assert store.slot_kind("user", "nope") is None


def test_slot_kind_scalar_for_plain_fact(store, emb):
    store.write_fact(Slot("user", "city", "Sydney"), emb("Sydney"))
    assert store.slot_kind("user", "city") == "scalar"


# --- Binding requirements added beyond the brief (Task 1 review findings) --

def test_add_member_rejects_empty_value(store, emb):
    """value_norm invariant: Postgres treats NULLs as distinct in a unique
    index, so a member row with an empty/NULL normalised value would bypass
    ``facts_member_current_uq`` entirely (Task 1 review finding). A member
    add whose value normalises to empty is rejected, not stored."""
    r = store.add_member(Slot("user", "bikes owned", "   "), emb("   "))
    assert r.action == "member_invalid"
    assert store.slot_kind("user", "bikes owned") is None
    assert store.members("user", "bikes owned") == []


def test_add_member_never_contests(store, emb):
    """v1 decision: member records never take status='contested'. A second,
    differing member add at the same slot inserts a second current member —
    it does not park a contender the way scalar write_fact does."""
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    r = store.add_member(Slot("user", "bikes owned", "gravel bike"), emb("gravel bike"))
    assert r.action == "member_added"
    assert r.record.status == "current"
    members = store.members("user", "bikes owned")
    assert len(members) == 2
    assert all(m.status == "current" for m in members)
    assert store.contenders_for("user", "bikes owned") == []


# --- Fix wave (coordinator review) -----------------------------------------

def test_scalar_to_member_conversion_preserves_polarity(store, emb):
    """Item 1: the scalar->member conversion built a bare ``Slot(...)`` with
    no polarity, so a negated fact ("no longer have a road bike") silently
    re-minted as an affirmative member. The converted member must carry the
    original scalar's polarity."""
    store.write_fact(Slot("user", "bikes owned", "road bike", "-"), emb("road bike"))
    store.add_member(Slot("user", "bikes owned", "gravel bike"), emb("gravel bike"))
    converted = next(m for m in store.members("user", "bikes owned")
                     if m.value == "road bike")
    assert converted.polarity == "-"


def test_write_fact_rejects_scalar_write_on_set_slot(store, emb):
    """Item 2: once a slot holds current members, write_fact must not insert
    a parallel current scalar at the same key — the slot models are mutually
    exclusive. Callers (the service layer) are expected to catch this and
    route through add_member/remove_member instead."""
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    with pytest.raises(ValueError, match="holds a set"):
        store.write_fact(Slot("user", "bikes owned", "hybrid bike"), emb("hybrid bike"))
    # The rejected write must not have mutated anything.
    assert [m.value for m in store.members("user", "bikes owned")] == ["road bike"]


def test_write_fact_converted_slot_also_rejects_scalar(store, emb):
    """Same guard on the conversion path: a slot that used to be a scalar and
    was converted to a set must reject a subsequent scalar write too."""
    store.write_fact(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    store.add_member(Slot("user", "bikes owned", "gravel bike"), emb("gravel bike"))
    with pytest.raises(ValueError, match="holds a set"):
        store.write_fact(Slot("user", "bikes owned", "hybrid bike"), emb("hybrid bike"))


def test_forget_entity_clears_members_index_other_slot_survives(store, emb):
    """Item 3(a): forget() rebuilds self._members from scratch — a purged
    entity's members must disappear (members() empty, slot_kind None) while
    an unrelated entity's members survive untouched."""
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    store.add_member(Slot("other", "tags", "keep"), emb("keep"))
    removed = store.forget("user")
    assert removed >= 1
    assert store.members("user", "bikes owned") == []
    assert store.slot_kind("user", "bikes owned") is None
    assert [m.value for m in store.members("other", "tags")] == ["keep"]


def test_dedup_siblings_rebuild_preserves_member_index(store, emb):
    """Item 3(b): dedup_siblings' post-apply rebuild of self._current must
    not lose an unrelated slot's current members. Two scalar slots share an
    (injected) slot_embedding and merge; a member row elsewhere must survive
    the rebuild with its current status and index entry intact. Pure
    in-memory (injected embeddings, like every other cortex test) — no
    Postgres/embedder needed, so this path IS reachable at unit-test level."""
    shared = emb("shared-slot-embedding")
    store.write_fact(Slot("proj", "server-ip", "10.0.0.1"), emb("val-a"),
                     support="user", now=1.0, slot_embedding=shared)
    store.write_fact(Slot("proj", "box-ip", "10.0.0.1"), emb("val-b"),
                     support="agent", now=2.0, slot_embedding=shared)
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))

    report = store.dedup_siblings(threshold=0.99, apply=True)
    assert len(report) == 1                      # the two ip slots merged

    members = store.members("user", "bikes owned")
    assert [m.value for m in members] == ["road bike"]
    assert members[0].status == "current"


def test_reindex_current_keeps_members_demotes_duplicate_scalars(store):
    """Item 3(c): the guard between Task 3 and silent persisted destruction.
    _reindex_current() (called by load()) must NOT apply the scalar
    duplicate-demotion healing to member rows — two current members sharing
    a slot are the normal, valid state, not a legacy collision. Records are
    appended raw (bypassing add_member/write_fact) to simulate what a
    deserializer hands it."""
    store.records.append(CortexRecord(
        entity="user", attribute="bikes owned", value="road bike",
        kind="member", status="current", asserted_at=1.0, last_confirmed=1.0))
    store.records.append(CortexRecord(
        entity="user", attribute="bikes owned", value="gravel bike",
        kind="member", status="current", asserted_at=2.0, last_confirmed=2.0))
    # Legacy pre-normalisation scalar collision at a different slot.
    store.records.append(CortexRecord(
        entity="NEBULA-SERPENT", attribute="type", value="server",
        status="current", asserted_at=1.0, last_confirmed=1.0))
    store.records.append(CortexRecord(
        entity="nebula-serpent", attribute="type", value="workstation",
        status="current", asserted_at=2.0, last_confirmed=2.0))

    store._reindex_current()

    members = store.members("user", "bikes owned")
    assert sorted(m.value for m in members) == ["gravel bike", "road bike"]
    assert all(m.status == "current" for m in members)

    scalars = [r for r in store.records if r.key == ("nebula-serpent", "type")]
    current_scalars = [r for r in scalars if r.status == "current"]
    assert len(current_scalars) == 1
    assert current_scalars[0].value == "workstation"   # most-recently-confirmed kept
    assert [r.status for r in scalars].count("superseded") == 1


def test_member_visible_in_current_records_and_search(store, emb):
    """Item 4: pin the binding requirement directly — a member is not a
    second-class citizen of the read paths. It must show up both in
    current_records() (the dump/introspection path) and in a cosine search
    for its own value."""
    r = store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    assert r.record in store.current_records()
    hits = store.search(emb("road bike"), top_k=5)
    assert any(rec.value == "road bike" for rec, _score in hits)
