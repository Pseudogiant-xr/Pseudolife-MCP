"""Phase 3 integration: CortexStore wired into MemoryService + co-persistence.

Unlike the unit suite, this constructs a real MemoryService (loads the embedder
offline) against a throwaway data dir — so it never touches production state.
Run with: .venv/bin/python3 tests/test_cortex_service.py
"""
from __future__ import annotations

import tempfile

from pseudolife_memory.service import MemoryService


def test_cortex_write_then_lookup_roundtrip_through_service():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        r = svc.cortex_write("grid", "size", "41", provenance=["ep1"])
        assert r["action"] == "inserted"
        assert r["value"] == "41"
        got = svc.cortex_lookup("grid", "size")
        assert got is not None
        assert got["value"] == "41"
        assert got["status"] == "current"
        assert got["provenance"] == ["ep1"]


def test_cortex_supersede_then_search_returns_current_only():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("grid", "size", "40", provenance=["ep1"])
        svc.cortex_write("grid", "size", "41", provenance=["ep2"])
        assert svc.cortex_lookup("grid", "size")["value"] == "41"
        entries = svc.cortex_search("grid size", top_k=10)["entries"]
        values = [e["value"] for e in entries]
        assert "41" in values
        assert "40" not in values


def test_cortex_copersists_across_service_restart():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("user", "city", "Sydney", provenance=["epX"])
        svc.save()
        svc2 = MemoryService(data_dir=d)
        got = svc2.cortex_lookup("user", "city")
        assert got is not None
        assert got["value"] == "Sydney"
        assert got["provenance"] == ["epX"]


if __name__ == "__main__":
    import sys
    import traceback

    tests = sorted(
        (name, obj)
        for name, obj in dict(globals()).items()
        if name.startswith("test_") and callable(obj)
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
