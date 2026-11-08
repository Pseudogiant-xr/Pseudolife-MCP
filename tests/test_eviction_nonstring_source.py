"""A non-string source never blocks capacity eviction.

The Console's consolidate stores a raw JSON ``source`` as given, so a live
entry can carry a list or object source until the next restart reloads its
stored text. Eviction looked that source up in the weight table, and an
unhashable one raised ``TypeError: unhashable type`` inside every later
store into the full band (forced real-data run, daemon box, 2026-11-06:
Python failed 13 of ``console-consolidate-source``'s 15 writes, and Rust
answered 200 to the writes Python refused only this way). Rust weighs the
stored text, and no stored form of a non-string source names a weighted
tier, so it weighs 1.0.
"""
import time

import pytest
import torch

from pseudolife_memory.memory.miras.protocols import RetentionPolicy
from pseudolife_memory.memory.titans_memory import MemoryEntry

NON_STRING_SOURCES = [["user"], {"source": "user"}, [["a"], ["b", "c"]], True, 7, 3.5, None]


def _policy():
    return RetentionPolicy(weight_decay=0.0, decay_factor_on_contradiction=0.3,
                           eviction_score=lambda e, now: 0.0, name="t")


def _entry(source):
    return MemoryEntry(text="x", embedding=torch.zeros(4), source=source, timestamp=time.time())


@pytest.mark.parametrize("source", NON_STRING_SOURCES, ids=repr)
def test_a_non_string_source_weighs_like_an_unweighted_tier(source):
    policy, now = _policy(), time.time()
    assert policy.source_weighted_score(_entry(source), now) == \
        policy.source_weighted_score(_entry("unweighted"), now)


def test_a_weighted_string_source_keeps_its_weight():
    """The near miss: a real tier name still scales the score."""
    policy, now = _policy(), time.time()
    assert policy.source_weights["system"] != 1.0
    assert policy.source_weighted_score(_entry("system"), now) == policy.source_weights["system"]


def test_a_full_band_still_stores_after_a_list_source():
    from pseudolife_memory.memory.miras.band import build_band
    from pseudolife_memory.utils.config import MIRASBandSpec
    band = build_band(MIRASBandSpec(name="b", max_entries=2, retention_policy="balanced"),
                      embedding_dim=4, device="cpu")
    band.store(text="listed", embedding=torch.zeros(4), source=["user"])
    band.store(text="mapped", embedding=torch.zeros(4), source={"source": "user"})
    band.store(text="third", embedding=torch.zeros(4), source="user")  # evicts
    assert len(band.entries) == 2
    assert band.entries[-1].text == "third"
