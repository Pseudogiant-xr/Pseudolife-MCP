"""Concrete :class:`UpdateRule` implementations.

Each wraps a :class:`torch.optim.Optimizer` (or a hand-rolled equivalent
for rules with no stock PyTorch implementation) and applies the
data-dependent ``eta`` modulation per step. All rules also perform
gradient clipping at ``max_grad_norm`` before stepping — the v0.4.x
default of ``1.0`` is preserved.

Rules
-----
* :class:`SGDMomentumUpdate` — wraps :class:`torch.optim.SGD` with
  momentum=0.9. The v0.4.x default; used by ``titans``.
* :class:`AdamUpdate` — wraps :class:`torch.optim.Adam` with bias
  correction. Used by ``moneta``.
* :class:`LionUpdate` — sign-based momentum, no PyTorch native impl.
  Used by ``memora``.
* :class:`MomentumOnlyUpdate` — heavy-ball without weight decay; used
  by slower bands where decay is handled by eviction instead.
"""

from __future__ import annotations

from typing import Iterable

import torch
import torch.nn as nn

from pseudolife_memory.memory.miras.protocols import MemoryModule, UpdateRule


def _scale_lr(optimizer: torch.optim.Optimizer, base_lr: float, eta: float) -> None:
    """Set ``lr = base_lr * eta`` on every param group.

    Matches the v0.4.x behaviour (titans_memory.py:200) — η squashed in
    ``[0, 1]`` modulates the base LR every step.
    """
    scaled = base_lr * eta
    for pg in optimizer.param_groups:
        pg["lr"] = scaled


def _load_optim_state_compat(
    optimizer: torch.optim.Optimizer, expected_name: str, state: dict,
) -> None:
    """Load an optimiser state dict, tolerating v1 (raw) and v2 (wrapped) layouts.

    v0.4.x persisted the raw output of ``torch.optim.Optimizer.state_dict()``
    directly. v0.5+ wraps it under ``{"name": <rule>, "opt": <raw>}`` so we
    can detect rule-type mismatches across saved/loaded versions. The
    migration loader needs to handle both:

    * **v1** (no ``"name"`` key, looks like a raw torch optimiser state) —
      try to load directly; if the optimiser type changed across versions
      torch will raise and we silently drop the state.
    * **v2** with matching name — load the inner ``"opt"`` block.
    * **v2** with mismatched name — drop the state (memory weights still load).
    """
    if not state:
        return
    # v1: raw torch optimiser state has ``param_groups`` and ``state`` at top.
    if "name" not in state and "opt" not in state and "param_groups" in state:
        try:
            optimizer.load_state_dict(state)
        except Exception:
            # Optimiser type mismatch (e.g. v1 SGD state → v0.5 Adam config).
            # Memory weights are loaded separately, so this is recoverable —
            # the optimiser just starts fresh.
            pass
        return
    # v2 — wrapped format with optional name check.
    if state.get("name") != expected_name:
        return
    optimizer.load_state_dict(state.get("opt", {}))


class SGDMomentumUpdate(UpdateRule):
    """SGD with momentum + weight decay, η-modulated LR.

    Reproduces the v0.4.x ``TitansMemoryBank`` optimiser bit-for-bit:

    .. code-block:: python

        torch.optim.SGD(params, lr=base_lr, momentum=0.9, weight_decay=wd)
        # per step:
        for pg in optimizer.param_groups: pg["lr"] = base_lr * eta
        loss.backward()
        clip_grad_norm_(params, 1.0)
        optimizer.step()
    """

    def __init__(
        self,
        params: Iterable[nn.Parameter],
        base_lr: float = 0.01,
        momentum: float = 0.9,
        weight_decay: float = 0.001,
        max_grad_norm: float = 1.0,
    ):
        self.base_lr = base_lr
        self.max_grad_norm = max_grad_norm
        self._params = list(params)
        self._opt = torch.optim.SGD(
            self._params,
            lr=base_lr,
            momentum=momentum,
            weight_decay=weight_decay,
        )

    def zero_grad(self) -> None:
        self._opt.zero_grad()

    def step(self, model: MemoryModule, loss: torch.Tensor, eta: float) -> None:
        _scale_lr(self._opt, self.base_lr, eta)
        torch.nn.utils.clip_grad_norm_(self._params, self.max_grad_norm)
        self._opt.step()

    def state_dict(self) -> dict:
        return {"name": self.name, "opt": self._opt.state_dict()}

    def load_state_dict(self, state: dict) -> None:
        # Tolerant of v1 raw layout — see _load_optim_state_compat docstring.
        _load_optim_state_compat(self._opt, self.name, state)

    @property
    def name(self) -> str:
        return "sgd_momentum"


