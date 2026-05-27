"""Concrete :class:`MemoryModule` implementations.

Each module is a small neural net whose weights ARE the memory. They map
key embeddings to value embeddings and are updated online via an
:class:`UpdateRule` at inference time (test-time training).

Modules
-------
* :class:`MLP3Module` — the v0.4.x default. 3 linear layers with GELU.
  Used by the ``titans`` preset.
* :class:`MLP2Module` — cheaper 2-layer variant; useful for very-fast
  bands where capacity matters less than throughput.
* :class:`LinearMemoryModule` — single linear layer; classic associative
  memory. Adopted by the ``yaad`` preset's first band.

All modules expose the data-dependent ``eta`` / ``theta`` gates required
by the :class:`MemoryModule` ABC.
"""

from __future__ import annotations

import torch
import torch.nn as nn

from pseudolife_memory.memory.miras.protocols import MemoryModule


class _GatedMixin:
    """Shared eta/theta gate heads + xavier init logic for memory modules.

    Pulled out so MLP3 / MLP2 / Linear all share the same gate construction
    and re-init logic without inheritance pyramids.
    """

    eta_gate: nn.Linear
    theta_gate: nn.Linear

    def _make_gates(self, dim: int) -> None:
        # Two scalar heads on the input embedding — sigmoid-squashed at use site.
        self.eta_gate = nn.Linear(dim, 1)
        self.theta_gate = nn.Linear(dim, 1)

    def _init_linear_weights(self) -> None:
        """Small Xavier init with zero biases — same as the v0.4.x MemoryMLP."""
        for m in self.modules():  # type: ignore[attr-defined]
            if isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight, gain=0.1)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)


class MLP3Module(MemoryModule, _GatedMixin):
    """3-layer MLP with GELU. The v0.4.x default — reproduces the
    behaviour of the old ``MemoryMLP`` bit-for-bit when paired with
    :class:`SGDMomentumUpdate` and :class:`L2ReconstructionObjective`.
    """

    def __init__(self, dim: int, hidden_dim: int = 512):
        super().__init__()
        self.dim = dim
        self.hidden_dim = hidden_dim
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, dim),
        )
        self._make_gates(dim)
        self.init_weights()

    def forward(self, key: torch.Tensor) -> torch.Tensor:
        return self.net(key)

    def compute_eta(self, x: torch.Tensor) -> float:
        with torch.no_grad():
            return torch.sigmoid(self.eta_gate(x)).item()

    def compute_theta(self, x: torch.Tensor) -> float:
        with torch.no_grad():
            return torch.sigmoid(self.theta_gate(x)).item()

    def init_weights(self) -> None:
        self._init_linear_weights()


class MLP2Module(MemoryModule, _GatedMixin):
    """2-layer MLP variant. ~1/3 fewer params than MLP3 at the same
    ``hidden_dim``. Suitable for fast bands where update latency matters
    more than long-range associative capacity.
    """

    def __init__(self, dim: int, hidden_dim: int = 512):
        super().__init__()
        self.dim = dim
        self.hidden_dim = hidden_dim
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, dim),
        )
        self._make_gates(dim)
        self.init_weights()

    def forward(self, key: torch.Tensor) -> torch.Tensor:
        return self.net(key)

    def compute_eta(self, x: torch.Tensor) -> float:
        with torch.no_grad():
            return torch.sigmoid(self.eta_gate(x)).item()

    def compute_theta(self, x: torch.Tensor) -> float:
        with torch.no_grad():
            return torch.sigmoid(self.theta_gate(x)).item()

    def init_weights(self) -> None:
        self._init_linear_weights()


class LinearMemoryModule(MemoryModule, _GatedMixin):
    """Single linear layer — classic associative memory.

    The capacity ceiling is the dim×dim weight matrix (~150 K params at
    dim=384), but updates are an order of magnitude cheaper than an MLP
    and the linear form has a closed-form Hebbian interpretation. Used
    by the ``yaad`` preset's first band as a fast reactive layer.
    """

    def __init__(self, dim: int, hidden_dim: int | None = None):
        # ``hidden_dim`` is accepted for API symmetry with MLP modules but
        # ignored — Linear memory has no hidden layer.
        super().__init__()
        self.dim = dim
        self.hidden_dim = hidden_dim or dim
        self.net = nn.Linear(dim, dim)
        self._make_gates(dim)
        self.init_weights()

    def forward(self, key: torch.Tensor) -> torch.Tensor:
        return self.net(key)

    def compute_eta(self, x: torch.Tensor) -> float:
        with torch.no_grad():
            return torch.sigmoid(self.eta_gate(x)).item()

    def compute_theta(self, x: torch.Tensor) -> float:
        with torch.no_grad():
            return torch.sigmoid(self.theta_gate(x)).item()

    def init_weights(self) -> None:
        self._init_linear_weights()


# Module registry used by :mod:`src.memory.miras.band` to construct
# from a ``MIRASBandSpec.memory_module`` string. New modules: register here.
MODULE_REGISTRY: dict[str, type[MemoryModule]] = {
    "mlp3": MLP3Module,
    "mlp2": MLP2Module,
    "linear": LinearMemoryModule,
}


def build_module(name: str, dim: int, hidden_dim: int) -> MemoryModule:
    """Construct a memory module by name. Used by :class:`MIRASBand`."""
    try:
        cls = MODULE_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown memory_module {name!r}. Available: {list(MODULE_REGISTRY)}"
        ) from exc
    return cls(dim=dim, hidden_dim=hidden_dim)
