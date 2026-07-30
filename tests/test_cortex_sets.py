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
    r = store.add_member(Slot("user", "tags", "tag-overflow"), emb("tag-overflow"))
    assert r.action == "member_capped"
    assert len(store.members("user", "tags")) == 100


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
