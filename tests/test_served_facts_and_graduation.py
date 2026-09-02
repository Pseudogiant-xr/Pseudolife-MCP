"""Served facts on the retrieval log (schema v34) + the graduation report.

Feature 1: ``search(return_event_id=True)`` hands back the id of the event
row it just wrote; ``attach_served_facts`` then records the cortex slots
the caller's cortex-first block served, by exact event id. The export read
(``retrieval_events_window``) carries the column so a Phase-1 reranker can
train on the fact half of the response too.

Feature 2: ``graduation_report`` — entries served in a high share of
recent distinct sessions are static-context candidates ("graduate to
CLAUDE.md"); computed from the existing event log, surfaced in
``memory_stats`` ``read_audit``.

Skips cleanly without a PG server (mirrors test_retrieval_log.py).
"""

from __future__ import annotations

import pytest

from tests.helpers import reload_mcp_filemode as _reload_mcp_filemode
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    s = PostgresStorage(pg_url)
    yield s
    s.close()


@pytest.fixture()
def svc(pg_conn, pg_url, tmp_path):
    from pseudolife_memory.service import MemoryService

    yield MemoryService(data_dir=tmp_path, database_url=pg_url)


def _served(*entry_ids: int) -> list[dict]:
    return [
        {"entry_id": eid, "score": 0.9 - 0.1 * rank, "rank": rank,
         "via": None, "bank": "flat"}
        for rank, eid in enumerate(entry_ids)
    ]


def _stored_entry_id(svc, text: str) -> int:
    svc.store(text, source="test")
    res = svc.search(text)
    assert res["count"] >= 1
    return int(res["entries"][0]["id"])


# ------------------------------------------------------------------
# Feature 1 — served facts on the event log
# ------------------------------------------------------------------

def test_attach_served_facts_round_trips(storage):
    eid = storage.add_retrieval_event("q", _served(7), session_id="s-1",
                                      now=1000.0)
    facts = [{"entity_norm": "dev-box", "attribute_norm": "gpu", "rank": 0,
              "score": 0.83, "kind": "scalar", "contested": False}]
    storage.attach_served_facts(eid, facts)
    ev = {e["id"]: e for e in storage.retrieval_events_window()}[eid]
    assert ev["served_facts"] == facts


def test_events_without_facts_export_null(storage):
    eid = storage.add_retrieval_event("q", _served(7), session_id="s-1",
                                      now=1000.0)
    ev = {e["id"]: e for e in storage.retrieval_events_window()}[eid]
    assert ev["served_facts"] is None


def test_search_returns_event_id_only_on_request(svc):
    text = "the quick brown fox jumps over the lazy dog"
    svc.store(text, source="test")
    plain = svc.search(text)
    assert "retrieval_event_id" not in plain, (
        "the event id is training plumbing — it must be opt-in, not part "
        "of the public search shape")
    res = svc.search(text, return_event_id=True)
    assert isinstance(res.get("retrieval_event_id"), int)
    # Kill-switch: no event row means no id, even when requested.
    svc.config.memory.retrieval_log.enabled = False
    res = svc.search(text, return_event_id=True)
    assert "retrieval_event_id" not in res


def test_service_attach_norms_and_records(svc):
    text = "the quick brown fox jumps over the lazy dog"
    svc.store(text, source="test")
    svc.cortex_write("Dev Box", "GPU", "RTX 4090", support="user")
    res = svc.search(text, return_event_id=True)
    evt = res["retrieval_event_id"]
    facts = svc.cortex_search("what gpu does the dev box have",
                              top_k=3).get("entries", [])
    assert facts
    svc.attach_served_facts(evt, facts)
    ev = {e["id"]: e for e in svc._storage.retrieval_events_window()}[evt]
    sf = ev["served_facts"]
    assert sf and sf[0]["entity_norm"] == "dev-box"
    assert sf[0]["attribute_norm"] == "gpu"
    assert sf[0]["rank"] == 0
    assert sf[0]["kind"] == "scalar"
    # v35: the log says whether rank 0 was earned by score or by a pin —
    # a reranker trained on it must not learn a 0.37-cosine fact as rank 0.
    assert sf[0]["pinned"] is False
    facts[0]["pinned"] = True
    res = svc.search(text, return_event_id=True)
    svc.attach_served_facts(res["retrieval_event_id"], facts)
    ev = {e["id"]: e for e in svc._storage.retrieval_events_window()}[
        res["retrieval_event_id"]]
    assert ev["served_facts"][0]["pinned"] is True


def test_service_attach_failure_is_swallowed(svc, monkeypatch):
    text = "the quick brown fox jumps over the lazy dog"
    svc.store(text, source="test")
    res = svc.search(text, return_event_id=True)

    def _boom(*a, **k):
        raise RuntimeError("simulated attach failure")

    monkeypatch.setattr(svc._storage, "attach_served_facts", _boom)
    before = svc._retrieval_log_errors
    svc.attach_served_facts(res["retrieval_event_id"],
                            [{"entity": "x", "attribute": "y"}])
    assert svc._retrieval_log_errors == before + 1


# ------------------------------------------------------------------
# Feature 1 — MCP handler wiring (file mode, no PG; mirrors
# test_abstain.py's reload pattern)
# ------------------------------------------------------------------

