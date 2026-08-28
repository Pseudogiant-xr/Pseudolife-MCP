"""Temporal-contiguity expansion on memory_search (agg-recall Phase 1, knob 1).

Design: docs/superpowers/specs/2026-08-03-aggregation-aware-recall-design.md.
A search hit optionally surfaces its temporal neighbors — same episode,
falling back to same source — ordered by timestamp, marked ``via:
"contiguity"`` so consumers (and the eval harness) can tell expansion hits
from direct hits. Default off (``memory.search.contiguity_neighbors = 0``);
a per-call override exists so the bench can pin its control arm to vanilla
retrieval regardless of config.
"""
from __future__ import annotations

from pathlib import Path

from pseudolife_memory.service import MemoryService
from pseudolife_memory.utils.config import AppConfig, load_config

# Every service-backed test here only stores and searches, so they share
# conftest's module-scoped ``warm_service`` via ``pristine_service`` (bank
# cleared per test, embedder stays warm). Seeding stays per-test — it is the
# service construction, not the five stores, that cost. A test that mutates
# ``svc.config`` must restore it in a ``finally``: the config object outlives
# the bank clear.


def test_search_config_defaults():
    cfg = AppConfig()
    assert cfg.memory.search.contiguity_neighbors == 0
    assert cfg.memory.search.timeline_channel is False


def test_yaml_search_block_parses(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "memory:\n  search:\n    contiguity_neighbors: 2\n"
        "    timeline_channel: true\n",
        encoding="utf-8")
    cfg = load_config(p)
    assert cfg.memory.search.contiguity_neighbors == 2
    assert cfg.memory.search.timeline_channel is True
    # A memory: block that omits search keeps the dataclass defaults.
    p2 = tmp_path / "config2.yaml"
    p2.write_text("memory:\n  top_k: 4\n", encoding="utf-8")
    cfg2 = load_config(p2)
    assert cfg2.memory.search.contiguity_neighbors == 0
    assert cfg2.memory.search.timeline_channel is False


def _seed_sequence(svc: MemoryService, episode: str = "ep-a") -> list[str]:
    """Five distinctive entries stored in order; returns the texts."""
    texts = [
        "monday: booked the venue for the launch party",
        "tuesday: the caterer confirmed the quote",
        "wednesday: zanzibar quartet agreed to play the launch party",
        "thursday: printed the invitations",
        "friday: sent all the invitations out",
    ]
    for t in texts:
        svc.store(t, source="user", episode=episode)
    return texts


def test_contiguity_off_by_default_is_identical(pristine_service):
    svc = pristine_service
    _seed_sequence(svc)
    got = svc.search("zanzibar quartet launch party", top_k=3)
    assert all("via" not in e for e in got["entries"]), got["entries"]


def test_contiguity_expands_hits_with_temporal_neighbors(pristine_service):
    svc = pristine_service
    _seed_sequence(svc)
    # min_score high enough that only the distinctive hit survives the
    # dense gate; neighbors are structural context, not scored hits.
    got = svc.search("zanzibar quartet", top_k=3, min_score=0.5,
                     contiguity_neighbors=1)
    entries = got["entries"]
    texts = [e["text"] for e in entries]
    assert any("zanzibar" in t for t in texts)
    # One neighbor each side, in stream order around the hit.
    assert any("caterer confirmed" in t for t in texts), texts
    assert any("printed the invitations" in t for t in texts), texts
    via = {e["text"]: e.get("via") for e in entries}
    assert via["tuesday: the caterer confirmed the quote"] == "contiguity"
    assert via["thursday: printed the invitations"] == "contiguity"
    # The direct hit is not via-marked.
    hit = next(t for t in texts if "zanzibar" in t)
    assert via[hit] is None or "via" not in next(
        e for e in entries if e["text"] == hit)
    # Neighbors sit adjacent to their parent hit: prev, hit, next.
    zi = texts.index(hit)
    assert texts[zi - 1] == "tuesday: the caterer confirmed the quote"
    assert texts[zi + 1] == "thursday: printed the invitations"


def test_contiguity_neighbors_never_duplicate_direct_hits(pristine_service):
    svc = pristine_service
    _seed_sequence(svc)
    # Broad query: several entries are direct hits already.
    got = svc.search("launch party invitations", top_k=5,
                     contiguity_neighbors=2)
    texts = [e["text"] for e in got["entries"]]
    assert len(texts) == len(set(texts)), texts


def test_temporal_neighbors_scope_and_tie_break(pristine_service):
    """Pure lookup semantics at the CMS layer (service-level episode
    attribution needs a real open-episode handle, so scope is unit-tested
    with hand-built entries): episode-carrying entries only neighbor their
    own episode; episode-less entries only neighbor episode-less entries of
    the same source; same-tick entries order by ``seq``."""
    import torch

    from pseudolife_memory.memory.titans_memory import MemoryEntry

    def _e(text, ts, seq, episode=None, source="user"):
        return MemoryEntry(text=text, embedding=torch.zeros(8),
                           timestamp=ts, seq=seq, episode_id=episode,
                           source=source)

    # Hand-built entries go straight into ``cms.bands[0].entries``, bypassing
    # the write API — safe on the shared service because ``cms.clear()``
    # empties exactly that list between tests.
    svc = pristine_service
    cms = svc._cms
    a1 = _e("a1", 1.0, 101, episode="ep-a")
    a2 = _e("a2", 2.0, 102, episode="ep-a")
    a3 = _e("a3", 2.0, 103, episode="ep-a")     # same tick as a2
    b1 = _e("b1", 1.5, 104, episode="ep-b")
    loose = _e("loose", 1.6, 105, source="user")
    cms.bands[0].entries.extend([a1, a2, a3, b1, loose])

    before, after = cms.temporal_neighbors(a2, 2)
    assert [e.text for e in before] == ["a1"]
    assert [e.text for e in after] == ["a3"]     # seq breaks the tie

    # Episode-less anchor: same-source episode-less entries only.
    before, after = cms.temporal_neighbors(loose, 3)
    assert all(e.episode_id is None for e in before + after)
    assert not any(e.text in ("a1", "a2", "a3", "b1")
                   for e in before + after)


def test_contiguity_config_default_applies_and_call_override_wins(
        pristine_service):
    svc = pristine_service
    _seed_sequence(svc)
    svc.config.memory.search.contiguity_neighbors = 1
    try:
        got = svc.search("zanzibar quartet", top_k=3, min_score=0.5)
        assert any(e.get("via") == "contiguity" for e in got["entries"])
        # Explicit 0 pins vanilla retrieval even with config on — the
        # bench's control arm depends on this.
        got_off = svc.search("zanzibar quartet", top_k=3, min_score=0.5,
                             contiguity_neighbors=0)
        assert all(e.get("via") is None for e in got_off["entries"])
    finally:
        # The shared service's config survives the bank clear — restore it or
        # every later test in this module runs with contiguity on.
        svc.config.memory.search.contiguity_neighbors = 0