class AdamUpdate(UpdateRule):
    """Adam with bias correction, η-modulated LR.

    Adam already normalises by the running gradient magnitude, so the
    η multiplier here means "this sample matters less" rather than
    "shrink the effective step by η" — Adam's adaptive scaling absorbs
    most of η's variance. In practice we still apply it for symmetry
    with the other rules.
    """

    def __init__(
        self,
        params: Iterable[nn.Parameter],
        base_lr: float = 0.001,
        betas: tuple[float, float] = (0.9, 0.999),
        weight_decay: float = 0.001,
        max_grad_norm: float = 1.0,
    ):
        self.base_lr = base_lr
        self.max_grad_norm = max_grad_norm
        self._params = list(params)
        self._opt = torch.optim.Adam(
            self._params,
            lr=base_lr,
            betas=betas,
            weight_decay=weight_decay,
        )

    def zero_grad(self) -> None:
        self._opt.zero_grad()

    def step(self, model: MemoryModule, loss: torch.Tensor, eta: float) -> None:
        _scale_lr(self._opt, self.base_lr, eta)
        torch.nn.utils.clip_grad_norm_(self._params, self.max_grad_norm)
        self._opt.step()

    def state_dict(self) -> dict:
        return {"name": self.name, "opt": self._opt.state_dict()}

    def load_state_dict(self, state: dict) -> None:
        _load_optim_state_compat(self._opt, self.name, state)

    @property
    def name(self) -> str:
        return "adam"


class LionUpdate(UpdateRule):
    """Lion: sign-based momentum, no PyTorch native impl.

    Reference: "Symbolic Discovery of Optimization Algorithms" (Chen et al.,
    2023). Each parameter step is ``sign(β1·m + (1-β1)·g) * lr``; the
    momentum buffer ``m`` is updated as ``β2·m + (1-β2)·g``. We follow
    the paper's recommended defaults β1=0.9, β2=0.99.

    Lion tends to need ``base_lr`` ~3-10× smaller than Adam for similar
    behaviour — the preset registry compensates.
    """

    def __init__(
        self,
        params: Iterable[nn.Parameter],
        base_lr: float = 0.0001,
        beta1: float = 0.9,
        beta2: float = 0.99,
        weight_decay: float = 0.0,
        max_grad_norm: float = 1.0,
    ):
        self.base_lr = base_lr
        self.beta1 = beta1
        self.beta2 = beta2
        self.weight_decay = weight_decay
        self.max_grad_norm = max_grad_norm
        self._params = list(params)
        # Momentum buffers — one per parameter tensor, lazily created on first step.
        self._momentum: dict[int, torch.Tensor] = {}

    def zero_grad(self) -> None:
        for p in self._params:
            if p.grad is not None:
                p.grad.detach_()
                p.grad.zero_()

    def step(self, model: MemoryModule, loss: torch.Tensor, eta: float) -> None:
        torch.nn.utils.clip_grad_norm_(self._params, self.max_grad_norm)
        lr = self.base_lr * eta
        with torch.no_grad():
            for p in self._params:
                if p.grad is None:
                    continue
                key = id(p)
                m = self._momentum.get(key)
                if m is None:
                    m = torch.zeros_like(p)
                    self._momentum[key] = m
                # Update rule from the Lion paper, eq. (1).
                update = (self.beta1 * m + (1.0 - self.beta1) * p.grad).sign()
                if self.weight_decay != 0.0:
                    p.add_(p, alpha=-lr * self.weight_decay)
                p.add_(update, alpha=-lr)
                # Momentum buffer update (separate β2 — done AFTER the step
                # per the paper's recommendation).
                m.mul_(self.beta2).add_(p.grad, alpha=1.0 - self.beta2)

    def state_dict(self) -> dict:
        # Save by tensor index in self._params for deterministic reload.
        return {
            "name": self.name,
            "momentum": [
                self._momentum[id(p)].cpu()
                for p in self._params
                if id(p) in self._momentum
            ],
        }

    def load_state_dict(self, state: dict) -> None:
        if not state or state.get("name") != self.name:
            return
        buffers = state.get("momentum", [])
        for p, m in zip(self._params, buffers):
            self._momentum[id(p)] = m.to(p.device)

    @property
    def name(self) -> str:
        return "lion"