_FACT ={"entity": "checkout-service", "attribute": "default port",
         "value": "9090", "origin": "agent", "confidence": 0.8,
         "score": 0.7}


def _patch_handler_flow(mod, monkeypatch, *, facts: bool):
    """Stub the service under the real memory_search handler: search
    honours return_event_id (asserting the handler asked for it), the
    cortex serves one fact or none, and attach calls are captured."""
    attached: list[tuple] = []

    def _search(**kw):
        assert kw.get("return_event_id") is True, (
            "the handler must request the event id")
        return {"query": kw.get("query", ""), "count": 0, "entries": [],
                "low_confidence": False, "retrieval_event_id": 42}

    monkeypatch.setattr(mod.service, "search", _search)
    monkeypatch.setattr(
        mod.service, "cortex_search",
        lambda *a, **k: {"entries": [_FACT] if facts else []})
    monkeypatch.setattr(
        mod.service, "attach_served_facts",
        lambda evt, f: attached.append((evt, f)))
    monkeypatch.setattr(
        mod.service, "trace", lambda **kw: {"trace": {}})
    return attached


def test_handler_never_returns_the_event_id(tmp_path, monkeypatch):
    # The invariant the feature rests on: the id is training plumbing and
    # must be popped before ANY return path — compact, verbose, explain.
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    _patch_handler_flow(mod, monkeypatch, facts=True)
    assert "retrieval_event_id" not in mod.memory_search("q")
    assert "retrieval_event_id" not in mod.memory_search("q", verbose=True)
    assert "retrieval_event_id" not in mod.memory_search("q", explain=True)


def test_handler_attaches_facts_to_the_event(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    attached = _patch_handler_flow(mod, monkeypatch, facts=True)
    mod.memory_search("q")
    assert attached and attached[0][0] == 42
    assert attached[0][1][0]["entity"] == "checkout-service"


def test_handler_skips_attach_without_facts(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    attached = _patch_handler_flow(mod, monkeypatch, facts=False)
    res = mod.memory_search("q")
    assert attached == []
    assert "retrieval_event_id" not in res


# ------------------------------------------------------------------
# Feature 2 — graduation report
# ------------------------------------------------------------------

def test_graduation_report_share_math(svc):
    eid_hot = _stored_entry_id(svc, "the quick brown fox jumps the lazy dog")
    eid_cold = _stored_entry_id(svc, "a completely different cold memory")
    # hot entry served in 4 of 5 sessions; cold in 1 of 5.
    for i in range(4):
        svc._storage.add_retrieval_event(
            "q", _served(eid_hot), session_id=f"s-{i}", now=1000.0 + i)
    svc._storage.add_retrieval_event(
        "q", _served(eid_cold), session_id="s-4", now=1004.0)
    report = svc._storage.graduation_report(
        window_days=30.0, min_sessions=5, min_share=0.6, limit=10,
        now=2000.0)
    ids = [r["entry_id"] for r in report]
    assert eid_hot in ids and eid_cold not in ids
    hot = next(r for r in report if r["entry_id"] == eid_hot)
    assert hot["sessions_served"] == 4 and hot["sessions_total"] == 5
    assert hot["share"] == 0.8
    assert hot["source"] == "test" and hot["text"]


def test_graduation_report_gates_on_min_sessions(svc):
    eid = _stored_entry_id(svc, "the quick brown fox jumps the lazy dog")
    for i in range(3):
        svc._storage.add_retrieval_event(
            "q", _served(eid), session_id=f"s-{i}", now=1000.0 + i)
    # 3 of 3 sessions is 100% share, but 3 sessions is below the floor —
    # a young log must not nominate candidates.
    assert svc._storage.graduation_report(
        window_days=30.0, min_sessions=5, min_share=0.6, now=2000.0) == []


def test_graduation_report_drops_evicted_entries(svc, pg_conn):
    svc.store("warm-up entry to initialise storage", source="test")
    for i in range(5):
        svc._storage.add_retrieval_event(
            "q", _served(999999), session_id=f"s-{i}", now=1000.0 + i)
    # Served ids carry no FK; an evicted/never-existing entry cannot be
    # promoted and must not appear.
    assert svc._storage.graduation_report(
        window_days=30.0, min_sessions=3, min_share=0.6, now=2000.0) == []


def test_stats_carries_graduation_candidates(svc):
    svc.store("the quick brown fox jumps over the lazy dog", source="test")
    audit = svc.stats().get("read_audit")
    assert audit is not None
    assert isinstance(audit.get("graduation_candidates"), list)


def test_graduation_failure_does_not_nuke_read_audit(svc, monkeypatch):
    # An advisory report must not destroy the audit it rides in: a
    # graduation failure degrades to [], the v33 audit survives intact.
    svc.store("the quick brown fox jumps over the lazy dog", source="test")

    def _boom(*a, **k):
        raise RuntimeError("simulated graduation failure")

    monkeypatch.setattr(svc._storage, "graduation_report", _boom)
    audit = svc.stats().get("read_audit")
    assert not audit.get("unavailable")
    assert audit["entries"]["total"] >= 1
    assert audit["graduation_candidates"] == []
