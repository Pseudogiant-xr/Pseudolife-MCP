"""v0.5 cosine band: novelty surprise + pure-cosine retrieve + no MLP."""

from __future__ import annotations

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.miras.band import MIRASBand
from pseudolife_memory.memory.miras.retention import build_policy


def _band(max_entries: int = 100) -> MIRASBand:
    return MIRASBand(
        name="t", embedding_dim=384, retention=build_policy("balanced"),
        max_entries=max_entries, update_interval=1,
        promotion_access_count=2, promotion_surprise=0.5, device="cpu",
    )


def _unit(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return F.normalize(torch.randn(384, generator=g), dim=0)


def test_compute_surprise_is_novelty():
    b = _band()
    e = _unit(1)
    assert b.compute_surprise(e) == 1.0          # empty band -> max novelty
    b.store("x", e, source="t", surprise=1.0)
    assert b.compute_surprise(e) < 0.05          # exact duplicate -> ~0
    assert b.compute_surprise(_unit(2)) > 0.3    # unrelated vector -> high


def test_retrieve_is_pure_cosine():
    b = _band()
    a, z = _unit(1), _unit(2)
    b.store("A", a, surprise=1.0)
    b.store("Z", z, surprise=1.0)
    res = b.retrieve(a, top_k=1)
    assert res.entries[0].text == "A"
    assert res.scores[0] > 0.99                  # cosine of a vector with itself


def test_store_does_no_training():
    b = _band()
    assert not hasattr(b, "memory")
    assert not hasattr(b, "update_rule")
    assert not hasattr(b, "objective")
    b.store("x", _unit(1), surprise=1.0)         # must not train / raise
    assert b.size == 1


def test_superseded_entries_evict_before_their_corrections():
    """2026-07-02 review fix: a correction arrives with near-zero surprise
    (it is semantically near-identical to what it replaces) while the
    superseded predecessor keeps a decayed-but-larger surprise, so
    surprise-driven eviction destroyed the correction and kept the stale
    fact. Superseded entries must always be the cheaper eviction."""
    import time

    b = _band(max_entries=2)
    b.store("old fact", _unit(1), surprise=0.9)
    b.store("correction", _unit(2), surprise=0.05)
    b.entries[0].superseded_at = time.time()      # "old fact" now stale

    b.store("newcomer", _unit(3), surprise=0.5)   # capacity → evict one

    texts = [e.text for e in b.entries]
    assert "old fact" not in texts
    assert "correction" in texts