class MomentumOnlyUpdate(UpdateRule):
    """Heavy-ball momentum, no weight decay, η-modulated LR.

    Equivalent to ``SGDMomentumUpdate(weight_decay=0.0)``. Intended for
    the slowest bands in a multi-band preset, where decay is handled by
    eviction policy rather than weight decay so old patterns don't
    asymptote to zero.
    """

    def __init__(
        self,
        params: Iterable[nn.Parameter],
        base_lr: float = 0.0001,
        momentum: float = 0.9,
        max_grad_norm: float = 1.0,
    ):
        self.base_lr = base_lr
        self.max_grad_norm = max_grad_norm
        self._params = list(params)
        self._opt = torch.optim.SGD(
            self._params,
            lr=base_lr,
            momentum=momentum,
            weight_decay=0.0,
        )

    def zero_grad(self) -> None:
        self._opt.zero_grad()

    def step(self, model: MemoryModule, loss: torch.Tensor, eta: float) -> None:
        _scale_lr(self._opt, self.base_lr, eta)
        torch.nn.utils.clip_grad_norm_(self._params, self.max_grad_norm)
        self._opt.step()

    def state_dict(self) -> dict:
        return {"name": self.name, "opt": self._opt.state_dict()}

    def load_state_dict(self, state: dict) -> None:
        _load_optim_state_compat(self._opt, self.name, state)

    @property
    def name(self) -> str:
        return "momentum_only"


class SurpriseModulatedUpdate(UpdateRule):
    """Wraps any base :class:`UpdateRule` with a deterministic surprise→η map.

    The original v0.4.x ``MemoryMLP.eta_gate`` was a learned linear+sigmoid
    head on the input embedding — a clean design *if* you have a training
    loop to update its weights against an outer objective. PseudoLife has
    no training loop (memory is updated only at inference time), so the
    gate weights stay at their xavier-init values forever and contribute
    essentially random multiplicative noise rather than learned modulation.

    This wrapper replaces that gate with a deterministic surprise function:

    .. math::

        \\eta_\\text{eff} = \\sigma\\left(\\frac{\\text{surprise} -
            \\text{threshold}}{T}\\right)

    where ``surprise`` is the loss value computed by the objective on the
    current sample (before the step), and ``T`` is a temperature knob.
    High surprise → ``η_eff`` near 1 → full base_lr step (we want to learn
    novel info fast). Low surprise → ``η_eff`` near 0 → tiny step (memory
    stable on familiar inputs). Captures the spirit of HOPE's self-modifying
    Titans gate without needing the training loop required to learn it.

    Bonus: when wrapped around a rule whose retention policy carries an
    ``l1_coef`` (set by :func:`elastic_net`), the wrapper also adds an
    ``l1_coef · sign(W)`` term to the gradient before stepping. This is
    how the ``elastic_net`` policy actually achieves sparse updates — the
    RetentionPolicy data alone can't reach into the optimiser, so the
    wrapper does it.
    """

    def __init__(
        self,
        base: UpdateRule,
        threshold: float = 0.3,
        temperature: float = 0.1,
        l1_coef: float = 0.0,
        params: Iterable[nn.Parameter] | None = None,
    ):
        self._base = base
        self.threshold = threshold
        self.temperature = max(1e-6, temperature)  # avoid div-by-zero
        self.l1_coef = l1_coef
        # ``params`` lets the wrapper access weights directly for the L1
        # gradient addition; fall back to the base's param list.
        self._params: list[nn.Parameter] = (
            list(params) if params is not None else list(getattr(base, "_params", []))
        )

    @property
    def base_lr(self) -> float:
        """Mirror the wrapped rule's base_lr for diagnostics."""
        return getattr(self._base, "base_lr", 0.0)

    def zero_grad(self) -> None:
        self._base.zero_grad()

    def step(self, model: MemoryModule, loss: torch.Tensor, eta: float) -> None:
        # Surprise = the pre-step loss value. ``loss`` is a 0-d tensor; ``.item()``
        # is a no-op detach. We compute eta_eff entirely with Python scalars so
        # there's no autograd graph leakage.
        surprise = float(loss.detach().item())
        # Sigmoid in pure Python is cheaper than allocating a torch tensor here.
        z = (surprise - self.threshold) / self.temperature
        # Clamp z to avoid sigmoid saturation NaN at huge magnitudes.
        if z > 50.0:
            eta_eff = 1.0
        elif z < -50.0:
            eta_eff = 0.0
        else:
            import math  # noqa: PLC0415
            eta_eff = 1.0 / (1.0 + math.exp(-z))

        # L1 contribution from elastic-net retention. Done BEFORE the base
        # step so the wrapped optimiser sees the augmented gradient.
        if self.l1_coef > 0.0:
            with torch.no_grad():
                for p in self._params:
                    if p.grad is not None:
                        p.grad.add_(p.sign(), alpha=self.l1_coef)

        # The caller-supplied ``eta`` (from MemoryModule.compute_eta) is also
        # applied — composes multiplicatively with surprise modulation so a
        # band that wants pure surprise-driven gating can use a memory module
        # whose compute_eta returns ~1.0.
        self._base.step(model, loss, eta=eta * eta_eff)

    def state_dict(self) -> dict:
        return {
            "name": self.name,
            "base_name": self._base.name,
            "base": self._base.state_dict(),
            "threshold": self.threshold,
            "temperature": self.temperature,
            "l1_coef": self.l1_coef,
        }

    def load_state_dict(self, state: dict) -> None:
        if not state:
            return
        if state.get("name") != self.name:
            # Cross-rule mismatch — drop. The wrapped base may still load if
            # the saved layout happens to match its own format.
            self._base.load_state_dict(state)
            return
        if state.get("base_name") != self._base.name:
            return
        self._base.load_state_dict(state.get("base", {}))
        # threshold / temperature / l1_coef are restored from the spec at
        # build time, not from saved state — the saved values are kept for
        # introspection but not applied (they may be re-tuned across versions).

    @property
    def name(self) -> str:
        # Composite name so saved state can be matched on load.
        return f"sm_{self._base.name}"


