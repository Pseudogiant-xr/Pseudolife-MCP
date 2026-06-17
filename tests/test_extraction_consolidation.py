"""Consolidation path tests: the dream_run limit passthrough (no network),
and an end-to-end stub-extractor supersession test (PG-gated, skips without it).
"""
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (pytest fixtures)

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


def test_consolidation_inserts_then_supersedes_via_stub_extractor(pg_conn, pg_url, tmp_path):
    """End-to-end consolidation without a live model: a stub extractor's claim is
    written to the cortex (insert), and a later higher-tier consolidation
    supersedes the stale value. Proves dream_run's pull -> extract ->
    cortex_write path. Skips cleanly when no test Postgres (pg_conn/pg_url)."""
    from pseudolife_memory.service import MemoryService
    from pseudolife_memory.memory.dream import Claim

    def stub(value, *, confidence, origin):
        class _Stub:
            def extract(self, texts, vocab):
                return [Claim(entity="checkout-service", attribute="default port",
                              value=value, confidence=confidence, origin=origin)]
        return _Stub()

    svc = MemoryService(data_dir=tmp_path, database_url=pg_url)

    def drain(extractor):
        while svc.dream_run(extractor, limit=100)["pulled"]:
            pass

    # 1) Consolidation inserts into the (empty) slot.
    svc.store("checkout-service default port note", source="t")
    drain(stub("9090", confidence=0.8, origin="agent"))
    rec = svc.cortex_lookup("checkout-service", "default port")
    assert rec is not None and rec["value"] == "9090"

    # 2) A later, higher-tier (user) consolidation supersedes the stale value.
    svc.store("checkout-service default port revised", source="t")
    drain(stub("9595", confidence=0.9, origin="user"))
    rec = svc.cortex_lookup("checkout-service", "default port")
    assert rec is not None and rec["value"] == "9595"
