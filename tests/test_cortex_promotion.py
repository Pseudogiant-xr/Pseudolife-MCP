"""Auto-promotion: ``store()`` deterministically promotes slot-shaped facts into
the cortex (the no-LLM floor that makes the cortex useful to models that don't
curate), with ``origin`` defaulted from ``source`` and overridable.

Uses a real MemoryService (offline embedder); slot extraction is deterministic
(``slots.extract_slots``), so the documented "Ragdoll cat named Jacque" example
yields a stable (Jacque, type, cat) slot.
"""
from __future__ import annotations

import tempfile

from pseudolife_memory.service import MemoryService

_SENTENCE = "I have a Ragdoll cat named Jacque"


def test_store_auto_promotes_slot_to_cortex():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.store(_SENTENCE, source="conversation")
        rec = svc.cortex_lookup("Jacque", "type")
        assert rec is not None
        assert rec["value"] == "cat"
        assert rec["origin"] == "user"          # source conversation -> user tier


def test_origin_defaults_to_agent_for_claude_source():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.store(_SENTENCE, source="claude")
        rec = svc.cortex_lookup("Jacque", "type")
        assert rec is not None and rec["origin"] == "agent"


def test_explicit_origin_overrides_source_default():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.store(_SENTENCE, source="claude", origin="user")
        rec = svc.cortex_lookup("Jacque", "type")
        assert rec is not None and rec["origin"] == "user"


def test_auto_promote_disabled_writes_nothing_to_cortex():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.config.memory.cortex.auto_promote = False
        svc.store(_SENTENCE, source="conversation")
        assert svc.cortex_lookup("Jacque", "type") is None
        assert svc.cortex_stats()["current"] == 0


def test_promoted_fact_is_low_confidence_floor():
    # Auto-promoted facts sit at the floor so a deliberate fact_set / user
    # assertion can out-rank them via the supersede margin.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.store(_SENTENCE, source="conversation")
        rec = svc.cortex_lookup("Jacque", "type")
        assert rec is not None and rec["confidence"] <= 0.55


if __name__ == "__main__":
    import sys
    import traceback

    tests = sorted(
        (n, o) for n, o in dict(globals()).items()
        if n.startswith("test_") and callable(o)
    )
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
