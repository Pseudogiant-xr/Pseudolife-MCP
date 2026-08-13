"""Slot-token inverted index (Pool 1.5 candidate gathering, 2026-07-12 perf fix).

``_slot_query_pool`` used to scan every entry in every band on every
``query_text`` search. It now looks candidates up in a token ->
(ordinal, containing band, entry) index. The maintenance contract these
tests pin:

* **store** extends the index in place (a new entry only ever ADDS
  tokens) — no rebuild, and a slotless store leaves it untouched;
* **removals** (capacity eviction, delete, promotion/consolidation,
  clear) flag it dirty for a lazy full rebuild;
* **wholesale entry replacement** (``load`` / ``hydrate_cms``) also
  flags it dirty — these paths bypass ``store`` entirely;
* **band filtering** keys on the band that CONTAINS the entry (matching
  the pre-index full-scan semantics), not the entry's ``bank`` stamp,
  which can go stale when a preset change re-routes hydrated rows.

Each invalidation test builds the index once (a call that would
otherwise happen inside a real ``retrieve(query_text=...)``), performs a
mutation, and asserts the *next* query reflects it rather than serving
stale cached entries.

Real ``ContinuumMemorySystem`` + real ``extract_slots`` (deterministic,
no ML) with synthetic embeddings — no sentence-transformers model needed
for these (mirrors ``tests/test_tag_filters.py``).
"""
from __future__ import annotations

import random
import time

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.utils.config import (
    MemoryConfig, MIRASBandSpec, MIRASConfig,
)


def _fresh_cms() -> ContinuumMemorySystem:
    cfg = MemoryConfig()
    cfg.surprise_threshold = -1.0  # disable the surprise gate
    return ContinuumMemorySystem(cfg)


def _hit_texts(cms: ContinuumMemorySystem, query_text: str, **kw) -> set[str]:
    hits = cms._slot_query_pool(query_text=query_text, k=5, seen_texts=set(), **kw)
    return {e.text for e, _score, _surprise in hits}


def test_slot_pool_matches_basic_query() -> None:
    cms = _fresh_cms()
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    assert "I have a Ragdoll cat named Jacque" in _hit_texts(
        cms, "do I have a cat named Jacque?",
    )


def test_slot_pool_finds_entry_stored_after_index_already_built() -> None:
    cms = _fresh_cms()
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # force-build the index while only Jacque exists
    cms.store("I have a Siamese cat named Miso", torch.randn(dim), source="user")
    assert "I have a Siamese cat named Miso" in _hit_texts(cms, "Miso")


def _pin_to_band0(cms: ContinuumMemorySystem) -> None:
    """Stop band[0] promoting its contents away.

    The default preset promotes a fresh entry out of ``working`` on the very
    next store (surprise=1.0 on an empty band beats the 0.5 threshold),
    which would leave band[0] empty and make a capacity eviction a no-op.
    """
    cms.bands[0].promotion_surprise = 2.0
    cms.bands[0].promotion_access_count = 10**9


def _two_band_cms(head_cap: int) -> ContinuumMemorySystem:
    """Two bands, consolidation disabled — so capacity eviction is the ONLY
    thing that can move an entry, and the only path that can invalidate the
    index. With the default preset a cascading promotion fires on the same
    tick and marks the index dirty anyway, which masks a missing eviction
    hook (verified: the hook can be deleted and the test still passes)."""
    cfg = MemoryConfig()
    cfg.surprise_threshold = -1.0
    cfg.miras = MIRASConfig(preset="custom", bands=[
        MIRASBandSpec(name="head", max_entries=head_cap, update_interval=10**9,
                      promotion_access_count=10**9, promotion_surprise=2.0),
        MIRASBandSpec(name="tail", max_entries=10, update_interval=10**9,
                      promotion_access_count=10**9, promotion_surprise=2.0),
    ])
    return ContinuumMemorySystem(cfg)


