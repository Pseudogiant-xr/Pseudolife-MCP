"""Task 2: CortexStore member model — set-valued slots.

A slot can hold either one scalar current record (the existing model) or
many "member" current records (this feature) — never both at once. A scalar
occupying a slot is converted one-way to a member the first time
``add_member`` targets that slot (the scalar row survives as an audit-visible
superseded record, ``superseded_by_value == "(converted to set)"``; there is
no path back from set to scalar).

Dedup at a set slot is confirm-or-insert, never contest: see
``CortexStore.add_member``'s docstring for the v1 decision this pins down.
One exception: an add against a slot whose current scalar is a number-led
aggregate value ("32", "$1,500") is protected — the guard parks the add as
a scalar contender (or confirms the scalar, if the add repeats its value)
instead of converting the slot to a set. See the aggregate-conversion-guard
cases below.

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

import tempfile
import zlib

import pytest
import torch

from pseudolife_memory.memory.cortex import (
    CortexRecord,
    CortexStore,
    MAX_CURRENT_MEMBERS,
    MEMBER_DEDUP_COSINE,
    _is_aggregate_value,
)
from pseudolife_memory.memory.slots import Slot
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


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


def test_is_aggregate_value_detection():
    hits = ["32", "27 species", "$1,500", "+3", "-5", "3.5 kg", "3rd place",
            "  42  ", "€200", "£15"]
    misses = ["gravel bike", "Rosa's Diner", "prod-eu", "", "   ",
              "thirty-two", "iPhone 15", None]
    for v in hits:
        assert _is_aggregate_value(v), f"should match: {v!r}"
    for v in misses:
        assert not _is_aggregate_value(v), f"should not match: {v!r}"


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


def test_member_add_on_aggregate_scalar_parks_contender(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.dirty_slots.clear()
    r = store.add_member(Slot("user", "birds", "Northern Flicker"),
                         emb("Northern Flicker"))
    assert r.action == "contested"
    assert r.record.status == "contested"
    assert r.record.value == "Northern Flicker"
    # Scalar survives, canonical; no set forms.
    assert store.slot_kind("user", "birds") == "scalar"
    assert store.members("user", "birds") == []
    cur = store.records[store._current[("user", "birds")]]
    assert cur.value == "27" and cur.status == "current"
    # Contender must persist: the guard schedules the slot rewrite itself
    # (_contend relies on its caller for dirty_slots, as write_fact does).
    assert ("user", "birds") in store.dirty_slots
    # Audit reason is the guard's own, not a tier reason.
    assert store.supersession_log[-1]["reason"] == "member_add_blocked_aggregate"


@pytest.mark.parametrize("total", ["27 species", "$1,500", "3.5 kg"])
def test_guard_covers_unit_and_currency_totals(store, emb, total):
    store.write_fact(Slot("user", "stat", total), emb(total))
    r = store.add_member(Slot("user", "stat", "Blue Jay"), emb("Blue Jay"))
    assert r.action == "contested"
    assert store.slot_kind("user", "stat") == "scalar"


def test_guard_applies_with_protect_provenance_off(emb):
    """Regression pin (final branch review): the aggregate-conversion guard
    in ``add_member`` must fire regardless of ``protect_provenance`` — it is
    a stated-total protection, not a provenance-tier decision (see
    docs/guide/memory-model.md, "The guard applies unconditionally —
    regardless of memory.cortex.protect_provenance"). ``protect_provenance``
    only gates write_fact's tier-based contest logic (cortex.py ~line 391);
    the guard checked here is a separate, unconditional branch
    (``_is_aggregate_value(cur.value)`` in ``add_member``), so a store built
    with the flag OFF must still park the add rather than converting the
    slot to a set.

    Watched RED: flipping the expected action from "contested" to
    "member_added" makes this fail (confirmed manually, then restored) —
    the guard is not gated by protect_provenance's default-True value.
    """
    store = CortexStore(protect_provenance=False)
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.dirty_slots.clear()

    r = store.add_member(Slot("user", "birds", "Northern Flicker"),
                         emb("Northern Flicker"))

    assert r.action == "contested"
    assert r.record.status == "contested"
    assert r.record.value == "Northern Flicker"
    assert store.slot_kind("user", "birds") == "scalar"
    assert store.members("user", "birds") == []
    cur = store.records[store._current[("user", "birds")]]
    assert cur.value == "27" and cur.status == "current"


def test_same_value_add_on_aggregate_scalar_confirms_not_contests(store, emb):
    """Review finding (FIX 1): re-asserting the SAME value that already
    occupies an aggregate scalar slot via add_member must confirm the
    scalar, not mint a contender identical to itself. Mirrors write_fact's
    own confirm branch (same value -> reinforce, never duplicate)."""
    store.write_fact(Slot("user", "birds", "27"), emb("27"), confidence=0.5)
    store.dirty_slots.clear()
    cur = store.records[store._current[("user", "birds")]]
    before = cur.confidence

    r = store.add_member(Slot("user", "birds", "27"), emb("27"))

    assert r.action == "confirmed"
    assert r.record is cur
    assert r.record.status == "current"
    assert store.contenders_for("user", "birds") == []
    assert r.record.confidence > before
    assert ("user", "birds") in store.dirty_slots


def test_second_blocked_add_supersedes_prior_contender(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.add_member(Slot("user", "birds", "Northern Flicker"),
                     emb("Northern Flicker"))
    r = store.add_member(Slot("user", "birds", "Blue Jay"), emb("Blue Jay"))
    assert r.action == "contested"
    assert [c.value for c in store.contenders_for("user", "birds")] == ["Blue Jay"]
    # The displaced contender stays in the audit trail as superseded.
    gone = [x for x in store.records
            if x.value == "Northern Flicker" and x.status == "superseded"]
    assert len(gone) == 1


def test_repeated_blocked_add_confirms_contender(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    first = store.add_member(Slot("user", "birds", "Northern Flicker"),
                             emb("Northern Flicker")).record
    before = first.confidence
    r = store.add_member(Slot("user", "birds", "Northern Flicker"),
                         emb("Northern Flicker"))
    assert r.action == "contested" and r.record is first
    assert r.record.confidence > before   # reinforce_rate makes strict increase available
    assert len(store.contenders_for("user", "birds")) == 1


def test_resolve_accept_promotes_blocked_member_to_scalar(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.add_member(Slot("user", "birds", "Northern Flicker"),
                     emb("Northern Flicker"))
    r = store.resolve("user", "birds", accept=True)
    assert r.action == "superseded"
    assert store.slot_kind("user", "birds") == "scalar"
    cur = store.records[store._current[("user", "birds")]]
    assert cur.value == "Northern Flicker"


def test_resolve_reject_keeps_aggregate_scalar(store, emb):
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    store.add_member(Slot("user", "birds", "Northern Flicker"),
                     emb("Northern Flicker"))
    r = store.resolve("user", "birds", accept=False)
    assert r.action == "contested"
    cur = store.records[store._current[("user", "birds")]]
    assert cur.value == "27" and cur.status == "current"
    assert store.contenders_for("user", "birds") == []


def test_empty_member_value_on_aggregate_slot_still_invalid(store, emb):
    # Regression pin: today's rejection-before-conversion ordering must
    # survive the aggregate guard too — an empty/blank member value is
    # rejected outright regardless of what occupies the slot, so this
    # passes both before and after the guard lands.
    store.write_fact(Slot("user", "birds", "27"), emb("27"))
    r = store.add_member(Slot("user", "birds", "   "), emb("blank"))
    assert r.action == "member_invalid"
    assert store.contenders_for("user", "birds") == []


def test_member_cap_drops_beyond_100(store, emb):
    for i in range(100):
        store.add_member(Slot("user", "tags", f"tag-{i:03d}"), emb(f"tag-{i:03d}"))
    store.dirty_slots.clear()
    r = store.add_member(Slot("user", "tags", "tag-overflow"), emb("tag-overflow"))
    assert r.action == "member_capped"
    assert len(store.members("user", "tags")) == 100
    # Item 6: the capped result carries the OFFENDING value, not an unrelated
    # existing member, and is not itself a persisted record. Identity check
    # (`is not`), not `==` — CortexRecord's dataclass __eq__ would walk into
    # comparing embedding tensors if enough of the leading fields happened to
    # match, and `tensor == tensor` on a multi-element tensor raises
    # (ambiguous truth value), not returns False.
    assert r.record.value == "tag-overflow"
    assert all(r.record is not x for x in store.records)
    # A rejected add must not schedule a slot rewrite — nothing changed.
    assert ("user", "tags") not in store.dirty_slots


def test_removed_member_can_rejoin(store, emb):
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    store.remove_member("user", "bikes owned", "road bike")
    r = store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    assert r.action == "member_added"
    assert len(store.members("user", "bikes owned")) == 1
    assert len(store.members("user", "bikes owned", include_removed=True)) == 2


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
    """Item 1 (bug) + item 8 (docstring pin): the scalar->member conversion
    built a bare ``Slot(...)`` with no polarity, so a negated fact ("no
    longer have a road bike") silently re-minted as an affirmative member —
    fixed to carry the original scalar's polarity through. Also pins that
    polarity is preserved verbatim (never interpreted) on a PLAIN negated
    add_member, not just on the converted one."""
    store.write_fact(Slot("user", "bikes owned", "road bike", "-"), emb("road bike"))
    store.add_member(Slot("user", "bikes owned", "gravel bike", "-"), emb("gravel bike"))
    members = {m.value: m for m in store.members("user", "bikes owned")}
    assert members["road bike"].polarity == "-"     # converted scalar
    assert members["gravel bike"].polarity == "-"    # plain negated add_member


def test_scalar_to_member_conversion_preserves_provenance(store, emb):
    """F3 (review-caught): the scalar->member conversion's ``_insert_member``
    call carried no ``support`` kwarg, so a scalar written with e.g.
    user-tier support silently re-minted as a member with NO support at all
    (``origin == ""``) — the provenance tier was lost on conversion. Fixed
    to pass the scalar's ``origin`` through (``None`` when it has none, so
    an unsupported scalar still converts to an unsupported member rather
    than fabricating a tier)."""
    store.write_fact(Slot("user", "bikes owned", "road bike"), emb("road bike"),
                     support="user")
    store.add_member(Slot("user", "bikes owned", "gravel bike"), emb("gravel bike"))
    members = {m.value: m for m in store.members("user", "bikes owned")}
    assert members["road bike"].support == {"user"}
    assert members["road bike"].origin == "user"


def test_conversion_drops_freshness_class_and_says_so(store, emb):
    """Deliberate decision (2026-08-09, stale-policy review finding 3): set
    members are evergreen-only — staleness decay compensates for scalar
    values that change without notice, while a set's "no longer true"
    channel is the explicit ``remove_member`` retraction; group-level
    policy transforms cannot honour the fresh-payload no-harm contract
    (docs/guide/memory-model.md, "Conversion rules"). The scalar→set
    conversion therefore DROPS a non-evergreen scalar's freshness class
    rather than carrying it onto the member — but must state the drop on
    the conversion's supersession-log entry instead of doing it silently.
    Unlike polarity/provenance above, this loss is intentional; the audit
    stamp is what makes it a decision rather than a bug."""
    store.write_fact(Slot("deploy", "status", "pending"), emb("pending"),
                     freshness_class="volatile")
    store.add_member(Slot("deploy", "status", "rollback armed"),
                     emb("rollback armed"))
    # The converted member is evergreen — the class did not ride along...
    members = {m.value: m for m in store.members("deploy", "status")}
    assert members["pending"].freshness_class == "evergreen"
    # ...and the audit entry states exactly what was dropped.
    entry = next(e for e in store.supersession_log
                 if e["decision"] == "convert_to_set")
    assert entry["dropped_freshness_class"] == "volatile"


def test_conversion_of_evergreen_scalar_logs_no_drop(store, emb):
    """The stamp marks a real loss, not the no-op case: converting an
    evergreen scalar (nothing to drop) must not add the key, so old log
    entries and drop-free conversions keep their existing shape."""
    store.write_fact(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    store.add_member(Slot("user", "bikes owned", "gravel bike"), emb("gravel bike"))
    entry = next(e for e in store.supersession_log
                 if e["decision"] == "convert_to_set")
    assert "dropped_freshness_class" not in entry


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
    # If the post-apply rebuild regressed to only handling scalars (e.g.
    # dropped the kind-aware split and routed the member row into
    # self._current instead of self._members), the member would wrongly
    # become lookup()-able as a scalar. This is the assertion that actually
    # goes red on that regression — the checks above alone cannot catch it.
    assert store.lookup("user", "bikes owned") is None


def test_dedup_siblings_excludes_members(store, emb):
    """F1 (bank-corrupting, review-caught): on a hydrated bank, member records
    carry ``slot_embedding=None`` (``add_member``/``_insert_member`` never set
    one). The ``cortex_dedup`` backfill loop used to give EVERY current
    record missing a slot embedding — scalars AND members alike — the
    identical value-free ``f"{entity} {attribute}"`` embedding. Since all
    members of one slot share the same ``(entity, attribute)``, that made
    them cosine-identical to each other, and ``dedup_siblings`` then
    clustered and superseded all but one — silently destroying the set.

    Simulated here at the ``CortexStore`` level (no service/embedder
    needed): three members are minted, then given an identical injected
    ``slot_embedding`` exactly as the backfill would. ``dedup_siblings``
    must exclude ``kind == "member"`` records from its candidate pool, so
    all three stay current — while a genuinely-paraphrased SCALAR pair with
    near-identical slot embeddings still merges as before."""
    store.add_member(Slot("user", "tags", "alpha"), emb("alpha"))
    store.add_member(Slot("user", "tags", "beta"), emb("beta"))
    store.add_member(Slot("user", "tags", "gamma"), emb("gamma"))
    shared_member_emb = emb("user tags")
    for m in store.members("user", "tags"):
        m.slot_embedding = shared_member_emb

    shared_scalar_emb = emb("shared-scalar-slot")
    store.write_fact(Slot("proj", "server-ip", "10.0.0.1"), emb("val-a"),
                     support="user", now=1.0, slot_embedding=shared_scalar_emb)
    store.write_fact(Slot("proj", "box-ip", "10.0.0.1"), emb("val-b"),
                     support="agent", now=2.0, slot_embedding=shared_scalar_emb)

    report = store.dedup_siblings(threshold=0.99, apply=True)

    assert len(report) == 1                          # only the scalar pair merged
    members = store.members("user", "tags")
    assert len(members) == 3
    assert all(m.status == "current" for m in members)
    # Exactly one of the two paraphrased scalar slots remains current —
    # the merge that should still happen.
    survivors = [r for r in store.current_records()
                 if r.kind != "member" and r.key in
                 {("proj", "server-ip"), ("proj", "box-ip")}]
    assert len(survivors) == 1


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


def test_all_removed_set_slot_reverts_to_scalar(store, emb):
    """Controller ruling (re-review item 1): a set slot with zero current
    members (all removed) reverts to scalar life. add_member -> remove_member
    leaves the slot with no current members, so write_fact's guard (which
    checks only CURRENT members) permits a fresh scalar write there;
    slot_kind() then reports "scalar" (self._current wins, checked first);
    lookup() returns the new scalar; members() is empty; the removed member
    row survives only as audit (include_removed=True)."""
    store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    store.remove_member("user", "bikes owned", "road bike")

    r = store.write_fact(Slot("user", "bikes owned", "hybrid bike"), emb("hybrid bike"))
    assert r.action == "inserted"

    assert store.slot_kind("user", "bikes owned") == "scalar"
    assert store.lookup("user", "bikes owned").value == "hybrid bike"
    assert store.members("user", "bikes owned") == []
    audit = store.members("user", "bikes owned", include_removed=True)
    assert [m.value for m in audit] == ["road bike"]


def test_member_visible_in_current_records_and_search(store, emb):
    """Item 4: pin the binding requirement directly — a member is not a
    second-class citizen of the read paths. It must show up both in
    current_records() (the dump/introspection path) and in a cosine search
    for its own value."""
    r = store.add_member(Slot("user", "bikes owned", "road bike"), emb("road bike"))
    assert r.record in store.current_records()
    hits = store.search(emb("road bike"), top_k=5)
    assert any(rec.value == "road bike" for rec, _score in hits)


# --- Task 3: persistence roundtrip (kind / value_norm survive save+load) ---
#
# The traced failure (Task 2 review, highest-consequence known risk): if
# ``kind`` is missing from either the torch save/load roundtrip or the PG
# column list/hydrator, member rows hydrate as kind="scalar", status="current"
# — _reindex_current then demotes all but one per slot, and that demotion's
# dirty_slots write PERSISTS the destruction back to Postgres. A 100-member
# set becomes a 1-value scalar across one daemon restart with no error. Both
# halves of that coupling get a dedicated, red-able test below.

def test_kind_survives_torch_roundtrip(store, emb, tmp_path):
    """The file-mode half of the coupling: CortexStore.save()/load() (the
    torch.save round-trip co-located with cms_state.pt) must not lose
    ``kind``. Red if save() omits it from the state dict or load() doesn't
    pass it back into CortexRecord — both members would then hydrate as
    kind="scalar", and _reindex_current's duplicate-scalar healing would
    demote one of them to superseded."""
    store.add_member(Slot("user", "tags", "alpha"), emb("alpha"))
    store.add_member(Slot("user", "tags", "beta"), emb("beta"))
    store.write_fact(Slot("user", "city", "Sydney"), emb("Sydney"))

    path = tmp_path / "cortex_state.pt"
    store.save(path)

    fresh = CortexStore()
    fresh.load(path)

    assert fresh.slot_kind("user", "tags") == "set"
    members = {m.value: m for m in fresh.members("user", "tags")}
    assert set(members) == {"alpha", "beta"}
    assert all(m.kind == "member" and m.status == "current"
              for m in members.values())
    assert fresh.slot_kind("user", "city") == "scalar"
    scalar = fresh.lookup("user", "city")
    assert scalar.kind == "scalar" and scalar.value == "Sydney"


@pytest.fixture()
def store_with_pg(pg_conn, pg_url):  # noqa: F811
    """A bare CortexStore paired with a ``reload_store()`` closure that
    write-throughs its dirty slots to Postgres and hydrates a brand new
    CortexStore from the facts table — the persistence-fixture idiom used by
    ``tests/test_entity_kind_inference.py::test_freshness_class_survives_a_write_read_round_trip``,
    generalised so callers don't need a full MemoryService."""
    from pseudolife_memory.storage import sync
    from pseudolife_memory.storage.postgres import PostgresStorage

    storage = PostgresStorage(pg_url)
    store = CortexStore()

    def reload_store() -> CortexStore:
        sync.sync_cortex_slots(store, storage)
        fresh = CortexStore()
        sync.hydrate_cortex(fresh, storage)
        return fresh

    yield store, reload_store
    storage.close()


