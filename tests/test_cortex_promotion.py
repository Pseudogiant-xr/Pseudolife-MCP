"""Auto-promotion: ``store()`` deterministically promotes slot-shaped facts into
the cortex (the no-LLM floor that makes the cortex useful to models that don't
curate), with ``origin`` defaulted from ``source`` and overridable.

Uses a real MemoryService (offline embedder); slot extraction is deterministic
(``slots.extract_slots``), so the documented "Ragdoll cat named Jacque" example
yields a stable (Jacque, type, cat) slot.

Shares conftest's service via ``pristine_service`` — the bank (CMS + cortex) is
emptied per test, so nothing survives between the parametrized cases. The
``auto_promote`` flip does NOT: ``svc.config`` outlives the clear, so every
test that sets it restores it in a ``finally``.
"""
from __future__ import annotations

import pytest

_SENTENCE = "I have a Ragdoll cat named Jacque"


@pytest.mark.parametrize("source,origin,field,expected", [
    ("conversation", None, "value", "cat"),     # deterministic slot extraction
    ("conversation", None, "origin", "user"),   # source conversation -> user tier
    ("claude", None, "origin", "agent"),        # source claude -> agent tier
    ("claude", "user", "origin", "user"),       # explicit origin wins
])
def test_store_auto_promotes_slot_to_cortex(pristine_service, source, origin,
                                            field, expected):
    svc = pristine_service
    svc.config.memory.cortex.auto_promote = True   # opt-in (default off)
    try:
        svc.store(_SENTENCE, source=source, origin=origin)
        rec = svc.cortex_lookup("Jacque", "type")
        assert rec is not None
        assert rec[field] == expected
    finally:
        svc.config.memory.cortex.auto_promote = False


def test_store_does_not_auto_promote_by_default(pristine_service):
    # Single-writer cortex: auto_promote ships OFF (and setting it False is the
    # same path), so a plain store() writes nothing to the cortex — the LLM
    # dream / memory_fact_set are the writers.
    svc = pristine_service
    assert svc.config.memory.cortex.auto_promote is False  # no leak from above
    out = svc.store(_SENTENCE, source="conversation")
    assert out["cortex_promoted"] == 0
    assert svc.cortex_lookup("Jacque", "type") is None
    assert svc.cortex_stats()["current"] == 0


def test_promoted_fact_is_low_confidence_floor(pristine_service):
    # Auto-promoted facts sit at the floor so a deliberate fact_set / user
    # assertion can out-rank them via the supersede margin.
    svc = pristine_service
    svc.config.memory.cortex.auto_promote = True   # opt-in (default off)
    try:
        svc.store(_SENTENCE, source="conversation")
        rec = svc.cortex_lookup("Jacque", "type")
        assert rec is not None and rec["confidence"] <= 0.55
    finally:
        svc.config.memory.cortex.auto_promote = False
