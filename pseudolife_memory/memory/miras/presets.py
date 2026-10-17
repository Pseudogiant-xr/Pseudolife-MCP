"""Compatibility exports for the configuration-only MIRAS preset registry."""

from pseudolife_memory.utils.miras_presets import (  # noqa: F401
    PRESET_REGISTRY,
    _preset_bands,
    continuum_bands,
    flat_bands,
)


def preset_bands(name: str):
    """Return bands from this module's public registry, including replacements."""
    return _preset_bands(name, PRESET_REGISTRY)