def test_slot_pool_follows_a_demoted_entry_to_its_new_band() -> None:
    """Capacity eviction demotes rather than destroys (2026-07-25), so the
    entry stays findable — but band filtering keys on the band that
    CONTAINS it, so the index must be rebuilt against its new home."""
    cms = _two_band_cms(head_cap=1)
    dim = cms.config.embedding_dim
    jacque = "I have a Ragdoll cat named Jacque"
    cms.store(jacque, torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # force-build the index while Jacque is in head
    cms.store("I have a bicycle named Rocket", torch.randn(dim), source="user")

    # Demoted, not deleted: still in the store, and reachable under its new
    # band. A stale index would answer under "head" and miss under "tail".
    assert [e.text for e in cms.bands[1].entries] == [jacque]
    assert jacque in _hit_texts(cms, "Jacque", band_filter={"tail"})
    assert _hit_texts(cms, "Jacque", band_filter={"head"}) == set()


def test_slot_pool_excludes_entry_evicted_from_the_deepest_band() -> None:
    """Overflow past the LAST band is a real drop — total capacity is still
    a bound — and the dropped entry must leave the index."""
    cfg = MemoryConfig()
    cfg.surprise_threshold = -1.0
    cfg.miras = MIRASConfig(preset="custom", bands=[
        MIRASBandSpec(name="only", max_entries=1, update_interval=10**9,
                      promotion_access_count=10**9, promotion_surprise=2.0),
    ])
    cms = ContinuumMemorySystem(cfg)
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # force-build the index while Jacque is live
    cms.store("I have a bicycle named Rocket", torch.randn(dim), source="user")

    assert _hit_texts(cms, "Jacque") == set()


def test_slot_pool_excludes_deleted_entry() -> None:
    cms = _fresh_cms()
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # force-build the index while Jacque is live
    cms.delete_entries(text="I have a Ragdoll cat named Jacque")
    assert _hit_texts(cms, "Jacque") == set()


def test_slot_pool_empty_after_clear() -> None:
    cms = _fresh_cms()
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # force-build the index while Jacque is live
    cms.clear()
    assert _hit_texts(cms, "Jacque") == set()


def _pin_to_band0(cms: ContinuumMemorySystem) -> None:
    """Disable auto-promotion out of band[0] (the default preset promotes a
    fresh entry on the very next store — surprise=1.0 on an empty band)."""
    cms.bands[0].promotion_surprise = 2.0
    cms.bands[0].promotion_access_count = 10**9


def _find_entry(cms: ContinuumMemorySystem, text: str) -> MemoryEntry | None:
    for band in cms.bands:
        for e in band.entries:
            if e.text == text:
                return e
    return None


def test_slotless_store_leaves_live_index_clean() -> None:
    """Most stores carry no slots (extraction is precision-gated) — they
    must not force a rebuild, or the interleaved store/search workload
    rebuilds on nearly every search."""
    cms = _fresh_cms()
    _pin_to_band0(cms)
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # build the index
    assert cms._slot_index_dirty is False
    stored, _ = cms.store(
        "the weather was pleasant during the walk yesterday",
        torch.randn(dim), source="user",
    )
    assert stored, "test setup invalid: slotless entry was not stored"
    e = _find_entry(cms, "the weather was pleasant during the walk yesterday")
    assert e is not None and e.slots == [], (
        "test setup invalid: expected a slotless entry")
    assert cms._slot_index_dirty is False
    assert "I have a Ragdoll cat named Jacque" in _hit_texts(cms, "Jacque")


def test_slotted_store_extends_live_index_without_rebuild() -> None:
    cms = _fresh_cms()
    _pin_to_band0(cms)
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # build the index
    assert cms._slot_index_dirty is False
    cms.store("I have a Siamese cat named Miso", torch.randn(dim), source="user")
    assert cms._slot_index_dirty is False   # extended in place, not flagged
    assert "I have a Siamese cat named Miso" in _hit_texts(cms, "Miso")


def test_load_replaces_index_contents(tmp_path) -> None:
    """``load`` swaps band entries wholesale (bypassing store) — a
    previously-built index must not keep serving the old bank."""
    dim = MemoryConfig().embedding_dim
    cms1 = _fresh_cms()
    cms1.store("I have a Siamese cat named Miso", torch.randn(dim), source="user")
    cms1.save(tmp_path)

    cms2 = _fresh_cms()
    cms2.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms2, "Jacque")  # build the index on the pre-load bank
    cms2.load(tmp_path)
    assert "I have a Siamese cat named Miso" in _hit_texts(cms2, "Miso")
    assert _hit_texts(cms2, "Jacque") == set()


