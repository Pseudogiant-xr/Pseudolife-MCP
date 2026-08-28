"""Timeline retrieval channel (agg-recall Phase 1, knob 2).

Design: docs/superpowers/specs/2026-08-03-aggregation-aware-recall-design.md.
Queries with temporal cues get lexically-relevant entries injected (the
fourth channel beside dense/slot/BM25) and the memory portion of the result
ordered ascending by stream position — the presentation a raw-turns control
gets for free and per-fact consolidation loses. Default off
(``memory.search.timeline_channel``); per-call override pins the bench's
control arm to vanilla retrieval.
"""
from __future__ import annotations

from pseudolife_memory.memory.cms import has_temporal_cue
from pseudolife_memory.service import MemoryService

# The service-backed tests only store and search, so they share conftest's
# module-scoped ``warm_service`` via ``pristine_service`` (bank cleared per
# test, embedder stays warm) instead of building a service apiece. Seeding
# stays per-test; the saving is the construction. A test that mutates
# ``svc.config`` restores it in a ``finally`` — the config outlives the clear.

SEQ = [
    "booked the venue for the launch party",
    "the caterer confirmed the quote for the launch party",
    "zanzibar quartet agreed to play at the launch party",
    "printed the launch party invitations",
    "sent all the launch party invitations out",
]


def _seed(svc: MemoryService) -> None:
    for t in SEQ:
        svc.store(t, source="user")


def test_temporal_cue_detection():
    assert has_temporal_cue("when did I book the venue?")
    assert has_temporal_cue("which came first, the venue or the caterer?")
    assert has_temporal_cue("how many times did we rehearse?")
    assert has_temporal_cue("how long did the printing take?")
    assert has_temporal_cue("what happened in March?")
    assert has_temporal_cue("list the party planning steps in order")
    assert not has_temporal_cue("what is the venue's address?")
    assert not has_temporal_cue("who is playing at the party?")
    # "May" is deliberately not a cue — too common as a modal verb.
    assert not has_temporal_cue("may I bring a guest?")


def test_timeline_off_by_default(pristine_service):
    svc = pristine_service
    _seed(svc)
    got = svc.search("when did the caterer confirm the launch party?",
                     top_k=5)
    assert all(e.get("via") != "timeline" for e in got["entries"])


def test_timeline_orders_memories_chronologically(pristine_service):
    svc = pristine_service
    _seed(svc)
    got = svc.search("what order did the launch party planning happen in?",
                     top_k=5, timeline=True)
    texts = [e["text"] for e in got["entries"] if e["text"] in SEQ]
    assert len(texts) >= 3, texts
    assert texts == sorted(texts, key=SEQ.index), texts


def test_timeline_injects_lexical_matches_with_via_marker():
    """CMS-level with controlled embeddings: an entry sharing the query's
    rare token but embedded orthogonally to it (dense cosine 0, below the
    default floor) is injected by the timeline channel, marked
    ``via="timeline"``, and ordered by stream position after the earlier
    dense hit. A real embedder scores token-sharing texts above the floor
    (probed 2026-08-03: 0.42), so the below-floor case needs basis
    vectors — the service-level tests above cover the integrated path."""
    import torch

    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.utils.config import MemoryConfig

    def _basis(i: int) -> torch.Tensor:
        v = torch.zeros(64)
        v[i] = 1.0
        return v

    cms = ContinuumMemorySystem(MemoryConfig())
    cms.store("ERR-4471 retry storm on the gateway", _basis(0), source="user")
    cms.store("unrelated grocery note mentioning ERR-4471", _basis(1),
              source="user")
    # bm25=False isolates the timeline channel's own injection — with BM25
    # on (the shipped default) the lexical entry enters through BM25 first
    # and timeline correctly declines to duplicate it.
    res = cms.retrieve(_basis(0), top_k=4,
                       query_text="when did ERR-4471 first happen?",
                       timeline=True, bm25=False)
    texts = [e.text for e in res.entries]
    assert "unrelated grocery note mentioning ERR-4471" in texts, texts
    marked = dict(zip(texts, res.via or [None] * len(texts)))
    assert marked["unrelated grocery note mentioning ERR-4471"] == "timeline"
    assert marked["ERR-4471 retry storm on the gateway"] is None
    # Chronological: the earlier store precedes the later one.
    assert (texts.index("ERR-4471 retry storm on the gateway")
            < texts.index("unrelated grocery note mentioning ERR-4471"))
    # Vanilla call (timeline off, bm25 off): the orthogonal entry is
    # not served at all.
    res_off = cms.retrieve(_basis(0), top_k=4,
                           query_text="when did ERR-4471 first happen?",
                           bm25=False)
    assert "unrelated grocery note mentioning ERR-4471" not in [
        e.text for e in res_off.entries]


def test_timeline_non_temporal_query_does_not_fire(pristine_service):
    svc = pristine_service
    _seed(svc)
    got = svc.search("tell me about the zanzibar quartet booking",
                     top_k=5, timeline=True)
    assert all(e.get("via") != "timeline" for e in got["entries"])


def test_timeline_config_default_applies_and_call_override_wins(
        pristine_service):
    svc = pristine_service
    _seed(svc)
    svc.config.memory.search.timeline_channel = True
    try:
        got = svc.search("what order did the launch party planning happen in?",
                         top_k=5)
        texts = [e["text"] for e in got["entries"] if e["text"] in SEQ]
        assert texts == sorted(texts, key=SEQ.index), texts
        # Explicit False pins vanilla retrieval (control arm contract).
        got_off = svc.search("when was the quote confirmed for the party?",
                             top_k=5, timeline=False)
        assert all(e.get("via") != "timeline" for e in got_off["entries"])
    finally:
        # The shared service's config survives the bank clear — restore it or
        # every later test in this module runs with the channel on.
        svc.config.memory.search.timeline_channel = False
