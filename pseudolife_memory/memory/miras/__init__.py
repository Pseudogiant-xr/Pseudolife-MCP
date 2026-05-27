"""MIRAS framework — pluggable memory-bank components.

A memory bank under MIRAS is defined by four orthogonal axes:

* :class:`MemoryModule` — the parametric mapping from key embeddings to
  value embeddings (MLP / Linear / etc.)
* :class:`UpdateRule` — the optimisation step that ingests a new sample
  (SGD-momentum / Adam / Lion / …)
* :class:`RetentionObjective` — the loss between predicted and target
  embeddings (L2 / Lp / negative-cosine / KV association)
* :class:`RetentionPolicy` — weight-decay strength + eviction scoring +
  contradiction-decay multiplier

A :class:`MIRASBand` is the unit of memory that combines all four plus a
frequency (``update_interval``) and capacity (``max_entries``). The
:mod:`src.memory.cms` orchestrator chains N bands together.

The ``titans`` preset reproduces v0.4.x behaviour bit-for-bit; other
presets (``moneta`` / ``yaad`` / ``memora``) pick different points in
this space — see :mod:`src.memory.miras.presets`.
"""

from pseudolife_memory.memory.miras.protocols import (
    MemoryModule,
    UpdateRule,
    RetentionObjective,
    RetentionPolicy,
)
from pseudolife_memory.memory.miras.band import MIRASBand, build_band
from pseudolife_memory.memory.miras.presets import preset_bands, PRESET_REGISTRY

__all__ = [
    "MemoryModule",
    "UpdateRule",
    "RetentionObjective",
    "RetentionPolicy",
    "MIRASBand",
    "build_band",
    "preset_bands",
    "PRESET_REGISTRY",
]
