"""Slot-token inverted index (Pool 1.5 candidate gathering, 2026-07-12 perf fix).

``_slot_query_pool`` used to scan every entry in every band on every
``query_text`` search. It now looks candidates up in a token -> entries
index built lazily and invalidated on writes. These tests pin the
invalidation contract across every mutation path that touches band
entries: store, capacity eviction, delete, promotion/consolidation, and
clear. Each test builds the index once (a call that would otherwise
happen inside a real ``retrieve(query_text=...)``), performs a mutation,
and asserts the *next* query reflects it rather than serving stale
cached entries.

Real ``ContinuumMemorySystem`` + real ``extract_slots`` (deterministic,
no ML) with synthetic embeddings — no sentence-transformers model needed
for these (mirrors ``tests/test_tag_filters.py``).
"""
from __future__ import annotations

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
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
