"""Unit tests for the CrossEncoderReranker.

We never load the real cross-encoder in the test suite — it'd add ~80MB
of network fetch on first run and 2-3 seconds of import time per
worker. Instead we monkeypatch ``sentence_transformers.CrossEncoder``
with a stub that returns deterministic scores driven by simple
keyword matching against the query.
"""

from __future__ import annotations

import math
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


class _StubCrossEncoder:
    """In-memory stand-in for ``sentence_transformers.CrossEncoder``.

    Scores each (query, candidate) pair by counting shared whitespace-
    split tokens, multiplied by 2 so scores span roughly [-1, +N] —
    a realistic range for a cross-encoder logit. Deterministic, no
    network, no model load.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.calls.append(list(pairs))
        out: list[float] = []
        for q, c in pairs:
            q_toks = set(q.lower().split())
            c_toks = set(c.lower().split())
            shared = q_toks & c_toks
            # Match -> positive logit, miss -> negative. The bias of -1
            # ensures candidates that share *no* tokens map to a sub-0.5
            # sigmoid, matching real cross-encoder behaviour for
            # off-topic candidates.
            out.append(2.0 * len(shared) - 1.0)
        return out


@pytest.fixture
def stub_ce(monkeypatch: pytest.MonkeyPatch) -> type[_StubCrossEncoder]:
    """Patch ``sentence_transformers.CrossEncoder`` for this test."""
    import sentence_transformers  # noqa: PLC0415
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", _StubCrossEncoder)
    return _StubCrossEncoder


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------


def test_invalid_fusion_weight_raises() -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    with pytest.raises(ValueError, match="fusion_weight"):
        CrossEncoderReranker(fusion_weight=1.5)
    with pytest.raises(ValueError, match="fusion_weight"):
        CrossEncoderReranker(fusion_weight=-0.1)


def test_invalid_top_n_raises() -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    with pytest.raises(ValueError, match="top_n"):
        CrossEncoderReranker(top_n=0)


def test_is_available_true_by_default() -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    assert r.is_available() is True


def test_is_available_false_after_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A load failure permanently disables the reranker."""
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    import sentence_transformers  # noqa: PLC0415

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no model for you")
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", _explode)

    r = CrossEncoderReranker()
    out = r.rerank("query", ["c1", "c2"])
    assert out == []
    assert r.is_available() is False


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------


def test_rerank_returns_one_score_per_candidate(stub_ce: type[_StubCrossEncoder]) -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    scores = r.rerank("the project uses python", ["python rocks", "java is fine", "unrelated"])
    assert len(scores) == 3


def test_rerank_scores_lie_in_unit_interval(stub_ce: type[_StubCrossEncoder]) -> None:
    """Sigmoid squashes raw logits into [0, 1] so fusion stays well-behaved."""
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    scores = r.rerank("the project uses python", ["python rocks", "totally unrelated"])
    for s in scores:
        assert 0.0 <= s <= 1.0


def test_rerank_promotes_semantically_relevant_candidate(stub_ce: type[_StubCrossEncoder]) -> None:
    """The relevant candidate should score higher than the off-topic one."""
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    scores = r.rerank(
        "what python testing framework is in use",
        [
            "we use pytest for python testing",  # 4 token overlaps
            "rust is a memory safe language",     # 0 overlaps
        ],
    )
    assert scores[0] > scores[1], (
        f"relevant candidate ({scores[0]:.3f}) should outscore "
        f"unrelated ({scores[1]:.3f})"
    )


def test_rerank_empty_candidates_returns_empty(stub_ce: type[_StubCrossEncoder]) -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    assert r.rerank("anything", []) == []


def test_rerank_empty_query_returns_empty(stub_ce: type[_StubCrossEncoder]) -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    assert r.rerank("   ", ["candidate"]) == []


def test_rerank_lazy_loads_model_only_on_first_call(stub_ce: type[_StubCrossEncoder]) -> None:
    """No HuggingFace fetch should happen at construction time."""
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    assert r._model is None  # noqa: SLF001 — testing lazy-load invariant.
    r.rerank("q", ["c"])
    assert r._model is not None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def test_fuse_blends_per_weight() -> None:
    """With fusion_weight=0.5 the fused score is the simple average."""
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker(fusion_weight=0.5)
    fused = r.fuse(originals=[0.4, 0.8], ce_scores=[0.9, 0.1])
    assert math.isclose(fused[0], 0.5 * 0.9 + 0.5 * 0.4)
    assert math.isclose(fused[1], 0.5 * 0.1 + 0.5 * 0.8)


def test_fuse_pure_reranker_with_weight_one() -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker(fusion_weight=1.0)
    fused = r.fuse(originals=[0.4, 0.8], ce_scores=[0.9, 0.1])
    assert fused == [0.9, 0.1]


def test_fuse_pure_original_with_weight_zero() -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker(fusion_weight=0.0)
    fused = r.fuse(originals=[0.4, 0.8], ce_scores=[0.9, 0.1])
    assert fused == [0.4, 0.8]


def test_fuse_passthrough_when_ce_empty() -> None:
    """Empty ce_scores (reranker failed) means originals pass through."""
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    fused = r.fuse(originals=[0.4, 0.8], ce_scores=[])
    assert fused == [0.4, 0.8]


def test_fuse_length_mismatch_raises() -> None:
    from pseudolife_memory.memory.reranker import CrossEncoderReranker  # noqa: PLC0415

    r = CrossEncoderReranker()
    with pytest.raises(ValueError, match="length mismatch"):
        r.fuse(originals=[0.4, 0.8, 0.5], ce_scores=[0.9, 0.1])
