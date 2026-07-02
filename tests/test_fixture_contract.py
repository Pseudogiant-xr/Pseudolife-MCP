"""Fixture-vs-serializer contract (2026-07-02 review, Console M2/M3).

The devserver fixtures are hand-written parallel truths, and they drifted
from the real service in ways that shipped broken Console features
invisibly: the Stream "Explain ranking" drawer QA'd green against invented
keys (``band``/numeric ``candidates``/``kept``/``text``) while production
rendered "undefined" and "[object Object]". This pins the exact keys the
Stream view consumes against BOTH the real trace/serializer output and the
fixtures, so drift fails CI instead of QA.

No Postgres, no embedder — file-mode CMS with raw unit vectors.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from pseudolife_memory.service import _entry_to_dict
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.web.fixtures import FixtureService

# What views/stream.js actually reads.
_DRAWER_TIER_KEYS = {"name", "candidates"}
_DRAWER_CANDIDATE_KEYS = {"text_preview", "kept"}
_DRAWER_TOPK_KEYS = {"text_preview", "score"}
_ENTRY_CARD_KEYS = {"id", "text", "source", "bank", "tags", "superseded",
                    "access_count", "timestamp"}


def _unit(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return F.normalize(torch.randn(384, generator=g), dim=0)


def _real_trace() -> dict:
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.utils.config import MemoryConfig

    cfg = MemoryConfig()
    cfg.surprise_threshold = 0.0
    cms = ContinuumMemorySystem(cfg)
    cms.store("alpha probe entry", _unit(1), source="t")
    cms.store("beta probe entry", _unit(2), source="t")
    _result, trace = cms.retrieve_with_trace(
        _unit(1), top_k=2, query_text="alpha probe entry")
    return trace


def _assert_drawer_shape(trace: dict, who: str) -> None:
    tiers = trace["tiers"]
    assert tiers, f"{who}: no tiers"
    for ti in tiers:
        assert _DRAWER_TIER_KEYS <= set(ti), f"{who} tier keys: {sorted(ti)}"
        assert isinstance(ti["candidates"], list), (
            f"{who}: tier candidates must be a list of dicts")
        for c in ti["candidates"]:
            assert _DRAWER_CANDIDATE_KEYS <= set(c), (
                f"{who} candidate keys: {sorted(c)}")
    for r in trace["final_topk"]:
        assert _DRAWER_TOPK_KEYS <= set(r), f"{who} final_topk keys: {sorted(r)}"


def test_real_trace_matches_drawer_contract():
    _assert_drawer_shape(_real_trace(), "real cms trace")


def test_fixture_trace_matches_drawer_contract():
    fx = FixtureService().trace("alpha")["trace"]
    _assert_drawer_shape(fx, "FixtureService.trace")


def test_entry_dicts_match_entry_card_contract():
    real = _entry_to_dict(MemoryEntry(
        text="probe", embedding=torch.zeros(4), surprise_score=0.0,
        timestamp=1.0))
    assert _ENTRY_CARD_KEYS <= set(real), f"real entry keys: {sorted(real)}"

    fx_entries = FixtureService().search("pseudolife")["entries"]
    assert fx_entries
    assert _ENTRY_CARD_KEYS <= set(fx_entries[0]), (
        f"fixture entry keys: {sorted(fx_entries[0])}")
