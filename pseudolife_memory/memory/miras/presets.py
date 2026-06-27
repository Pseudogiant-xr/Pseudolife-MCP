"""MIRAS presets — band layouts for the Continuum Memory System.

A preset returns a list of :class:`MIRASBandSpec` for
:class:`src.utils.config.MIRASConfig`. Select one in ``config.yaml``:

.. code-block:: yaml

    memory:
      miras:
        preset: continuum   # or: custom

v0.5: the test-time-trained neural memory was removed (bands are plain cosine
stores — see ``docs/2026-06-21-neural-memory-investigation.md``). A band spec is
now just capacity / cadence / promotion / eviction. The default ``continuum``
preset is the 8-tier recency-tiered store; the obsolete neural-experiment
presets (``titans`` / ``moneta`` / ``yaad`` / ``memora``) are kept as
deprecated aliases of ``continuum`` so old configs still load. When
``preset = custom`` the loader uses whatever ``bands`` block the user wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pseudolife_memory.utils.config import MIRASBandSpec


def _spec(**overrides: object) -> "MIRASBandSpec":
    """Construct a band spec with current dataclass defaults, then apply
    overrides. Imported inside the function to dodge the circular import
    between ``config.py`` (defines :class:`MIRASBandSpec`) and this module."""
    from pseudolife_memory.utils.config import MIRASBandSpec  # noqa: PLC0415

    return MIRASBandSpec(**overrides)  # type: ignore[arg-type]


def continuum_bands() -> list["MIRASBandSpec"]:
    """8 tiers: working / micro / instant / fast / medium / slow / archival /
    forever — a recency-tiered cosine store for agentic deployments.

    Geometric ~3× spacing of ``update_interval`` (1 → 1000) drives consolidation
    cadence; ``max_entries`` widens through the consolidation tiers (``slow`` is
    the main long-term store; ``archival`` / ``forever`` are deep but rarely
    reached); ``promotion_*`` raise the bar to enter slower tiers. Fast tiers
    evict ``balanced``; slow tiers ``surprise_heavy`` to pin rare-but-important
    facts. ~5K total capacity — right-sized 2026-06-27 from a 44K over-provisioned
    layout: a personal bank accrues ~10/day, so this engages eviction/curation in
    ~1yr (the ``slow`` band) instead of ~decades. Raise it for high-volume /
    multi-agent deployments (or set ``preset: custom``). Pairs with
    :meth:`ContinuumMemorySystem.begin_logical_turn` so consolidation fires per
    agent step, not per raw store.
    """
    return [
        _spec(name="working", max_entries=200, update_interval=1,
              promotion_access_count=2, promotion_surprise=0.4,
              retention_policy="balanced"),
        _spec(name="micro", max_entries=250, update_interval=1,
              promotion_access_count=2, promotion_surprise=0.45,
              retention_policy="balanced"),
        _spec(name="instant", max_entries=300, update_interval=2,
              promotion_access_count=2, promotion_surprise=0.5,
              retention_policy="balanced"),
        _spec(name="fast", max_entries=400, update_interval=5,
              promotion_access_count=3, promotion_surprise=0.55,
              retention_policy="balanced"),
        _spec(name="medium", max_entries=600, update_interval=15,
              promotion_access_count=3, promotion_surprise=0.65,
              retention_policy="balanced"),
        _spec(name="slow", max_entries=1500, update_interval=50,
              promotion_access_count=4, promotion_surprise=0.7,
              retention_policy="surprise_heavy"),
        _spec(name="archival", max_entries=1000, update_interval=200,
              promotion_access_count=5, promotion_surprise=0.8,
              retention_policy="surprise_heavy"),
        _spec(name="forever", max_entries=1000, update_interval=1000,
              promotion_access_count=999_999, promotion_surprise=1.0,
              retention_policy="surprise_heavy"),
    ]


# ``continuum`` is the only real layout now. The pre-v0.5 neural-experiment
# presets are deprecated aliases so existing config.yaml files keep loading.
PRESET_REGISTRY = {
    "continuum": continuum_bands,
    "titans": continuum_bands,
    "moneta": continuum_bands,
    "yaad": continuum_bands,
    "memora": continuum_bands,
}


def preset_bands(name: str) -> list["MIRASBandSpec"]:
    """Return the band specs for a named preset. ``custom`` is handled by the
    YAML loader (uses the user's ``bands`` block), not here."""
    try:
        return PRESET_REGISTRY[name]()
    except KeyError as exc:
        raise ValueError(
            f"Unknown MIRAS preset {name!r}. Available: {list(PRESET_REGISTRY)} + 'custom'"
        ) from exc