def test_store_roundtrip_preserves_kind(store_with_pg, emb):
    """This task's RED: the store-level persistence roundtrip through
    Postgres. ``kind`` must appear in ``_FACT_COLS`` and be threaded through
    ``upsert_fact``/``_record_to_row``/``hydrate_cortex`` for a member to
    survive a fresh hydration as a member at all."""
    store, reload_store = store_with_pg
    store.add_member(Slot("user", "tags", "alpha"), emb("alpha", dim=1024))
    fresh = reload_store()
    assert fresh.slot_kind("user", "tags") == "set"
    assert [m.value for m in fresh.members("user", "tags")] == ["alpha"]


def test_blocked_aggregate_contender_survives_pg_roundtrip(store_with_pg, emb):
    """Regression pin: the guard's contender must survive persistence with
    status and kind intact — a hydration that dropped either would resurrect
    the destructive conversion on the next daemon restart."""
    store, reload_store = store_with_pg
    store.write_fact(Slot("user", "birds", "27"), emb("27", dim=1024))
    r = store.add_member(Slot("user", "birds", "Northern Flicker"),
                         emb("Northern Flicker", dim=1024))
    assert r.action == "contested"
    fresh = reload_store()
    assert fresh.slot_kind("user", "birds") == "scalar"
    assert fresh.members("user", "birds") == []
    cur = fresh.records[fresh._current[("user", "birds")]]
    assert cur.value == "27" and cur.status == "current"
    conts = fresh.contenders_for("user", "birds")
    assert [c.value for c in conts] == ["Northern Flicker"]
    assert conts[0].kind == "scalar"


