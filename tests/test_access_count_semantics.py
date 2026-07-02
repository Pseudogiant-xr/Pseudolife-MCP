"""access_count semantics (2026-07-02 review fix).

Promotion, MTT retention, and eviction all key off ``access_count``, so it
must count *returned results*, not band-local retrieval candidates — a
candidate-level bump marks up to ``top_k`` entries per band per query
regardless of relevance, which corrupts all three downstream signals.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.miras.band import MIRASBand
from pseudolife_memory.memory.miras.retention import build_policy
from pseudolife_memory.utils.config import MemoryConfig


def _unit(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return F.normalize(torch.randn(384, generator=g), dim=0)


def _band() -> MIRASBand:
    return MIRASBand(
        name="t", embedding_dim=384, retention=build_policy("balanced"),
        max_entries=100, update_interval=1,
        promotion_access_count=2, promotion_surprise=0.5, device="cpu",
    )


def test_band_retrieve_does_not_bump_access_count():
    """Band-local top-k is a *candidate* set; it must not count as access."""
    b = _band()
    a = _unit(1)
    b.store("A", a, surprise=1.0)
    b.retrieve(a, top_k=1)
    assert b.entries[0].access_count == 0


def test_filtered_out_candidates_are_not_bumped():
    """A band-local candidate dropped by a result filter never reached the
    caller — it must not earn an access (the pre-fix behavior bumped it)."""
    cms = ContinuumMemorySystem(MemoryConfig())
    a = _unit(1)
    near = F.normalize(a + 0.05 * _unit(3), dim=0)
    cms.store("kept", a, source="t")
    cms.store("filtered", near, source="u")

    result = cms.retrieve(a, top_k=2, sources=["t"])

    assert [e.text for e in result.entries] == ["kept"]
    by_text = {e.text: e for band in cms.bands for e in band.entries}
    assert by_text["filtered"].access_count == 0
    assert by_text["kept"].access_count == 1


def test_cms_retrieve_bumps_only_returned_entries():
    """Only entries that survive the merged final top-k earn an access."""
    cms = ContinuumMemorySystem(MemoryConfig())
    a, z = _unit(1), _unit(2)
    cms.store("match me", a, source="t")
    cms.store("unrelated", z, source="t")

    result = cms.retrieve(a, top_k=1)

    assert [e.text for e in result.entries] == ["match me"]
    by_text = {e.text: e for band in cms.bands for e in band.entries}
    assert by_text["match me"].access_count == 1
    assert by_text["unrelated"].access_count == 0