class _StubStorage:
    """Just enough of PostgresStorage for hydrate_cms."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def load_entries(self) -> list[dict]:
        return self._rows

    def load_episodes(self) -> list[dict]:
        return []


def _entry_row(rid: int, text: str, band: str, slots: list[list[str]],
               dim: int) -> dict:
    return {
        "id": rid, "band": band, "text": text,
        "embedding": torch.randn(dim), "surprise": 0.5, "ts": time.time(),
        "access_count": 0, "source": "user", "superseded_at": None,
        "superseded_by_text": None, "last_logical_turn": None,
        "episode_id": None, "episode_title": None, "tags": [],
        "slots": slots, "reinforcements": 0,
    }


def test_hydrate_cms_invalidates_built_index() -> None:
    """Hydration appends to band lists directly (bypassing store) — it
    must flag the index dirty like it already flags band._dirty."""
    from pseudolife_memory.storage.sync import hydrate_cms

    cms = _fresh_cms()
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # build the index pre-hydration
    hydrate_cms(cms, _StubStorage([
        _entry_row(1, "I have a Siamese cat named Miso", cms.bands[0].name,
                   [["Miso", "type", "cat", "+"]], dim),
    ]))
    assert "I have a Siamese cat named Miso" in _hit_texts(cms, "Miso")


def test_band_filter_matches_containing_band_not_bank_stamp() -> None:
    """A preset change makes hydrate_cms re-route rows whose band no longer
    exists into band[0] while the entry keeps its old ``bank`` stamp. Band
    filtering must key on the band that actually holds the entry (the
    pre-index full-scan semantics), in both directions."""
    cms = _fresh_cms()
    dim = cms.config.embedding_dim
    e = MemoryEntry(
        text="zanthar timeout fact",
        embedding=torch.randn(dim),
        source="user",
        bank="defunct-band",
        slots=[("zanthar build system", "default timeout", "4500 seconds", "+")],
    )
    cms.bands[0].entries.append(e)
    cms.bands[0]._dirty = True
    cms._slot_index_dirty = True
    assert "zanthar timeout fact" in _hit_texts(
        cms, "zanthar timeout", band_filter={cms.bands[0].name})
    assert _hit_texts(
        cms, "zanthar timeout", band_filter={"defunct-band"}) == set()


def test_slot_pool_finds_promoted_entry_with_band_filter() -> None:
    cms = _fresh_cms()
    # Pin Jacque to band[0] first (see test_slot_pool_excludes_evicted_entry
    # for why: the default preset would otherwise auto-promote it out of
    # "working" on this very store, before the index is even built,
    # making the explicit _consolidate call below a no-op).
    cms.bands[0].promotion_surprise = 2.0
    cms.bands[0].promotion_access_count = 10**9
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    assert cms.bands[0].entries and cms.bands[0].entries[0].text == (
        "I have a Ragdoll cat named Jacque"
    ), "test setup invalid: Jacque did not stay in band[0] as expected"
    _hit_texts(cms, "Jacque")  # force-build the index pre-promotion
    cms.bands[0].promotion_surprise = -1.0  # now guarantee promotion
    cms._consolidate(0, 1)
    assert not cms.bands[0].entries, "test setup invalid: promotion did not fire"
    dest_name = cms.bands[1].name
    assert "I have a Ragdoll cat named Jacque" in _hit_texts(
        cms, "Jacque", band_filter={dest_name},
    )


# ---------------------------------------------------------------------------
# Shadow verification (2026-08-13). A sampled read recomputes the index
# from the band entries and compares MEMBERSHIP against the live copy —
# the runtime tripwire for mutation paths that neither extend the index
# nor flag it dirty (the bug class the 2026-07-12 audit found three of).
# ---------------------------------------------------------------------------


def _shadow_cms(rate: float) -> ContinuumMemorySystem:
    cms = _fresh_cms()
    cms.config.slot_index_shadow_rate = rate
    _pin_to_band0(cms)  # keep stores extend-in-place (no promotion dirtying)
    return cms


def _rogue_entry(dim: int) -> MemoryEntry:
    return MemoryEntry(
        text="zanthar build system times out after 4500 seconds",
        embedding=torch.randn(dim),
        source="user",
        slots=[("zanthar build system", "default timeout", "4500 seconds", "+")],
    )


def test_shadow_check_repairs_stale_entry_after_bypassing_removal() -> None:
    """A removal that skips the maintenance contract leaves the index
    serving a ghost; the shadow check must catch and repair it."""
    cms = _shadow_cms(rate=1.0)
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    cms.store("I have a Siamese cat named Miso", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # build the index
    assert not cms._slot_index_dirty, "test setup invalid: index went dirty"
    # Bypass every maintenance hook: drop Jacque straight out of the band
    # lists — no store(), no dirty flag.
    for band in cms.bands:
        band.entries[:] = [e for e in band.entries if "Jacque" not in e.text]
    assert "I have a Ragdoll cat named Jacque" not in _hit_texts(cms, "Jacque")
    assert cms.stats()["slot_index_shadow_divergences"] == 1
    # The repair adopted the fresh copy: the next query diverges no further.
    _hit_texts(cms, "Jacque")
    assert cms.stats()["slot_index_shadow_divergences"] == 1


def test_shadow_check_catches_entry_added_behind_the_index() -> None:
    """An entry appended directly to a band (a hydrate-style bypass) is
    invisible to the live index; the shadow check must surface it."""
    cms = _shadow_cms(rate=1.0)
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # build the index
    assert not cms._slot_index_dirty, "test setup invalid: index went dirty"
    cms.bands[0].entries.append(_rogue_entry(dim))
    assert "zanthar build system times out after 4500 seconds" in _hit_texts(
        cms, "zanthar timeout",
    )
    assert cms.stats()["slot_index_shadow_divergences"] == 1


def test_shadow_rate_zero_leaves_stale_index_alone() -> None:
    """rate=0.0 disables the check entirely — the poisoned index keeps
    serving the ghost (this pins that the hook above is load-bearing)."""
    cms = _shadow_cms(rate=0.0)
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")
    for band in cms.bands:
        band.entries[:] = [e for e in band.entries if "Jacque" not in e.text]
    assert "I have a Ragdoll cat named Jacque" in _hit_texts(cms, "Jacque")
    assert cms.stats()["slot_index_shadow_divergences"] == 0


def _entry_ordinals(
    ix: dict[str, list[tuple[int, str, object]]],
) -> dict[int, int]:
    return {id(e): o for items in ix.values() for (o, _b, e) in items}


def test_shadow_check_no_false_positive_from_extend_in_place() -> None:
    """Extend-in-place ordinals legitimately differ from a rebuild's
    band-then-insertion renumbering; the membership comparison must not
    flag that. Setup mirrors the production shape where they actually
    diverge: a slotted entry seated in a deeper band takes a LOW ordinal
    at rebuild, then a later in-place store into band[0] takes the next
    counter value — while a fresh walk would renumber band[0] first."""
    cms = _shadow_cms(rate=1.0)
    dim = cms.config.embedding_dim
    deep = MemoryEntry(
        text="zanthar deploy gate needs two approvals",
        embedding=torch.randn(dim),
        source="user",
        slots=[("zanthar deploy gate", "approvals", "two", "+")],
    )
    cms.bands[1].entries.append(deep)
    cms._slot_index_dirty = True
    _hit_texts(cms, "zanthar")  # rebuild: the deep entry takes ordinal 0
    cms.store("I have a Siamese cat named Miso", torch.randn(dim), source="user")
    assert not cms._slot_index_dirty, "test setup invalid: store dirtied index"
    # Load-bearing setup check: the live ordinals must genuinely differ
    # from a fresh walk's, or this test degenerates and would pass even
    # if the comparison wrongly included ordinals.
    fresh_ix, _ = cms._compute_slot_index()
    assert _entry_ordinals(cms._slot_token_index) != _entry_ordinals(fresh_ix), (
        "test setup invalid: extend-in-place ordinals match a fresh rebuild"
    )
    for _ in range(3):
        assert "I have a Siamese cat named Miso" in _hit_texts(cms, "Miso")
    assert cms.stats()["slot_index_shadow_divergences"] == 0


def test_shadow_rate_default_is_on() -> None:
    """The shipped default keeps the tripwire live (CHANGELOG: 0.01)."""
    assert MemoryConfig().slot_index_shadow_rate == 0.01


def test_shadow_check_samples_at_default_rate(monkeypatch) -> None:
    """Default config plus a forced sample: the check must fire without
    the rate ever being set explicitly — pins that the gate reads the
    real config field (a field rename or a zeroed default fails here)."""
    cms = _fresh_cms()
    _pin_to_band0(cms)
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")
    for band in cms.bands:
        band.entries[:] = [e for e in band.entries if "Jacque" not in e.text]
    monkeypatch.setattr(cms._shadow_rng, "random", lambda: 0.0)
    assert "I have a Ragdoll cat named Jacque" not in _hit_texts(cms, "Jacque")
    assert cms.stats()["slot_index_shadow_divergences"] == 1


def test_shadow_sampler_does_not_touch_global_rng() -> None:
    """The sampler draws from a dedicated Random instance — a consumer
    that seeds the module-global ``random`` for reproducibility must see
    an unperturbed stream regardless of how many queries sample."""
    cms = _shadow_cms(rate=0.5)
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # build the index
    random.seed(1234)
    expected = random.Random(1234).random()
    for _ in range(20):
        _hit_texts(cms, "Jacque")
    assert random.random() == expected