@pytest.fixture()
def svc(pg_conn, pg_url):  # noqa: F811
    """Same idiom as ``tests/test_entity_kind_inference.py``'s ``svc``
    fixture — a real
    MemoryService against the bench Postgres, torn down after the test."""
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def test_kind_survives_pg_hydration(svc):
    """The Postgres half of the coupling, at the storage layer directly
    (not yet through the service API — svc.set_add/set_remove are Task 4).
    Writes 2 members on one slot + 1 scalar on another straight through
    ``CortexStore``, persists via ``svc.save()`` (PG mode + explicit save ->
    ``sync.snapshot_cortex``'s full-resync path), and hydrates a FRESH
    CortexStore from the facts table — exercising ``_FACT_COLS``,
    ``upsert_fact``'s NOT NULL guard, ``_record_to_row``, and
    ``hydrate_cortex`` together.

    Also pins the Task 1 review finding this task's brief calls out
    explicitly: a persisted member row's ``value_norm`` must be non-NULL,
    since Postgres treats NULL as distinct in a unique index and a NULL
    ``value_norm`` would silently bypass ``facts_member_current_uq``. F5
    (review-caught): non-NULL alone doesn't pin the CONTENT — a row could
    persist any non-null junk and the assertion would still pass. The
    "alpha" member is written as ``" Alpha "`` (leading/trailing space,
    mixed case) so the persisted ``value_norm`` is checked against
    :func:`pseudolife_memory.memory.cortex._norm_value` applied to the
    ORIGINAL text, not merely re-derived from the already-normalised value
    stored in the record.
    """
    import torch

    from pseudolife_memory.memory.cortex import CortexStore, _norm_value
    from pseudolife_memory.memory.slots import Slot
    from pseudolife_memory.storage import sync

    EMB = torch.zeros(1024)

    svc._ensure_init()
    svc._cortex.add_member(Slot("user", "tags", " Alpha "), EMB)
    svc._cortex.add_member(Slot("user", "tags", "beta"), EMB)
    svc._cortex.write_fact(Slot("user", "city", "Sydney"), EMB)
    svc.save()

    rows = svc._storage.load_facts()
    member_rows = [r for r in rows if r["kind"] == "member"]
    scalar_rows = [r for r in rows if r["kind"] == "scalar"]
    assert len(member_rows) == 2
    assert len(scalar_rows) == 1
    # Task 1's per-slot member-uniqueness index depends on this being non-NULL...
    assert all(r["value_norm"] for r in member_rows)
    assert all(r["value_norm"] is None for r in scalar_rows)
    # ...and F5 pins the actual CONTENT, not just non-NULLness.
    by_value = {r["value"]: r for r in member_rows}
    assert by_value[" Alpha "]["value_norm"] == _norm_value(" Alpha ") == "alpha"
    assert by_value["beta"]["value_norm"] == _norm_value("beta") == "beta"

    fresh = CortexStore()
    sync.hydrate_cortex(fresh, svc._storage)

    assert fresh.slot_kind("user", "tags") == "set"
    members = fresh.members("user", "tags")
    assert sorted(m.value for m in members) == [" Alpha ", "beta"]  # verbatim, not normalised
    assert all(m.status == "current" and m.kind == "member" for m in members)

    scalar = fresh.lookup("user", "city")
    assert scalar is not None and scalar.kind == "scalar" and scalar.value == "Sydney"


