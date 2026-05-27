"""Cross-encoder reranker for the merged retrieval pool (Tier B).

The CMS bi-encoder pipeline (dense MiniLM-L6 cosine + slot-graph + chain
residual) is cheap and good at coarse recall, but it bi-encodes query and
candidate independently — surface-token noise and near-duplicates can
shuffle ranks in pathological ways. A cross-encoder attends over
(query, candidate) jointly, producing a relevance score per pair that
generally outperforms bi-encoder scoring on standard IR benchmarks
(MS MARCO, BEIR) by 5-15 NDCG points.

We use ``cross-encoder/ms-marco-MiniLM-L-6-v2`` — a 22M-parameter
distilled MiniLM trained on MS MARCO passage ranking. ~80MB, fetched
from the HuggingFace Hub on first use. Reasonably fast on CPU
(~10ms / pair on a modern x86) so reranking the top-20 candidates
costs ~200ms wall-clock added to a search.

Design notes
------------
* **Fail-soft.** If the model fails to load (no network, disk full,
  hub down) the reranker disables itself silently and the caller falls
  back to the bi-encoder score. Memory operations never break because
  of an optional model.
* **Lazy load.** The model is imported and instantiated on the first
  ``rerank()`` call, not at ``__init__``. Keeps service startup fast
  for installs that never enable the reranker.
* **Score scale.** Cross-encoder logits are unbounded (typically -10
  to +10 on MS MARCO). We squash with ``sigmoid`` so the fused score
  has a well-defined [0, 1] range that composes cleanly with the
  bi-encoder's already-normalised cosine score.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _sigmoid(x: float) -> float:
    """Numerically stable scalar sigmoid."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class CrossEncoderReranker:
    """Optional cross-encoder reranker for the top-N retrieval candidates.

    Parameters
    ----------
    model_name:
        HuggingFace model id. Default ``cross-encoder/ms-marco-MiniLM-L-6-v2``
        is the canonical small MS MARCO reranker (~80MB, 22M params).
    fusion_weight:
        Mixing coefficient in ``final = fusion_weight * sigmoid(ce) +
        (1 - fusion_weight) * original``. 0.0 disables the reranker;
        1.0 ignores the bi-encoder score entirely.
    top_n:
        Number of candidates to rerank. Candidates beyond top_n keep
        their bi-encoder score (so they can still appear in the result
        set if the reranker downranks the top-N below them).

    Lifecycle
    ---------
    * ``is_available()``: returns False if the model failed to load.
    * ``rerank(query, candidates)``: returns one squashed score per
      candidate. Returns an empty list on failure.
    * ``fuse(originals, ce_scores)``: applies the weighted-sum
      fusion. Pure function — no model access.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        fusion_weight: float = 0.7,
        top_n: int = 20,
    ) -> None:
        if not 0.0 <= fusion_weight <= 1.0:
            raise ValueError(
                f"fusion_weight must be in [0, 1], got {fusion_weight!r}",
            )
        if top_n < 1:
            raise ValueError(f"top_n must be >= 1, got {top_n!r}")
        self.model_name = model_name
        self.fusion_weight = float(fusion_weight)
        self.top_n = int(top_n)
        self._model = None        # CrossEncoder, lazy-loaded.
        self._disabled = False    # Set on any load failure.

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the reranker hasn't been disabled by load failure.

        Doesn't actually load the model — just reports whether
        :meth:`rerank` is worth calling. The model loads lazily on the
        first ``rerank()``.
        """
        return not self._disabled

    def _ensure_loaded(self) -> bool:
        """Load the CrossEncoder lazily. Return True on success."""
        if self._disabled:
            return False
        if self._model is not None:
            return True
        try:
            from sentence_transformers import CrossEncoder  # noqa: PLC0415
            logger.info(
                "Loading cross-encoder reranker %r (first use)...",
                self.model_name,
            )
            self._model = CrossEncoder(self.model_name)
            logger.info("Cross-encoder reranker loaded.")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Cross-encoder reranker failed to load (%s) — disabling. "
                "Memory will fall back to bi-encoder scoring.",
                exc,
            )
            self._disabled = True
            return False

    def rerank(self, query: str, candidates: list[str]) -> list[float]:
        """Score each candidate against the query with the cross-encoder.

        Returns one squashed score in ``[0, 1]`` per candidate, in the
        same order. Returns an empty list when the model is unavailable
        or ``candidates`` is empty so the caller can detect failure and
        fall back to the original ranking.

        The cross-encoder's raw logit is squashed via sigmoid so the
        output composes cleanly with the bi-encoder's normalised
        cosine score in :meth:`fuse`.
        """
        if not candidates:
            return []
        if not query.strip():
            return []
        if not self._ensure_loaded():
            return []
        try:
            pairs = [(query, c) for c in candidates]
            raw_scores = self._model.predict(pairs)
            # CrossEncoder.predict returns numpy array or list of floats.
            return [_sigmoid(float(s)) for s in raw_scores]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Cross-encoder rerank failed mid-call (%s); falling back to "
                "bi-encoder ordering.",
                exc,
            )
            return []

    def fuse(
        self,
        originals: list[float],
        ce_scores: list[float],
    ) -> list[float]:
        """Weighted sum of bi-encoder and cross-encoder scores.

        Pure function — no model access. Lengths must match. When
        ``ce_scores`` is empty (rerank failed) the originals pass
        through unchanged so callers can use one code path regardless
        of whether the reranker fired.
        """
        if not ce_scores:
            return list(originals)
        if len(ce_scores) != len(originals):
            raise ValueError(
                f"fuse: length mismatch — originals={len(originals)}, "
                f"ce_scores={len(ce_scores)}",
            )
        w = self.fusion_weight
        return [w * ce + (1.0 - w) * orig for orig, ce in zip(originals, ce_scores)]
