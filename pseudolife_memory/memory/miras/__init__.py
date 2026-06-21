"""MIRAS framework — the Continuum Memory System's band layer.

v0.5: bands are plain **cosine** vector stores (the test-time neural memory was
removed — see ``docs/2026-06-21-neural-memory-investigation.md``; the machinery
is archived on ``archive/neural-memory-titans``). A band combines a capacity
(``max_entries``), a consolidation cadence (``update_interval``), promotion
thresholds, and a :class:`RetentionPolicy` (eviction + contradiction-decay). The
:mod:`src.memory.cms` orchestrator chains N bands into a recency-tiered store.
"""

from pseudolife_memory.memory.miras.protocols import RetentionPolicy
from pseudolife_memory.memory.miras.band import MIRASBand, build_band
from pseudolife_memory.memory.miras.presets import preset_bands, PRESET_REGISTRY

__all__ = [
    "RetentionPolicy",
    "MIRASBand",
    "build_band",
    "preset_bands",
    "PRESET_REGISTRY",
]