def test_members_survive_restart(tmp_service_dir):
    """Service-level restart test — written now per the brief, unskipped in
    Task 4 once ``svc.set_add``/``svc.set_remove`` exist."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    svc.set_add("user", "bikes owned", "road bike")
    svc.set_add("user", "bikes owned", "gravel bike")
    svc.set_remove("user", "bikes owned", "road bike")
    svc.save()
    svc2 = MemoryService(data_dir=tmp_service_dir)
    got = svc2.cortex_lookup("user", "bikes owned")
    assert got["kind"] == "set"
    assert [m["value"] for m in got["members"]] == ["gravel bike"]
    assert [m["value"] for m in got["removed"]] == ["road bike"]


@pytest.fixture()
def tmp_service_dir(tmp_path):
    return str(tmp_path)


# --- Task 4: service surface --------------------------------------------


def test_set_add_returns_documented_shape(tmp_service_dir):
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    out = svc.set_add("user", "bikes owned", "road bike")
    assert out == {
        "action": "member_added",
        "entity": "user",
        "attribute": "bikes owned",
        "member": "road bike",
        "members_count": 1,
    }
    out2 = svc.set_add("user", "bikes owned", "Road Bike")   # dedup -> confirm
    assert out2["action"] == "member_confirmed"
    assert out2["members_count"] == 1


def test_set_remove_returns_documented_shape(tmp_service_dir):
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    svc.set_add("user", "bikes owned", "road bike")
    out = svc.set_remove("user", "bikes owned", "road bike")
    assert out == {
        "action": "member_removed",
        "entity": "user",
        "attribute": "bikes owned",
        "member": "road bike",
        "members_count": 0,
    }
    missing = svc.set_remove("user", "bikes owned", "unicycle")
    assert missing["action"] == "member_not_found"


def test_cortex_write_rejects_scalar_write_on_set_slot_with_actionable_message(
        tmp_service_dir):
    """Item 2 (tool-boundary mapping): cortex_write must translate the
    store's ValueError into a message naming the set tools, not the store's
    own add_member/remove_member names."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    svc.set_add("user", "bikes owned", "road bike")
    with pytest.raises(ValueError, match="memory_set_add"):
        svc.cortex_write("user", "bikes owned", "hybrid bike")


