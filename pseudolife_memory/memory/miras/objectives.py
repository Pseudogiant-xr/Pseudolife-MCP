"""Concrete :class:`RetentionObjective` implementations.

Each defines how the loss between a predicted value and its target is
computed, and what scalar surprise to report to the gate. ``surprise_scalar``
is deliberately decoupled from ``loss`` — see the docstring on
:class:`src.memory.miras.protocols.RetentionObjective`.

Objectives
----------
* :class:`L2ReconstructionObjective` — ``mse_loss``. v0.4.x default; used by ``titans``.
* :class:`LpReconstructionObjective` — ``(|pred-target|^p).mean()``. Used by ``moneta`` (p=1.5).
* :class:`NegativeSimilarityObjective` — ``1 - cos_sim``. Used by ``memora`` and as
  the consistent surprise metric across all objectives.
* :class:`KVAssociationObjective` — splits the embedding into K/V halves and
  trains the module to map K → V; closer to attention's inductive bias.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from pseudolife_memory.memory.miras.protocols import RetentionObjective


def _normalise(t: torch.Tensor) -> torch.Tensor:
    """L2-normalise a 1-D embedding (with an implicit batch dim of 1).

    Matches the v0.4.x ``F.normalize(x.unsqueeze(0), p=2, dim=1).squeeze(0)``.
    """
    return F.normalize(t.unsqueeze(0), p=2, dim=1).squeeze(0)


def _cosine_surprise(predicted: torch.Tensor, target: torch.Tensor) -> float:
    """``1 - cos_sim``, clamped to ``[0, 1]``. Used by every implementation
    as the consistent surprise metric, regardless of training loss."""
    with torch.no_grad():
        p = _normalise(predicted)
        t = _normalise(target)
        sim = F.cosine_similarity(p.unsqueeze(0), t.unsqueeze(0)).item()
    return max(0.0, 1.0 - sim)


class L2ReconstructionObjective(RetentionObjective):
    """Mean-squared error — the v0.4.x default.

    Pairs with :class:`SGDMomentumUpdate` in the ``titans`` preset to
    reproduce v0.4.x behaviour exactly.
    """

    def loss(self, predicted: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        return F.mse_loss(predicted, target)

    def surprise_scalar(self, predicted: torch.Tensor, target: torch.Tensor) -> float:
        return _cosine_surprise(predicted, target)

    @property
    def name(self) -> str:
        return "l2"


class LpReconstructionObjective(RetentionObjective):
    """``(|pred-target|^p).mean()`` for arbitrary ``p > 0``.

    For p=2 this is exactly MSE up to a constant — but useful values of
    p deviate: p=1 (L1, sparser updates), p=1.5 (Moneta's choice — between
    L1 and L2, robust to outliers but smoother than L1), p=4 (penalises
    large errors quadratically harder than MSE).

    Gradient magnitude scales with ``p``, so for ``p > 2`` we recommend
    a smaller ``base_lr`` in the update rule (the preset registry handles
    this) and rely on :attr:`UpdateRule.max_grad_norm` for safety.
    """

    def __init__(self, p: float = 1.5):
        if p <= 0:
            raise ValueError(f"LpReconstruction requires p > 0, got {p}")
        self.p = p

    def loss(self, predicted: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        # ``abs().pow(p).mean()`` instead of e.g. ``torch.norm(...)`` because we
        # want the loss averaged across dimensions, not the vector norm.
        return (predicted - target).abs().pow(self.p).mean()

    def surprise_scalar(self, predicted: torch.Tensor, target: torch.Tensor) -> float:
        # Consistent surprise metric across all objectives — see protocol docstring.
        return _cosine_surprise(predicted, target)

    @property
    def name(self) -> str:
        return f"lp_p{self.p}"


class NegativeSimilarityObjective(RetentionObjective):
    """``1 - cos_sim(predicted, target)``.

    The objective and the surprise scalar coincide here. The gradient is
    well-behaved as long as both vectors stay non-zero — the data-dependent
    eta gate keeps it stable in practice. Used by ``memora`` as a softer
    alternative to MSE for the slowest bands.
    """

    def loss(self, predicted: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        p = _normalise(predicted)
        t = _normalise(target)
        # Negative similarity → minimisation drives sim → 1.
        # ``F.cosine_similarity`` produces a scalar with implicit batch dim;
        # ``squeeze`` ensures it lines up with the other objectives' shapes.
        return 1.0 - F.cosine_similarity(p.unsqueeze(0), t.unsqueeze(0)).squeeze()

    def surprise_scalar(self, predicted: torch.Tensor, target: torch.Tensor) -> float:
        return _cosine_surprise(predicted, target)

    @property
    def name(self) -> str:
        return "neg_sim"


class HuberObjective(RetentionObjective):
    """Huber loss — quadratic in the small-error regime, linear above ``delta``.

    Pairs well with the mid-to-slow tiers of the ``continuum`` preset where
    promoted entries from faster tiers can be noisy: outliers caused by
    surprise spikes during consolidation aren't allowed to dominate the
    gradient. Used by Yaad-style robust memory layers in the MIRAS paper.

    .. math::

        \\text{loss}(p, t) = \\begin{cases}
          \\tfrac{1}{2}(p - t)^2          & |p - t| \\le \\delta \\\\
          \\delta\\,(|p - t| - \\tfrac{\\delta}{2}) & \\text{otherwise}
        \\end{cases}

    ``delta`` defaults to 1.0 (the canonical Huber threshold). At our
    embedding scale (unit-normalised, dim 384), per-element errors are
    typically in [-1, 1] so delta=1.0 keeps almost all errors in the
    quadratic regime — the *behaviour* is L2 in the common case, with the
    linear tail acting as a safety valve for outliers.
    """

    def __init__(self, delta: float = 1.0):
        if delta <= 0:
            raise ValueError(f"HuberObjective requires delta > 0, got {delta}")
        self.delta = delta

    def loss(self, predicted: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        return F.huber_loss(predicted, target, delta=self.delta)

    def surprise_scalar(self, predicted: torch.Tensor, target: torch.Tensor) -> float:
        # Use the consistent cosine surprise across objectives so the global
        # surprise_threshold knob retains its meaning regardless of objective.
        return _cosine_surprise(predicted, target)

    @property
    def name(self) -> str:
        return f"huber_d{self.delta}"


class KVAssociationObjective(RetentionObjective):
    """Split the embedding into K/V halves; train M to map K → V.

    The first ``dim/2`` coordinates of the embedding act as the key, the
    second ``dim/2`` as the value. The module predicts the full
    ``dim``-vector from the input, but only the V-half of the prediction
    is supervised against the V-half of the target. Closer to the inductive
    bias of attention layers — the K/V split mirrors how transformers
    separate addressing from content.

    Note: implementations may want to override how the module's output is
    interpreted (K-only vs full); we keep the same forward signature for
    drop-in compatibility and supervise the V-half only.
    """

    def loss(self, predicted: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        half = predicted.shape[-1] // 2
        pred_v = predicted[..., half:]
        target_v = target[..., half:]
        return F.mse_loss(pred_v, target_v)

    def surprise_scalar(self, predicted: torch.Tensor, target: torch.Tensor) -> float:
        return _cosine_surprise(predicted, target)

    @property
    def name(self) -> str:
        return "kv"


# Registry used by :class:`MIRASBand` to construct from a string.
OBJECTIVE_REGISTRY: dict[str, type[RetentionObjective]] = {
    "l2": L2ReconstructionObjective,
    "lp": LpReconstructionObjective,
    "neg_sim": NegativeSimilarityObjective,
    "huber": HuberObjective,
    "kv": KVAssociationObjective,
    # Aliases for the v0.4.x string used in old saved state.
    "l2_reconstruction": L2ReconstructionObjective,
}


def build_objective(name: str, p: float = 2.0) -> RetentionObjective:
    """Construct an objective by name.

    ``p`` is only consumed by :class:`LpReconstructionObjective`; for
    :class:`HuberObjective` it's reinterpreted as the ``delta`` knob (the
    Huber crossover threshold). For the rest it's silently ignored.
    """
    try:
        cls = OBJECTIVE_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown objective {name!r}. Available: {list(OBJECTIVE_REGISTRY)}"
        ) from exc
    if cls is LpReconstructionObjective:
        return cls(p=p)
    if cls is HuberObjective:
        # ``p`` defaults to 2.0 which is also a sensible Huber delta — the
        # spec field can stay generic. Callers that want a different delta
        # set ``objective_p`` in the MIRASBandSpec.
        return cls(delta=p if p > 0 else 1.0)
    return cls()
