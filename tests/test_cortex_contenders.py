"""Service-level contenders: the provenance guard surfaces a conflicting agent
value as a contender against a user fact, and resolve() promotes/retires it.

Constructs a real MemoryService (offline embedder) against a throwaway data dir.
"""
from __future__ import annotations

import tempfile

from pseudolife_memory.service import MemoryService


def test_store_agent_fact_parks_contender_against_user_fact():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        out = svc.cortex_write("project", "language", "rust", support="agent")
        assert out["action"] == "contested"
        assert out["current"]["value"] == "go"      # user fact still current
        assert out["value"] == "rust"               # the contender (flat record)
        conts = svc.cortex_contenders("project", "language")["contenders"]
        assert len(conts) == 1 and conts[0]["value"] == "rust"


def test_cortex_resolve_accept_then_lookup_returns_new_value_and_persists():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        res = svc.cortex_resolve("project", "language", accept=True)
        assert res["resolved"] is True and res["accepted"] is True
        assert svc.cortex_lookup("project", "language")["value"] == "rust"
        # persisted: a fresh service reads the resolved value
        svc2 = MemoryService(data_dir=d)
        assert svc2.cortex_lookup("project", "language")["value"] == "rust"


def test_cortex_resolve_reject_keeps_current():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        res = svc.cortex_resolve("project", "language", accept=False)
        assert res["resolved"] is True and res["accepted"] is False
        assert svc.cortex_lookup("project", "language")["value"] == "go"
        assert svc.cortex_contenders("project", "language")["contenders"] == []


def test_cortex_resolve_no_contender():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        res = svc.cortex_resolve("project", "language", accept=True)
        assert res["resolved"] is False and res["reason"] == "no_contender"


def test_cortex_search_flags_contested_entries():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("project", "language", "go", support="user")
        svc.cortex_write("project", "language", "rust", support="agent")
        entries = svc.cortex_search("project language", top_k=5)["entries"]
        assert entries and entries[0]["contested"] is True
        assert entries[0]["contender_value"] == "rust"


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
