"""Cross-encoder margin gate (2026-07-12 perf work).

The reranker exists to fix *ambiguous* orderings — when the bi-encoder
already separates the top candidates decisively, a ~200ms cross-encoder
pass can only reshuffle a ranking that wasn't in doubt. The gate:

* ``RerankerConfig.skip_margin`` (default ``0.0`` = gate off, exact old
  behavior);
* when > 0, the reranker is skipped iff the gap between the two best
  bi-encoder-adjusted scores in the head is >= skip_margin;
* a single-candidate head is trivially unambiguous (nothing to reorder)
  and is skipped whenever the gate is enabled;
* a skip is visible in the retrieval trace as
  ``reranker.reason == "unambiguous_margin"``.

The margin must be computed over the SORTED head scores — the head is
``neural + reference`` concatenated, which is not globally sorted, so
``head[0] - head[1]`` on raw positions would measure garbage.

Real ContinuumMemorySystem + synthetic embeddings + a stub reranker
(mirrors tests/test_slot_query_index.py — no models loaded).
"""
from __future__ import annotations

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import MemoryConfig


class _StubReranker:
    """Counts rerank calls; scores are irrelevant to the gate tests."""

    def __init__(self) -> None:
        self.rerank_calls = 0

    def is_available(self) -> bool:
        return True

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.rerank_calls += 1
        return [0.9] * len(candidates)

    def fuse(self, originals: list[float], ce_scores: list[float]) -> list[float]:
        if not ce_scores:
            return list(originals)
        return [0.7 * ce + 0.3 * orig for orig, ce in zip(originals, ce_scores)]


def _cms_with_stub(skip_margin: float) -> tuple[ContinuumMemorySystem, _StubReranker]:
    cfg = MemoryConfig()
    cfg.surprise_threshold = -1.0
    cfg.reranker.enabled = True
    cfg.reranker.skip_margin = skip_margin
    stub = _StubReranker()
    cms = ContinuumMemorySystem(cfg, reranker=stub)
    return cms, stub


def _query(cms: ContinuumMemorySystem, emb: torch.Tensor) -> dict:
    _result, trace = cms.retrieve_with_trace(
        emb, top_k=5, query_text="which fruit was mentioned?",
    )
    return trace


def _unit(v: torch.Tensor) -> torch.Tensor:
    return v / v.norm()


def test_default_margin_zero_always_fires() -> None:
    cms, stub = _cms_with_stub(skip_margin=0.0)
    dim = cms.config.embedding_dim
    q = _unit(torch.randn(dim))
    ortho = _unit(torch.randn(dim))
    ortho = _unit(ortho - (ortho @ q) * q)  # exactly orthogonal to q
    cms.store("clear winner about apples", q.clone(), source="user")
    cms.store("orthogonal note about xylophones", ortho, source="user")
    trace = _query(cms, q)
    assert stub.rerank_calls == 1
    assert trace["reranker"]["fired"] is True


def test_wide_margin_skips_reranker() -> None:
    cms, stub = _cms_with_stub(skip_margin=0.3)
    dim = cms.config.embedding_dim
    q = _unit(torch.randn(dim))
    ortho = _unit(torch.randn(dim))
    ortho = _unit(ortho - (ortho @ q) * q)
    cms.store("clear winner about apples", q.clone(), source="user")
    cms.store("orthogonal note about xylophones", ortho, source="user")
    trace = _query(cms, q)
    assert stub.rerank_calls == 0, (
        "top-2 cosines are ~1.0 vs ~0.0 — decisively separated, the "
        "cross-encoder pass is wasted latency")
    assert trace["reranker"]["fired"] is False
    assert trace["reranker"]["reason"] == "unambiguous_margin"


def test_narrow_margin_fires_reranker() -> None:
    cms, stub = _cms_with_stub(skip_margin=0.3)
    dim = cms.config.embedding_dim
    q = _unit(torch.randn(dim))
    cms.store("first near-tie about apples", q.clone(), source="user")
    cms.store("second near-tie about pears", q.clone(), source="user")
    trace = _query(cms, q)
    assert stub.rerank_calls == 1, (
        "identical embeddings -> zero margin -> exactly the ambiguity "
        "the cross-encoder exists to resolve")
    assert trace["reranker"]["fired"] is True


def test_single_candidate_is_trivially_unambiguous() -> None:
    cms, stub = _cms_with_stub(skip_margin=0.3)
    dim = cms.config.embedding_dim
    q = _unit(torch.randn(dim))
    cms.store("the only memory about apples", q.clone(), source="user")
    trace = _query(cms, q)
    assert stub.rerank_calls == 0
    assert trace["reranker"]["fired"] is False
    assert trace["reranker"]["reason"] == "unambiguous_margin"
