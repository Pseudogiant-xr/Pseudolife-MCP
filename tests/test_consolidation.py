"""Consolidation clustering unit tests (Tier C C-6).

The clustering function is pure: given a list of ``(entry,
relevance_score)`` candidates and a cohesion threshold, group entries
into clusters of mutually-similar items. No I/O, no model calls — the
ML work is in the embeddings the caller already produced.

Tests exercise the behaviour-facing surface, not the algorithm's
internals. In particular: deterministic ordering (highest-relevance
seed first; clusters sorted by cohesion × size), cohesion threshold
acting as the cluster boundary, and graceful behaviour on empty /
singleton pools.
"""

from __future__ import annotations

import torch

from pseudolife_memory.memory.consolidation import (
    Cluster,
    cluster_candidates,
)
from pseudolife_memory.memory.titans_memory import MemoryEntry


def _entry(text: str, embedding: torch.Tensor) -> MemoryEntry:
    return MemoryEntry(text=text, embedding=embedding)


def _normalised(seed_dim: int = 8, *, seed: torch.Tensor | None = None) -> torch.Tensor:
    """Helper: random normalised vector (or jitter around ``seed``)."""
    if seed is not None:
        v = seed + 0.02 * torch.randn(seed_dim)
        return v / v.norm()
    v = torch.randn(seed_dim)
    return v / v.norm()


def test_empty_candidates_returns_empty_list() -> None:
    assert cluster_candidates([]) == []


def test_singleton_candidate_drops_below_min_cluster_size() -> None:
    e = _entry("only one", _normalised())
    assert cluster_candidates([(e, 1.0)], min_cluster_size=2) == []


def test_two_similar_candidates_form_one_cluster() -> None:
    torch.manual_seed(7)
    base = _normalised(8)
    e1 = _entry("a", _normalised(8, seed=base))
    e2 = _entry("b", _normalised(8, seed=base))
    clusters = cluster_candidates(
        [(e1, 0.9), (e2, 0.8)],
        min_cohesion=0.5,
        min_cluster_size=2,
    )
    assert len(clusters) == 1
    members = {m.text for m in clusters[0].members}
    assert members == {"a", "b"}
    assert isinstance(clusters[0].cohesion, float)
    assert clusters[0].cohesion > 0.5


def test_dissimilar_candidates_do_not_cluster() -> None:
    torch.manual_seed(11)
    e1 = _entry("alpha", _normalised(8))
    e2 = _entry("beta", _normalised(8))
    clusters = cluster_candidates(
        [(e1, 0.9), (e2, 0.8)],
        min_cohesion=0.95,  # near-identical required
        min_cluster_size=2,
    )
    # Two random unit vectors won't cluster under 0.95.
    assert clusters == []


def test_seed_is_highest_relevance_candidate() -> None:
    """The first unclustered candidate to seed a cluster is the
    highest-relevance one — that's what gives results predictable
    ordering for the calling LLM.
    """
    torch.manual_seed(13)
    base = _normalised(8)
    high = _entry("high", _normalised(8, seed=base))
    low = _entry("low", _normalised(8, seed=base))
    clusters = cluster_candidates(
        [(low, 0.4), (high, 0.95)],
        min_cohesion=0.5,
        min_cluster_size=2,
    )
    assert len(clusters) == 1
    assert clusters[0].members[0].text == "high"
    assert clusters[0].seed_score == 0.95


def test_clusters_sorted_by_cohesion_times_size_desc() -> None:
    """When there are multiple clusters, the one with the strongest
    cohesion × size product comes first — the LLM should look at the
    most-promising consolidation first.
    """
    torch.manual_seed(17)

    # Cluster A: three near-identical entries (high cohesion, size 3).
    base_a = _normalised(8)
    a_members = [
        _entry(f"a{i}", _normalised(8, seed=base_a)) for i in range(3)
    ]
    # Cluster B: two members, lower cohesion (more jitter).
    base_b = _normalised(8)

    def _jitter(seed: torch.Tensor) -> torch.Tensor:
        v = seed + 0.4 * torch.randn(8)
        return v / v.norm()

    b_members = [_entry(f"b{i}", _jitter(base_b)) for i in range(2)]

    pool = [
        (a_members[0], 0.95), (a_members[1], 0.92), (a_members[2], 0.90),
        (b_members[0], 0.85), (b_members[1], 0.80),
    ]
    clusters = cluster_candidates(pool, min_cohesion=0.6, min_cluster_size=2)
    assert len(clusters) >= 1  # at least the strong one survives
    first_texts = {m.text for m in clusters[0].members}
    # The first cluster must be the strong-A cluster, not the noisy-B.
    assert first_texts == {"a0", "a1", "a2"}


def test_max_clusters_caps_output_length() -> None:
    """Even a noisy pool with many possible micro-clusters returns at
    most ``max_clusters`` entries — keeps responses bounded for the LLM.
    """
    torch.manual_seed(19)

    # Make 5 distinct mini-clusters (each pair near-identical).
    pool = []
    score = 1.0
    for c in range(5):
        base = _normalised(8)
        for _ in range(2):
            e = _entry(f"c{c}-x", _normalised(8, seed=base))
            pool.append((e, score))
            score -= 0.01

    out = cluster_candidates(pool, min_cohesion=0.5, max_clusters=3)
    assert len(out) <= 3


def test_returns_cluster_dataclass() -> None:
    """API contract: each cluster is a :class:`Cluster` carrying members
    + cohesion + seed_score — what the MCP layer wraps into a JSON dict.
    """
    torch.manual_seed(23)
    base = _normalised(8)
    e1 = _entry("p", _normalised(8, seed=base))
    e2 = _entry("q", _normalised(8, seed=base))
    clusters = cluster_candidates(
        [(e1, 0.7), (e2, 0.6)],
        min_cohesion=0.4,
        min_cluster_size=2,
    )
    assert len(clusters) == 1
    c = clusters[0]
    assert isinstance(c, Cluster)
    assert isinstance(c.cohesion, float)
    assert isinstance(c.seed_score, float)
    assert len(c.members) == 2


def test_cohesion_for_single_member_cluster_is_undefined_so_dropped() -> None:
    """A cluster with one member has no internal pairs to compute
    cohesion over. Drop it via the ``min_cluster_size`` filter."""
    torch.manual_seed(29)
    e1 = _entry("alone", _normalised(8))
    e2 = _entry("far", _normalised(8))  # too far to cluster with e1
    clusters = cluster_candidates(
        [(e1, 0.9), (e2, 0.85)],
        min_cohesion=0.99,
        min_cluster_size=2,
    )
    assert clusters == []


def test_min_cohesion_acts_as_inclusive_threshold() -> None:
    """Cosine == min_cohesion should be considered in-cluster. Otherwise
    edge cases where two near-identical entries score exactly at the
    threshold would silently drop them."""
    torch.manual_seed(31)
    dim = 8
    base = torch.ones(dim) / (dim ** 0.5)
    # Two entries identical to base → cosine = 1.0, well above any
    # threshold we'd reasonably set.
    e1 = _entry("x", base.clone())
    e2 = _entry("y", base.clone())
    clusters = cluster_candidates(
        [(e1, 0.9), (e2, 0.8)],
        min_cohesion=1.0,
        min_cluster_size=2,
    )
    assert len(clusters) == 1
