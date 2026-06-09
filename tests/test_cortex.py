"""Phase 1 acceptance tests for the sibling cortex store (slot-keyed canonical facts).

Maps to the hippocampal/cortical spec's §12:
  * dedup        — same fact asserted N times → one ``current`` record, all provenance
  * supersession — value change → old ``superseded``, new ``current``, search returns only new
  * no-decay     — an untouched fact is unchanged after unrelated writes; the cortex
                   deliberately exposes no decay / promotion sweep
  * read-isolation — fuzzy search returns only ``current`` records (never superseded)

Plus the confidence-margin supersession block, confirm-reinforcement bound, and a
persistence round-trip (incl. missing-file robustness).

Embeddings are injected (dependency injection) so these run fast without loading a
sentence-transformer. Time is injected via ``now=`` for determinism.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import torch

from pseudolife_memory.memory.cortex import CortexStore, CortexRecord
from pseudolife_memory.memory.slots import Slot


def _unit(seed: int, dim: int = 8) -> torch.Tensor:
    """Deterministic unit vector for a given seed."""
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(dim, generator=g)
    return v / v.norm()


def test_same_fact_ten_times_yields_one_current_record_with_all_provenance():
    store = CortexStore()
    emb = _unit(1)
    for i in range(10):
        store.write_fact(
            Slot("qwen", "sampling.temp", "0.7"),
            emb,
            provenance=[f"ep{i}"],
            now=1000.0 + i,
        )
    rec = store.lookup("qwen", "sampling.temp")
    assert rec is not None
    assert rec.value == "0.7"
    assert rec.status == "current"
    current = [r for r in store.records_for("qwen", "sampling.temp") if r.status == "current"]
    assert len(current) == 1
    assert set(rec.provenance) == {f"ep{i}" for i in range(10)}
    assert rec.asserted_at == 1000.0
    assert rec.last_confirmed == 1009.0


def test_value_change_supersedes_old_and_search_returns_only_new():
    store = CortexStore()
    store.write_fact(Slot("grid", "size", "40"), _unit(2), provenance=["ep1"], now=1.0)
    store.write_fact(Slot("grid", "size", "41"), _unit(3), provenance=["ep2"], now=2.0)

    cur = store.lookup("grid", "size")
    assert cur.value == "41"
    assert cur.status == "current"
    assert cur.supersedes_value == "40"

    history = store.records_for("grid", "size")
    old = [r for r in history if r.value == "40"][0]
    assert old.status == "superseded"
    assert old.superseded_by_value == "41"
    assert old.superseded_at == 2.0

    hits = store.search(_unit(3), top_k=10)
    values = [r.value for r, _ in hits]
    assert "41" in values
    assert "40" not in values


def test_lower_confidence_candidate_does_not_supersede():
    store = CortexStore(supersede_confidence_margin=0.15)
    store.write_fact(Slot("box", "ip", "192.168.0.104"), _unit(4), confidence=0.9, now=1.0)
    res = store.write_fact(Slot("box", "ip", "10.0.0.5"), _unit(5), confidence=0.5, now=2.0)
    assert res.action == "contested"
    cur = store.lookup("box", "ip")
    assert cur.value == "192.168.0.104"
    assert cur.status == "current"
    assert len(store.supersession_log) >= 1


def test_untouched_fact_unchanged_after_unrelated_writes_no_decay():
    store = CortexStore()
    store.write_fact(Slot("user", "city", "Sydney"), _unit(6), confidence=0.8, now=1.0)
    before = store.lookup("user", "city")
    snapshot = (before.value, before.status, before.confidence, before.last_confirmed)
    for i in range(50):
        store.write_fact(Slot(f"e{i}", "attr", f"v{i}"), _unit(100 + i), now=1000.0 + i)
    after = store.lookup("user", "city")
    assert (after.value, after.status, after.confidence, after.last_confirmed) == snapshot
    # The cortex must NOT carry the continuum's decay/promotion machinery.
    assert not hasattr(store, "decay")
    assert not hasattr(store, "promote")


def test_reasserting_same_fact_reinforces_confidence_but_stays_bounded():
    store = CortexStore()
    store.write_fact(Slot("x", "y", "z"), _unit(7), confidence=0.5, now=1.0)
    c0 = store.lookup("x", "y").confidence
    for i in range(20):
        store.write_fact(Slot("x", "y", "z"), _unit(7), now=2.0 + i)
    c1 = store.lookup("x", "y").confidence
    assert c1 > c0
    assert c1 <= 1.0


def test_persistence_roundtrip_preserves_current_superseded_and_provenance():
    with tempfile.TemporaryDirectory() as d:
        store = CortexStore()
        store.write_fact(Slot("grid", "size", "40"), _unit(2), provenance=["ep1"], now=1.0)
        store.write_fact(Slot("grid", "size", "41"), _unit(3), provenance=["ep2"], now=2.0)
        store.write_fact(Slot("qwen", "temp", "0.7"), _unit(1), provenance=["epA"], now=3.0)
        store.dream_cursor = 12345.0
        path = Path(d) / "cortex_state.pt"
        store.save(path)

        loaded = CortexStore()
        loaded.load(path)
        cur = loaded.lookup("grid", "size")
        assert cur.value == "41" and cur.status == "current"
        hist = loaded.records_for("grid", "size")
        assert any(r.value == "40" and r.status == "superseded" for r in hist)
        assert set(loaded.lookup("qwen", "temp").provenance) == {"epA"}
        hits = loaded.search(_unit(3), top_k=5)
        assert hits and hits[0][0].value == "41"
        assert loaded.dream_cursor == 12345.0


def test_support_recorded_and_origin_promotes_on_corroboration():
    # A fact the agent guesses, then the user confirms, must report origin=user.
    store = CortexStore()
    r0 = store.write_fact(Slot("nebula", "runtime", "node"), _unit(8),
                          confidence=0.6, support="agent", now=1.0)
    assert r0.action == "inserted"
    assert r0.record.support == {"agent"}
    assert r0.record.origin == "agent"

    r1 = store.write_fact(Slot("nebula", "runtime", "node"), _unit(8),
                          confidence=0.95, support="user", now=2.0)
    assert r1.action == "confirmed"
    cur = store.lookup("nebula", "runtime")
    assert cur.support == {"agent", "user"}
    assert cur.origin == "user"          # user outranks agent in the support set


def test_user_confirmation_lifts_confidence_past_reinforce():
    store = CortexStore()
    store.write_fact(Slot("a", "b", "c"), _unit(9), confidence=0.5, support="agent", now=1.0)
    # plain agent reinforce of 0.5 would be ~0.67; a user confirm at 0.95 must win.
    store.write_fact(Slot("a", "b", "c"), _unit(9), confidence=0.95, support="user", now=2.0)
    assert store.lookup("a", "b").confidence >= 0.95


def test_origin_empty_without_support_and_support_persists_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        store = CortexStore()
        store.write_fact(Slot("k", "v", "1"), _unit(10), now=1.0)                    # no support
        store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(11),
                         support="user", now=2.0)
        assert store.lookup("k", "v").origin == ""
        path = Path(d) / "cortex_state.pt"
        store.save(path)

        loaded = CortexStore()
        loaded.load(path)
        assert loaded.lookup("k", "v").support == set()
        assert loaded.lookup("box", "ip").support == {"user"}
        assert loaded.lookup("box", "ip").origin == "user"


def test_forget_removes_entity_and_rebuilds_index():
    store = CortexStore()
    store.write_fact(Slot("keep", "a", "1"), _unit(12), now=1.0)
    store.write_fact(Slot("junk", "x", "old"), _unit(13), now=2.0)
    store.write_fact(Slot("junk", "x", "new"), _unit(14), now=3.0)   # supersedes -> 2 records
    store.write_fact(Slot("junk", "y", "v"), _unit(15), now=4.0)
    removed = store.forget("junk")
    assert removed == 3                                  # both x records + the y record
    assert store.lookup("junk", "x") is None
    assert store.lookup("junk", "y") is None
    assert store.lookup("keep", "a").value == "1"        # untouched, still indexed
    assert store.stats()["slots"] == 1


def test_forget_exact_slot_only():
    store = CortexStore()
    store.write_fact(Slot("proj", "lang", "rust"), _unit(16), now=1.0)
    store.write_fact(Slot("proj", "port", "5000"), _unit(17), now=2.0)
    assert store.forget("proj", "lang") == 1
    assert store.lookup("proj", "lang") is None
    assert store.lookup("proj", "port").value == "5000"  # sibling slot survives


def test_separator_and_case_variants_collapse_to_one_slot():
    store = CortexStore()
    a = store.write_fact(Slot("NEBULA-SERPENT", "grid_size", "41x41"), _unit(20), now=1.0)
    assert a.action == "inserted"
    # same fact, different casing AND separators -> must confirm, not fork
    b = store.write_fact(Slot("nebula serpent", "grid-size", "41x41"), _unit(20), now=2.0)
    assert b.action == "confirmed"
    c = store.write_fact(Slot("nebula.serpent", "grid.size", "41x41"), _unit(20), now=3.0)
    assert c.action == "confirmed"
    assert store.stats()["slots"] == 1
    assert store.lookup("nebula_serpent", "grid_size").value == "41x41"   # any variant resolves


def test_load_reconciles_legacy_colliding_current_records():
    with tempfile.TemporaryDirectory() as d:
        store = CortexStore()
        # simulate pre-normalisation persistence: two CURRENT records differing
        # only by case/separator (impossible to create after this change).
        store.records = [
            CortexRecord(entity="NEBULA-SERPENT", attribute="runtime", value="old",
                         status="current", asserted_at=1.0, last_confirmed=1.0, embedding=_unit(1)),
            CortexRecord(entity="nebula serpent", attribute="runtime", value="new",
                         status="current", asserted_at=2.0, last_confirmed=2.0, embedding=_unit(2)),
        ]
        path = Path(d) / "c.pt"
        store.save(path)

        loaded = CortexStore()
        loaded.load(path)
        cur = [r for r in loaded.records if r.status == "current"]
        assert len(cur) == 1
        assert loaded.lookup("nebula-serpent", "runtime").value == "new"   # newer kept
        assert any(r.status == "superseded" and r.value == "old" for r in loaded.records)


def test_vocab_lists_normalised_current_slots():
    store = CortexStore()
    store.write_fact(Slot("NEBULA-SERPENT", "grid_size", "41x41"), _unit(20), now=1.0)
    store.write_fact(Slot("box", "ip", "1.2.3.4"), _unit(21), now=2.0)
    v = store.vocab()
    assert "nebula-serpent.grid-size" in v
    assert "box.ip" in v


def test_load_missing_file_is_empty_not_error():
    with tempfile.TemporaryDirectory() as d:
        store = CortexStore()
        store.load(Path(d) / "does_not_exist.pt")  # must not raise
        assert store.lookup("anything", "here") is None


# ---------------------------------------------------------------------------
# Provenance-aware contenders — a weaker-tier (or below-margin) conflicting
# write is parked as a status="contested" contender, not silently superseded.
# ---------------------------------------------------------------------------


def test_agent_write_contends_user_fact_not_supersede():
    store = CortexStore()
    store.write_fact(Slot("box", "ip", "10.0.0.1"), _unit(20), support="user", now=1.0)
    res = store.write_fact(Slot("box", "ip", "10.0.0.2"), _unit(21), support="agent", now=2.0)
    assert res.action == "contested"          # parked, not superseded
    assert store.lookup("box", "ip").value == "10.0.0.1"   # user fact still current
    conts = store.contenders_for("box", "ip")
    assert len(conts) == 1 and conts[0].value == "10.0.0.2"
    assert conts[0].status == "contested"


def test_action_over_agent_supersedes_but_agent_over_action_contends():
    store = CortexStore()
    store.write_fact(Slot("svc", "port", "8080"), _unit(22), support="agent", now=1.0)
    r1 = store.write_fact(Slot("svc", "port", "9090"), _unit(23), support="action", now=2.0)
    assert r1.action == "superseded"          # action (2) >= agent (1)
    assert store.lookup("svc", "port").value == "9090"
    r2 = store.write_fact(Slot("svc", "port", "7070"), _unit(24), support="agent", now=3.0)
    assert r2.action == "contested"           # agent (1) < action (2)
    assert store.lookup("svc", "port").value == "9090"


def test_user_write_supersedes_lower_tier():
    store = CortexStore()
    store.write_fact(Slot("p", "lang", "go"), _unit(25), support="agent", now=1.0)
    r = store.write_fact(Slot("p", "lang", "rust"), _unit(26), support="user", now=2.0)
    assert r.action == "superseded"
    assert store.lookup("p", "lang").value == "rust"


def test_below_margin_same_tier_now_records_contender():
    store = CortexStore(supersede_confidence_margin=0.15)
    store.write_fact(Slot("box", "ip", "192.168.0.104"), _unit(27), confidence=0.9,
                     support="agent", now=1.0)
    res = store.write_fact(Slot("box", "ip", "10.0.0.5"), _unit(28), confidence=0.5,
                           support="agent", now=2.0)
    assert res.action == "contested"
    assert store.lookup("box", "ip").value == "192.168.0.104"
    assert len(store.contenders_for("box", "ip")) == 1


def test_at_most_one_active_contender_newer_value_supersedes_prior():
    store = CortexStore()
    store.write_fact(Slot("k", "v", "current"), _unit(29), support="user", now=1.0)
    store.write_fact(Slot("k", "v", "first"), _unit(30), support="agent", now=2.0)
    store.write_fact(Slot("k", "v", "second"), _unit(31), support="agent", now=3.0)
    conts = store.contenders_for("k", "v")
    assert len(conts) == 1 and conts[0].value == "second"
    # the prior contender is retained as superseded history, not current/contested
    hist = [r for r in store.records_for("k", "v") if r.value == "first"]
    assert hist and hist[0].status == "superseded"


def test_contender_confirm_reinforces_same_value():
    store = CortexStore()
    store.write_fact(Slot("k", "v", "cur"), _unit(32), support="user", now=1.0)
    store.write_fact(Slot("k", "v", "alt"), _unit(33), confidence=0.5, support="agent", now=2.0)
    c0 = store.contenders_for("k", "v")[0].confidence
    for i in range(10):
        store.write_fact(Slot("k", "v", "alt"), _unit(33), support="agent", now=3.0 + i)
    conts = store.contenders_for("k", "v")
    assert len(conts) == 1 and conts[0].confidence > c0   # reinforced, still one


def test_unknown_tier_contests_known_but_known_supersedes_legacy_unknown():
    store = CortexStore()
    # known user fact, unknown-tier write -> contends
    store.write_fact(Slot("a", "b", "x"), _unit(34), support="user", now=1.0)
    r1 = store.write_fact(Slot("a", "b", "y"), _unit(35), now=2.0)        # no support
    assert r1.action == "contested"
    # legacy unknown fact, known agent write -> supersedes (1 >= 0)
    store.write_fact(Slot("c", "d", "x"), _unit(36), now=1.0)            # no support
    r2 = store.write_fact(Slot("c", "d", "y"), _unit(37), support="agent", now=2.0)
    assert r2.action == "superseded"
    assert store.lookup("c", "d").value == "y"


if __name__ == "__main__":
    import sys
    import traceback

    tests = sorted(
        (name, obj)
        for name, obj in dict(globals()).items()
        if name.startswith("test_") and callable(obj)
    )
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
