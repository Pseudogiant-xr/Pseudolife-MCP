"""Auto-promotion: ``store()`` deterministically promotes slot-shaped facts into
the cortex (the no-LLM floor that makes the cortex useful to models that don't
curate), with ``origin`` defaulted from ``source`` and overridable.

Uses a real MemoryService (offline embedder); slot extraction is deterministic
(``slots.extract_slots``), so the documented "Ragdoll cat named Jacque" example
yields a stable (Jacque, type, cat) slot.
"""
from __future__ import annotations

import tempfile

import pytest

from pseudolife_memory.service import MemoryService

_SENTENCE = "I have a Ragdoll cat named Jacque"


@pytest.mark.parametrize("source,origin,field,expected", [
    ("conversation", None, "value", "cat"),     # deterministic slot extraction
    ("conversation", None, "origin", "user"),   # source conversation -> user tier
    ("claude", None, "origin", "agent"),        # source claude -> agent tier
    ("claude", "user", "origin", "user"),       # explicit origin wins
])
def test_store_auto_promotes_slot_to_cortex(source, origin, field, expected):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.config.memory.cortex.auto_promote = True   # opt-in (default off)
        svc.store(_SENTENCE, source=source, origin=origin)
        rec = svc.cortex_lookup("Jacque", "type")
        assert rec is not None
        assert rec[field] == expected


def test_store_does_not_auto_promote_by_default():
    # Single-writer cortex: auto_promote ships OFF (and setting it False is the
    # same path), so a plain store() writes nothing to the cortex — the LLM
    # dream / memory_fact_set are the writers.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        out = svc.store(_SENTENCE, source="conversation")
        assert out["cortex_promoted"] == 0
        assert svc.cortex_lookup("Jacque", "type") is None
        assert svc.cortex_stats()["current"] == 0


def test_promoted_fact_is_low_confidence_floor():
    # Auto-promoted facts sit at the floor so a deliberate fact_set / user
    # assertion can out-rank them via the supersede margin.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.config.memory.cortex.auto_promote = True   # opt-in (default off)
        svc.store(_SENTENCE, source="conversation")
        rec = svc.cortex_lookup("Jacque", "type")
        assert rec is not None and rec["confidence"] <= 0.55