def test_promote_slots_skips_set_slot_and_logs_info(tmp_service_dir, caplog):
    """Item 1 (extraction-path routing): a scalar candidate for a slot that
    already holds a set must not raise out of the auto-promote loop, must
    not mutate the set, and must be logged at INFO naming the slot (not
    silently dropped at debug like an unrelated write failure)."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    svc.set_add("user", "database", "postgres")
    before = sorted(m["value"] for m in svc.cortex_lookup("user", "database")["members"])

    with caplog.at_level("INFO", logger="pseudolife_memory.service"):
        svc.config.memory.cortex.auto_promote = True
        out = svc.store("my database is mysql", source="conversation")

    assert out["cortex_promoted"] == 0
    after = sorted(m["value"] for m in svc.cortex_lookup("user", "database")["members"])
    assert after == before
    assert any(
        "slot holds a set" in rec.message and "user.database" in rec.message
        for rec in caplog.records
    )


def test_cortex_lookup_on_set_slot(tmp_service_dir):
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    svc.set_add("user", "bikes owned", "road bike")
    svc.set_add("user", "bikes owned", "gravel bike")
    svc.set_remove("user", "bikes owned", "road bike")
    got = svc.cortex_lookup("user", "bikes owned")
    assert got["kind"] == "set"
    assert got["entity"] == "user" and got["attribute"] == "bikes owned"
    assert [m["value"] for m in got["members"]] == ["gravel bike"]
    assert [m["value"] for m in got["removed"]] == ["road bike"]


def test_history_on_set_slot_is_time_ordered(tmp_service_dir):
    # Flaked once in a full-suite run (2026-08-06): the version list is
    # built member-by-member and only the timestamp sort restores
    # chronology, so an equal-timestamp tie let a removal sort ahead of a
    # later member's add. The tie itself is pinned deterministically by
    # test_history_set_order_is_deterministic_under_timestamp_ties below.
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    svc.set_add("user", "bikes owned", "road bike")
    svc.set_add("user", "bikes owned", "gravel bike")
    svc.set_remove("user", "bikes owned", "road bike")
    got = svc.history("user", "bikes owned")
    assert got["kind"] == "set"
    events = [(v["value"], v["event"]) for v in got["versions"]]
    # road bike added, gravel bike added, road bike removed — time-ordered.
    assert events == [
        ("road bike", "added"),
        ("gravel bike", "added"),
        ("road bike", "removed"),
    ]
    ats = [v["at"] for v in got["versions"]]
    assert ats == sorted(ats)


def test_history_set_order_is_deterministic_under_timestamp_ties(
        tmp_service_dir, monkeypatch):
    """Regression (2026-08-06 flake): with every write stamped at the SAME
    clock value, the set-history sort has no timestamp signal at all — the
    order must still come out adds-in-insertion-order with each removal
    after the adds of its instant, via the tie-break key, not sort luck."""
    import pseudolife_memory.memory.cortex as cortex_mod
    from pseudolife_memory.service import MemoryService

    class _FrozenTime:
        @staticmethod
        def time():
            return 1_700_000_000.0

    monkeypatch.setattr(cortex_mod, "time", _FrozenTime)
    svc = MemoryService(data_dir=tmp_service_dir)
    svc.set_add("user", "bikes owned", "road bike")
    svc.set_add("user", "bikes owned", "gravel bike")
    svc.set_remove("user", "bikes owned", "road bike")
    got = svc.history("user", "bikes owned")
    events = [(v["value"], v["event"]) for v in got["versions"]]
    assert events == [
        ("road bike", "added"),
        ("gravel bike", "added"),
        ("road bike", "removed"),
    ]


def test_resolve_refuses_when_slot_converted_to_set(store, emb):
    """Item 3 (resolve() bypass): a contender parked on a scalar slot before
    the slot converts to a set must not be promotable/retirable through
    resolve() afterwards — that would bypass write_fact's scalar/set
    exclusivity guard. Refused, members untouched."""
    store.write_fact(Slot("user", "bikes owned", "road bike"), emb("road bike"),
                      support="user")
    store.write_fact(Slot("user", "bikes owned", "gravel bike"), emb("gravel bike"),
                      support="agent")   # weaker tier -> parked as contender
    assert [c.value for c in store.contenders_for("user", "bikes owned")] == ["gravel bike"]

    store.add_member(Slot("user", "bikes owned", "hybrid bike"), emb("hybrid bike"))
    assert store.slot_kind("user", "bikes owned") == "set"
    before = sorted(m.value for m in store.members("user", "bikes owned"))

    r = store.resolve("user", "bikes owned", accept=True)
    assert r.action == "refused"

    after = sorted(m.value for m in store.members("user", "bikes owned"))
    assert after == before
    assert [c.value for c in store.contenders_for("user", "bikes owned")] == ["gravel bike"]


def test_cortex_resolve_service_reports_slot_holds_set(tmp_service_dir):
    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    svc.cortex_write("user", "bikes owned", "road bike", support="user")
    svc.cortex_write("user", "bikes owned", "gravel bike", support="agent")
    svc.set_add("user", "bikes owned", "hybrid bike")
    res = svc.cortex_resolve("user", "bikes owned", accept=True)
    assert res == {"resolved": False, "reason": "slot_holds_set",
                    "entity": "user", "attribute": "bikes owned"}


# --- Task 5 review carry: the untested PG durability leg ------------------


def test_set_add_remove_survive_pg_hydration_through_the_service(svc, pg_url):  # noqa: F811
    """Task 4's PG coverage (``test_kind_survives_pg_hydration`` above) calls
    ``svc._cortex.add_member`` directly and rehydrates a bare ``CortexStore``
    — it never exercises ``svc.set_add``/``svc.set_remove`` themselves, so
    the per-slot write-through those methods actually take on every write
    (``_save_cortex`` -> ``sync.sync_cortex_slots`` ->
    ``PostgresStorage.replace_slot_facts`` -> ``_txn``) — the exact path a
    live ``memory_set_add``/``memory_set_remove`` MCP call uses — stayed
    untested. Close that gap: two adds + one remove THROUGH THE SERVICE API
    (each call persists on its own; no explicit ``svc.save()``), then
    rehydrate a brand-new ``MemoryService`` against the same database and
    read the slot back."""
    import tempfile

    from pseudolife_memory.service import MemoryService

    svc.set_add("user", "bikes owned", "road bike")
    svc.set_add("user", "bikes owned", "gravel bike")
    svc.set_remove("user", "bikes owned", "road bike")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fresh = MemoryService(data_dir=d, database_url=pg_url)
        try:
            got = fresh.cortex_lookup("user", "bikes owned")
            assert got["kind"] == "set"
            assert [m["value"] for m in got["members"]] == ["gravel bike"]
            assert [m["value"] for m in got["removed"]] == ["road bike"]
        finally:
            if fresh._storage is not None:
                fresh._storage.close()


def test_set_add_alone_survives_pg_hydration_through_the_service(svc, pg_url):  # noqa: F811
    """Review finding on the test above: it calls set_add TWICE then
    set_remove ONCE, so it does not actually pin set_add's own
    ``_save_cortex()`` call — ``dirty_slots`` is slot-keyed, not
    call-keyed, so the later set_remove's flush would carry an earlier
    set_add's dirty mark along for free even if set_add never called
    ``_save_cortex`` itself (verified empirically: commenting out just
    set_add's ``_save_cortex()`` left the test above green). Pin set_add's
    own write-through in isolation: ONE add, no other write of any kind on
    this service, then rehydrate a fresh service and assert the member
    survived."""
    import tempfile

    from pseudolife_memory.service import MemoryService

    svc.set_add("user", "bikes owned", "road bike")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        fresh = MemoryService(data_dir=d, database_url=pg_url)
        try:
            got = fresh.cortex_lookup("user", "bikes owned")
            assert got is not None and got["kind"] == "set"
            assert [m["value"] for m in got["members"]] == ["road bike"]
        finally:
            if fresh._storage is not None:
                fresh._storage.close()


# --- Task 6: serving — one entry per set slot ------------------------------
#
# Today each member of a set-valued slot surfaces as its OWN entry in
# cortex_search (it is a plain current record, so the dense/BM25 pool ranks
# it exactly like a scalar fact). These tests pin the grouping: a set slot
# must collapse to exactly ONE entry, ranked by its best-scoring member, with
# a composed value string that names the WHOLE current membership (not just
# whichever members happened to individually clear the score floor).
#
# Embeddings are injected directly (bypassing the real embedder, and
# ``svc.set_add``'s own embedding call) so ranking is deterministic: two
# orthogonal unit vectors for the two members, and ``encode_query`` stubbed
# to hand back one of them verbatim — cosine 1.0 for the named member, 0.0
# for the other, with no dependence on how the real sentence-transformer
# happens to score "road bike" against "gravel bike".

def _service_with_deterministic_embedder(tmp_service_dir):
    import torch

    from pseudolife_memory.service import MemoryService

    svc = MemoryService(data_dir=tmp_service_dir)
    svc._ensure_init()
    dim = svc._embedder.encode_single("probe").shape[0]
    v1 = torch.zeros(dim)
    v1[0] = 1.0
    v2 = torch.zeros(dim)
    v2[1] = 1.0
    return svc, v1, v2


def test_cortex_search_groups_set_slot_into_one_entry(tmp_service_dir):
    from pseudolife_memory.memory.slots import Slot

    svc, v1, v2 = _service_with_deterministic_embedder(tmp_service_dir)
    svc._cortex.add_member(Slot("user", "bikes owned", "road bike"), v1)
    svc._cortex.add_member(Slot("user", "bikes owned", "gravel bike"), v2)
    svc._embedder.encode_query = lambda text, normalize=True: v1.clone()

    entries = svc.cortex_search("road bike", top_k=5, min_score=0.0)["entries"]
    assert len(entries) == 1                     # ONE entry, not one per member
    e = entries[0]
    assert e["kind"] == "set"
    assert e["entity"] == "user" and e["attribute"] == "bikes owned"
    assert e["value"] == "road bike; gravel bike (2 members)"
    assert e["score"] == 1.0                      # max over the members that ranked
    assert e["contested"] is False
    assert [m["value"] for m in e["members"]] == ["road bike", "gravel bike"]
    # F5 (Task 6 review): a set entry must carry SOME currency signal —
    # max last_confirmed over the full current membership — so the
    # mcp_server cortex-first block has a real date to render instead of
    # silently going blank for every set slot.
    assert "last_confirmed" in e
    assert e["last_confirmed"] == max(m["last_confirmed"] for m in e["members"])
    # Re-review: asserted_at + age were still missing entirely. Both are
    # backed by the same anchor -- max(tx_time or asserted_at) over the
    # full current membership, the same priority _cortex_record_to_dict
    # uses for a scalar's "age" (tx_time preferred over asserted_at).
    # "asserted_at" is the raw float, exactly how _cortex_record_to_dict
    # renders a scalar's own "asserted_at" (no ISO formatting at this
    # layer -- mcp_server._iso_seconds does that downstream).
    want_anchor = max(
        (m["tx_time"] or m["asserted_at"]) for m in e["members"])
    assert e["asserted_at"] == want_anchor
    assert isinstance(e["asserted_at"], float)
    assert e["age"], "set entry must carry a human-readable age"


def test_cortex_search_set_entry_orders_by_score_not_insertion(tmp_service_dir):
    """Insertion order was road bike then gravel bike; the query names gravel
    bike, so the composed value must lead with gravel bike (score-descending,
    the order members "emerged from fusion") even though it was added
    second."""
    from pseudolife_memory.memory.slots import Slot

    svc, v1, v2 = _service_with_deterministic_embedder(tmp_service_dir)
    svc._cortex.add_member(Slot("user", "bikes owned", "road bike"), v1)
    svc._cortex.add_member(Slot("user", "bikes owned", "gravel bike"), v2)
    svc._embedder.encode_query = lambda text, normalize=True: v2.clone()

    entries = svc.cortex_search("gravel bike", top_k=5, min_score=0.0)["entries"]
    assert len(entries) == 1
    assert entries[0]["value"] == "gravel bike; road bike (2 members)"
    assert entries[0]["score"] == 1.0


def test_cortex_search_set_entry_lists_unranked_current_members(tmp_service_dir):
    """A min_score floor that only "road bike" clears must not shrink the
    composed value to just that member — the slot's full current membership
    (fetched independently of what made it into the ranked hit list) is
    always shown, per the brief."""
    from pseudolife_memory.memory.slots import Slot

    svc, v1, v2 = _service_with_deterministic_embedder(tmp_service_dir)
    svc._cortex.add_member(Slot("user", "bikes owned", "road bike"), v1)
    svc._cortex.add_member(Slot("user", "bikes owned", "gravel bike"), v2)
    svc._embedder.encode_query = lambda text, normalize=True: v1.clone()

    # gravel bike is orthogonal to the query -> cosine 0.0, below the floor.
    entries = svc.cortex_search("road bike", top_k=5, min_score=0.5)["entries"]
    assert len(entries) == 1
    assert entries[0]["value"] == "road bike; gravel bike (2 members)"
    assert entries[0]["score"] == 1.0


def test_cortex_search_no_set_entry_when_no_member_ranks(tmp_service_dir):
    """If every member of a slot scores below the floor, the slot must not
    appear at all — grouping only starts once at least one member ranks."""
    from pseudolife_memory.memory.slots import Slot

    svc, v1, v2 = _service_with_deterministic_embedder(tmp_service_dir)
    svc._cortex.add_member(Slot("user", "bikes owned", "road bike"), v1)
    svc._cortex.add_member(Slot("user", "bikes owned", "gravel bike"), v2)
    import torch
    svc._embedder.encode_query = lambda text, normalize=True: torch.zeros_like(v1)

    entries = svc.cortex_search("unrelated", top_k=5, min_score=0.5)["entries"]
    assert entries == []


def test_cortex_search_mixed_scalar_and_set_entries(tmp_service_dir):
    """Scalar facts and a set-slot entry can co-exist in one result list; the
    scalar path is untouched (still carries contested/source_entries shape)."""
    svc, v1, v2 = _service_with_deterministic_embedder(tmp_service_dir)
    from pseudolife_memory.memory.slots import Slot

    svc._cortex.add_member(Slot("user", "bikes owned", "road bike"), v1)
    svc._cortex.write_fact(Slot("user", "city", "Sydney"), v2)
    svc._embedder.encode_query = lambda text, normalize=True: (v1 + v2) / 2

    entries = svc.cortex_search("bikes and city", top_k=5, min_score=0.0)["entries"]
    kinds = {e.get("kind") for e in entries}
    assert "set" in kinds
    scalar = next(e for e in entries if e.get("kind") != "set")
    assert scalar["value"] == "Sydney"
    assert scalar["contested"] is False