# Registry used by :mod:`src.memory.miras.band` to construct from a string.
# Keys with ``sm_`` prefix are SurpriseModulatedUpdate wrappers around the
# corresponding base rule.
UPDATE_RULE_REGISTRY: dict[str, type[UpdateRule]] = {
    "sgd_momentum": SGDMomentumUpdate,
    "adam": AdamUpdate,
    "lion": LionUpdate,
    "momentum_only": MomentumOnlyUpdate,
    # Surprise-modulated variants are constructed via build_update_rule, not
    # by direct class lookup — registered as a sentinel so the registry view
    # still lists them.
    "sm_sgd_momentum": SGDMomentumUpdate,
    "sm_adam": AdamUpdate,
    "sm_lion": LionUpdate,
    "sm_momentum_only": MomentumOnlyUpdate,
}

# Names that the wrapper composes its base for.
_SURPRISE_MODULATED_PREFIX = "sm_"


def build_update_rule(
    name: str,
    params: Iterable[nn.Parameter],
    base_lr: float,
    weight_decay: float,
    *,
    l1_coef: float = 0.0,
    sm_threshold: float = 0.3,
    sm_temperature: float = 0.1,
) -> UpdateRule:
    """Construct an update rule by name. Used by :class:`MIRASBand`.

    Names with ``sm_`` prefix wrap the corresponding base rule in
    :class:`SurpriseModulatedUpdate`. ``l1_coef`` is forwarded to the
    wrapper for elastic-net retention's sparse-update behaviour.

    ``sm_threshold`` and ``sm_temperature`` tune the surprise→η sigmoid;
    defaults match the v0.4.x ``surprise_threshold`` config knob's working
    range.
    """
    try:
        _ = UPDATE_RULE_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown update_rule {name!r}. Available: {list(UPDATE_RULE_REGISTRY)}"
        ) from exc

    is_sm = name.startswith(_SURPRISE_MODULATED_PREFIX)
    base_name = name[len(_SURPRISE_MODULATED_PREFIX):] if is_sm else name
    base_cls = UPDATE_RULE_REGISTRY[base_name]

    # Materialise params once so both base + wrapper see the same tensor list.
    params_list = list(params)

    if base_cls is LionUpdate:
        base = base_cls(params=params_list, base_lr=base_lr, weight_decay=weight_decay)
    elif base_cls is MomentumOnlyUpdate:
        base = base_cls(params=params_list, base_lr=base_lr)
    else:
        base = base_cls(params=params_list, base_lr=base_lr, weight_decay=weight_decay)

    if not is_sm:
        return base
    return SurpriseModulatedUpdate(
        base=base,
        threshold=sm_threshold,
        temperature=sm_temperature,
        l1_coef=l1_coef,
        params=params_list,
    )
