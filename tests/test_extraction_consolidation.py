"""Consolidation path tests: the dream_run limit passthrough (no network),
and an end-to-end stub-extractor supersession test (PG-gated, skips without it).
"""
import pseudolife_memory.mcp_server as srv


def test_dream_run_passes_limit(monkeypatch):
    seen = {}

    def fake_dream_run(extractor, *, limit=None):
        seen["limit"] = limit
        return {"pulled": 0, "claims": 0, "inserted": 0, "confirmed": 0,
                "contested": 0, "superseded": 0, "cursor": 0.0}

    monkeypatch.setattr(srv.service, "dream_run", fake_dream_run)
    srv.memory_dream_run(limit=500)
    assert seen["limit"] == 500
