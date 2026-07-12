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

import time

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.utils.config import MemoryConfig


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


def test_slot_pool_excludes_evicted_entry() -> None:
    cms = _fresh_cms()
    cms.bands[0].max_entries = 1
    # Pin Jacque to band[0] — the default preset promotes a fresh entry
    # out of "working" on the very next store (surprise=1.0 on an empty
    # band beats the 0.5 default threshold), which would leave band[0]
    # empty and make the eviction below a no-op.
    cms.bands[0].promotion_surprise = 2.0
    cms.bands[0].promotion_access_count = 10**9
    dim = cms.config.embedding_dim
    cms.store("I have a Ragdoll cat named Jacque", torch.randn(dim), source="user")
    _hit_texts(cms, "Jacque")  # force-build the index while Jacque is live
    cms.store("I have a bicycle named Rocket", torch.randn(dim), source="user")
    # band[0].max_entries=1 forces the Jacque entry out on the second store.
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
