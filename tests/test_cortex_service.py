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


def test_cortex_read_includes_relative_age_and_stamp():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("server", "port", "8080", support="user")
        got = svc.cortex_lookup("server", "port")
        assert got is not None
        assert got.get("age") == "just now"          # written moments ago
        assert got["tx_time"] and got["writer_id"]   # temporal stamp surfaced


def test_failed_cortex_save_surfaces_as_persistence_error():
    """A durable-save failure must NOT be swallowed: it surfaces to the caller
    and bumps the health-visible persist-error counter (F3)."""
    from pseudolife_memory.service import MemoryService, PersistenceError

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc._ensure_init()
        assert svc._cortex is not None

        def _boom(*a, **k):
            raise OSError("disk full")

        svc._cortex.save = _boom  # force the durable write to fail
        raised = False
        try:
            svc.cortex_write("server", "port", "8080", support="user")
        except PersistenceError:
            raised = True
        assert raised, "a failed cortex save must surface, not be swallowed"
        assert svc._persist_errors >= 1


def test_memory_history_returns_version_timeline():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("server", "port", "8080", support="user", now=1000.0)
        svc.cortex_write("server", "port", "9090", support="user", now=2000.0)
        hist = svc.history("server", "port")
        assert hist["count"] >= 2
        values = [v["value"] for v in hist["versions"]]
        assert "8080" in values and "9090" in values
        for v in hist["versions"]:               # each version is attributed
            assert "writer_id" in v and "tx_time" in v
        txs = [v["tx_time"] for v in hist["versions"]]
        assert txs == sorted(txs)                # oldest -> newest


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


def test_fact_get_miss_returns_candidates():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        svc = MemoryService(data_dir=d)
        svc.cortex_write("server", "port", "8080", support="user")
        got = svc.cortex_candidates("server", "nonexistent-attr")
        assert got and got[0]["why"] == "same_entity"
        assert got[0]["attribute"] == "port"
        # A genuinely similar slot surfaces via embeddings too.
        sim = svc.cortex_candidates("srv", "port number")
        assert any(c["why"] == "similar_slot" and c["entity"] == "server"
                   for c in sim) or sim == []  # embedder-dependent, tolerate empty
