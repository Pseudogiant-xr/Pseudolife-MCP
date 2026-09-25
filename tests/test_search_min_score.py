"""The dense relevance floor is a config knob, not a literal in cms.py.

``cms.retrieve`` gated the dense pool on a bare ``MIN_SCORE = 0.25`` from
the initial release, set for the old MiniLM embedder and never revisited
after the 2026-07-28 switch to Qwen3-Embedding-0.6B. It now reads
``memory.search.min_score`` (default 0.25, so nothing moves), and a
per-call ``min_score`` still overrides it. What a configured floor must
NOT change is the explicit-floor contract: only a caller's own floor
bounds the slot and lexical injections, which carry their own scales.
"""

from __future__ import annotations

import math
from pathlib import Path

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import MemoryConfig, SearchConfig, load_config

DIM = 16


def _vec(cos: float) -> torch.Tensor:
    v = torch.zeros(DIM)
    v[0] = cos
    v[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


def _query() -> torch.Tensor:
    v = torch.zeros(DIM)
    v[0] = 1.0
    return v


def _cms(rows, **search_kwargs) -> ContinuumMemorySystem:
    cfg = MemoryConfig(embedding_dim=DIM)
    for key, value in search_kwargs.items():
        setattr(cfg.search, key, value)
    cms = ContinuumMemorySystem(cfg)
    for text, cos in rows:
        cms.store(text, _vec(cos), source="user")
    return cms


PLAIN = [("alpha note about the schedule", 0.90),
         ("beta note about the schedule", 0.45),
         ("gamma note about the schedule", 0.30)]

# The only slot-bearing row sits far below every dense floor, so it can
# reach the result only through the slot channel, at that channel's own
# 0.55-0.95 confidence scale (0.666667 here).
MIXED = [("the gateway rollout owner is the platform team", 0.95),
         ("deployment note eta about the pipeline schedule", 0.62),
         ("I have a Ragdoll cat named Jacque", 0.30)]
MIXED_QUERY = "who owns the gateway rollout and what breed is Jacque"
SLOT_HIT = "I have a Ragdoll cat named Jacque"


def test_the_floor_ships_at_its_initial_release_value(tmp_path: Path):
    assert SearchConfig().min_score == 0.25
    p = tmp_path / "config.yaml"
    p.write_text("memory:\n  search:\n    min_score: 0.4\n", encoding="utf-8")
    assert load_config(p).memory.search.min_score == 0.4
    p.write_text("memory:\n  search:\n    fusion: weighted_sum\n",
                 encoding="utf-8")
    assert load_config(p).memory.search.min_score == 0.25


def test_the_configured_floor_gates_the_dense_pool():
    default = _cms(PLAIN).retrieve(_query(), top_k=3)
    assert len(default.entries) == 3
    assert default.params["min_score"] == 0.25
    res = _cms(PLAIN, min_score=0.5).retrieve(_query(), top_k=3)
    assert [e.text for e in res.entries] == ["alpha note about the schedule"]
    assert res.params["min_score"] == 0.5
    assert res.params["min_score_explicit"] is False


def test_a_per_call_floor_still_overrides_the_configured_one():
    res = _cms(PLAIN, min_score=0.5).retrieve(_query(), top_k=3,
                                              min_score=0.2)
    assert len(res.entries) == 3
    assert res.params["min_score"] == 0.2
    assert res.params["min_score_explicit"] is True


def test_only_a_callers_floor_bounds_the_slot_channel():
    configured = _cms(MIXED, min_score=0.7).retrieve(
        _query(), top_k=4, query_text=MIXED_QUERY)
    texts = [e.text for e in configured.entries]
    assert SLOT_HIT in texts                      # 0.67 slot hit survives
    assert "deployment note eta about the pipeline schedule" not in texts
    explicit = _cms(MIXED).retrieve(
        _query(), top_k=4, query_text=MIXED_QUERY, min_score=0.7)
    assert SLOT_HIT not in [e.text for e in explicit.entries]
