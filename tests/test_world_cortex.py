"""WorldCortexStore — slot logic, supersession, freshness decay, search, forget."""
import time

import torch

from pseudolife_memory.memory.freshness import ttl_seconds
from pseudolife_memory.memory.world_cortex import ORIGIN, WorldCortexStore


def _emb(*xs):
    return torch.tensor(list(xs), dtype=torch.float32)


def test_insert_and_lookup():
    s = WorldCortexStore()
    action, rec = s.write_fact("anthropic", "latest-model", "opus-4.8",
                               source_url="https://x", source_quote="Opus 4.8 is...",
                               freshness_class="volatile", confidence=0.9)
    assert action == "inserted"
    assert rec.origin == ORIGIN
    got = s.lookup("Anthropic", "Latest Model")   # slot identity is normalised
    assert got is not None and got.value == "opus-4.8"
    assert got.source_url == "https://x"


def test_confirm_refreshes_retrieval_and_keeps_one_record():
    s = WorldCortexStore()
    t0 = 1000.0
    s.write_fact("e", "a", "v1", confidence=0.6, freshness_class="volatile", now=t0)
    action, rec = s.write_fact("e", "a", "v1", confidence=0.8, freshness_class="volatile",
                               source_url="u2", now=t0 + 500)
    assert action == "confirmed"
    assert rec.retrieved_at == t0 + 500     # retrieval anchor refreshed
    assert rec.confidence == 0.8
    assert len(s.current_records()) == 1


def test_newer_source_supersedes():
    s = WorldCortexStore()
    s.write_fact("anthropic", "latest-model", "opus-4.7", now=1000.0)
    action, rec = s.write_fact("anthropic", "latest-model", "opus-4.8", now=2000.0)
    assert action == "superseded"
    assert s.lookup("anthropic", "latest-model").value == "opus-4.8"
    assert rec.supersedes_value == "opus-4.7"
    # exactly one current; the old one is retained as superseded
    cur = s.current_records()
    assert len(cur) == 1 and cur[0].value == "opus-4.8"
    assert any(r.status == "superseded" and r.value == "opus-4.7" for r in s.records)


def test_effective_confidence_decays_with_age():
    s = WorldCortexStore()
    now = time.time()
    _, rec = s.write_fact("e", "a", "v", confidence=0.9, freshness_class="volatile",
                          retrieved_at=now)
    assert abs(rec.effective_confidence(now) - 0.9) < 1e-9       # fresh: full
    aged = rec.effective_confidence(now + ttl_seconds("volatile"))
    assert aged < 0.45                                           # decayed to ~floor
    assert rec.is_stale(now + 2.1 * ttl_seconds("volatile")) is True


def test_evergreen_does_not_decay():
    s = WorldCortexStore()
    now = time.time()
    _, rec = s.write_fact("mamba", "kind", "state-space-model",
                          confidence=0.95, freshness_class="evergreen", retrieved_at=now)
    assert abs(rec.effective_confidence(now + 9999 * 86400.0) - 0.95) < 1e-9
    assert rec.is_stale(now + 9999 * 86400.0) is False


def test_search_by_embedding():
    s = WorldCortexStore()
    s.write_fact("a", "x", "v1", embedding=_emb(1.0, 0.0), freshness_class="evergreen")
    s.write_fact("b", "y", "v2", embedding=_emb(0.0, 1.0), freshness_class="evergreen")
    hits = s.search(_emb(0.9, 0.1), top_k=1)
    assert len(hits) == 1 and hits[0][0].value == "v1"


def test_forget():
    s = WorldCortexStore()
    s.write_fact("dreamtest", "port", "8123")
    s.write_fact("dreamtest", "host", "10.0.0.1")
    s.write_fact("keep", "a", "b")
    removed = s.forget("dreamtest")
    assert removed == 2
    assert s.lookup("dreamtest", "port") is None
    assert s.lookup("keep", "a") is not None
