"""Abstract base classes for the four MIRAS axes.

Each axis is an independently-pluggable component of a :class:`MIRASBand`.
Implementations live in sibling modules (``modules.py``, ``update_rules.py``,
``objectives.py``, ``retention.py``). Keeping the contract narrow here
makes the implementations easy to test in isolation.

Why ABCs and not Protocols
--------------------------
:class:`MemoryModule` inherits from :class:`torch.nn.Module` so subclasses
get parameter registration / device-moving / state-dict serialisation for
free; that needs concrete inheritance, not structural typing.
:class:`UpdateRule` and :class:`RetentionObjective` use ABCs for
symmetry — and because every implementation will subclass them in this
codebase, there's no need for the looser structural form.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import torch
import torch.nn as nn

if TYPE_CHECKING:
    from pseudolife_memory.memory.titans_memory import MemoryEntry


class MemoryModule(nn.Module, ABC):
    """Parametric mapping from key embeddings to value embeddings.

    The weights ARE the memory — updated at inference time via a small
    optimisation step driven by an :class:`UpdateRule`. Implementations
    vary in body (MLP3 / MLP2 / Linear) but every one must expose two
    data-dependent gate heads (``eta``, ``theta``) used by the update
    rule to modulate step size and surprise-EMA weighting respectively.

    Concrete implementations live in :mod:`src.memory.miras.modules`.
    """

    @abstractmethod
    def forward(self, key: torch.Tensor) -> torch.Tensor:
        """Predict the value associated with ``key`` under the current weights."""
        ...

    @abstractmethod
    def compute_eta(self, x: torch.Tensor) -> float:
        """Data-dependent learning-rate multiplier in ``[0, 1]``.

        Called inside :meth:`UpdateRule.step` to scale the base LR for this
        particular sample. Currently a sigmoid head on top of a learned linear
        projection — the projection itself is initialised at random and never
        trained explicitly; it nevertheless contributes a useful per-sample
        modulation because samples with similar embeddings produce similar
        η values.
        """
        ...

    @abstractmethod
    def compute_theta(self, x: torch.Tensor) -> float:
        """Data-dependent surprise-EMA weight in ``[0, 1]``.

        Multiplies the per-step surprise contribution to the running EMA
        used for introspection / consolidation decisions. Like ``compute_eta``,
        a sigmoid head over a learned-but-untrained linear projection.
        """
        ...

    @abstractmethod
    def init_weights(self) -> None:
        """Re-initialise to the same small-Xavier starting point as ``__init__``.

        Called by :meth:`ContinuumMemorySystem.clear` to wipe a bank without
        recreating the module object (preserves device, hyperparams, etc.).
        """
        ...


class UpdateRule(ABC):
    """One step of memory-weight update given a loss tensor.

    Concrete implementations (SGD-momentum / Adam / Lion / …) live in
    :mod:`src.memory.miras.update_rules`. The interface is deliberately
    narrower than :class:`torch.optim.Optimizer` so we can wrap optimisers
    that don't fit PyTorch's exact API (e.g. Lion) uniformly.

    Lifecycle inside :class:`MIRASBand.update_memory`::

        rule.zero_grad()
        loss = objective.loss(model(x), x)
        loss.backward()
        rule.step(model, loss, eta=model.compute_eta(x))
    """

    @abstractmethod
    def zero_grad(self) -> None:
        """Reset accumulated gradients on the wrapped parameters."""
        ...

    @abstractmethod
    def step(self, model: MemoryModule, loss: torch.Tensor, eta: float) -> None:
        """Apply one update.

        ``loss`` is passed for rules that want extra context (e.g. surprise-
        proportional step sizes); most implementations just rely on
        ``model.parameters().grad`` already populated by ``loss.backward()``.
        ``eta`` is the data-dependent LR multiplier from
        :meth:`MemoryModule.compute_eta`; the rule decides whether/how to
        apply it (e.g. Adam multiplies its already-normalised step by η,
        Lion multiplies its already-signed step).
        """
        ...

    @abstractmethod
    def state_dict(self) -> dict:
        """Serialise running state (momentum buffers, Adam moments, etc.)."""
        ...

    @abstractmethod
    def load_state_dict(self, state: dict) -> None:
        """Restore running state. Implementations should tolerate empty dicts
        (used by the v1→v2 migration when the saved optimiser type differs
        from the current preset's)."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Short stable identifier (``sgd_momentum`` / ``adam`` / …).

        Saved in the state dict so the loader can detect type mismatches
        between saved state and current config.
        """
        ...


class RetentionObjective(ABC):
    """How loss between predicted and target embeddings is computed.

    Concrete implementations live in :mod:`src.memory.miras.objectives`.
    ``loss`` returns a differentiable tensor used by :meth:`UpdateRule.step`;
    ``surprise_scalar`` returns a non-differentiable ``[0, 1]``-ish float
    used by the surprise gate in :class:`ContinuumMemorySystem.store`.

    The two are split so that ``surprise_scalar`` can use a *different*
    metric than the training loss — for example, an L_p objective trains
    against the L_p reconstruction error but the surprise gate consistently
    uses ``1 - cos_sim`` so the gate threshold ``surprise_threshold = 0.3``
    means the same thing across all presets.
    """

    @abstractmethod
    def loss(self, predicted: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        """Differentiable loss tensor used to drive the update."""
        ...

    @abstractmethod
    def surprise_scalar(self, predicted: torch.Tensor, target: torch.Tensor) -> float:
        """``[0, 1]``-ish scalar used by the surprise gate.

        Implementations should aim for ``0.0 = perfect prediction,
        1.0 = no idea`` so the global ``surprise_threshold`` config knob
        retains a consistent meaning regardless of preset.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Short stable identifier saved in the state dict."""
        ...


def _default_source_weights() -> dict[str, float]:
    """Default per-source multipliers on eviction score.

    Designed for the agentic-deployment taxonomy that ``continuum`` targets:
    long-running agents emit far more bookkeeping memories (tool calls,
    thinking traces) than human-equivalent user statements, so the bank
    should preferentially evict the bookkeeping when full. For chat-only
    apps all entries get ``source="user"`` or ``"assistant"`` (back-compat
    aliases below) which map to neutral or near-neutral weights — no
    behaviour change from v0.5.x.
    """
    return {
        # Human input — highest retention. The user *is* ground truth.
        "user_msg": 1.5,
        "user": 1.5,           # v0.5.x back-compat alias
        # First-class observations and actions.
        "tool_result": 1.0,
        "agent_action": 1.0,
        "assistant": 1.0,      # v0.5.x back-compat alias
        # Cheap to lose — the tool call is reconstructible from its result,
        # and thinking is by design an ephemeral scratchpad.
        "tool_call": 0.5,
        "llm_thinking": 0.2,
        # Near-pinned configuration / system prompts.
        "system": 10.0,
    }


@dataclass
class RetentionPolicy:
    """Weight-decay + eviction + contradiction-decay parameters.

    Unlike the other three axes this is a plain dataclass rather than an
    ABC, because the behaviour is fully captured by data + a couple of
    pre-defined scoring callables. Named policies (``balanced`` /
    ``recency_heavy`` / ``surprise_heavy`` / ``elastic_net``) are constructed
    by factory functions in :mod:`src.memory.miras.retention`.

    Attributes
    ----------
    weight_decay:
        Multiplied into the update rule (e.g. SGD ``weight_decay=`` kwarg).
    decay_factor_on_contradiction:
        Embedding-magnitude multiplier applied by
        :func:`src.memory.contradiction.decay_contradicted_entries` when
        a memory entry is marked superseded. Smaller = more aggressive.
    eviction_score:
        Callable ``(entry, now_seconds) -> float`` used by
        :meth:`MIRASBand._evict_one`. Lower scores get evicted first. The
        per-source weighting is applied on top via
        :meth:`source_weighted_score`.
    name:
        Short stable identifier saved in the state dict.
    l1_coef:
        Per-step L1 sparsity coefficient consumed by
        :class:`SurpriseModulatedUpdate` (added to the gradient as
        ``coef · sign(W)``). ``0.0`` for L2-only policies; nonzero for
        ``elastic_net`` to drive sparse weight updates on fast tiers.
    source_weights:
        Per-``entry.source`` multipliers on eviction score. Higher = harder
        to evict. Unknown sources fall back to ``1.0`` so adding new source
        names downstream is non-breaking.
    """

    weight_decay: float
    decay_factor_on_contradiction: float
    eviction_score: Callable[["MemoryEntry", float], float]
    name: str
    l1_coef: float = 0.0
    source_weights: dict[str, float] = field(default_factory=_default_source_weights)

    def source_weighted_score(self, entry: "MemoryEntry", now: float) -> float:
        """``(eviction_score + 1) × source_weights[entry.source]``.

        The ``+ 1`` floor ensures source weighting still differentiates
        fresh entries (where the base score is near zero — e.g. access_count
        is still 0 because the entry was just stored). Without it, the
        multiplication collapses to ``0 × anything = 0`` and the eviction
        order at capacity becomes whichever entry happened to be first.

        Established entries with a meaningful base score still see weighting
        compose multiplicatively as you'd expect — base = 10, weight 0.2 →
        score 2.2; base = 10, weight 1.5 → score 16.5.
        """
        base = self.eviction_score(entry, now)
        weight = self.source_weights.get(entry.source, 1.0)
        return (base + 1.0) * weight
