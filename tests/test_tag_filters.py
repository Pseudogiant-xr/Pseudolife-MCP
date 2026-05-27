"""Tag plumbing through ``cms.store`` + retrieval filters (Tier C C-4).

Two surfaces exercised here:

* ``CMS.store(text, embedding, source, tags=...)`` carries the
  normalised tag list through to the stored entry, including the
  promotion path.
* ``CMS.retrieve(..., tags=[...], episodes=[...])`` drops entries whose
  tags / episode-id are not in the respective filter lists. AND-combined
  with the existing ``sources`` / ``bands`` filters.

Filter semantics: an entry passes ``tags=`` if **any** of the
filter's tags appear in the entry's tag set (intersection non-empty).
Episode filter is OR within the filter list, AND with the other
filters.

Edge cases:
* ``tags=None`` / ``episodes=None`` — no filter, default behaviour.
* Empty list filter — explicit ``[]`` is treated as "no filter" rather
  than "filter that matches nothing", which would silently drop every
  hit on a typo. Documented behaviour.
"""

from __future__ import annotations

import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.utils.config import MemoryConfig


def _fresh_cms() -> ContinuumMemorySystem:
    cfg = MemoryConfig()
    cfg.surprise_threshold = -1.0  # disable the surprise gate
    return ContinuumMemorySystem(cfg)


def _find_by_text(cms: ContinuumMemorySystem, text: str) -> MemoryEntry | None:
    for band in cms.bands:
        for e in band.entries:
            if e.text == text:
                return e
    return None


# ── Store-side tag plumbing ──────────────────────────────────────────────


def test_store_carries_tags_to_entry() -> None:
    cms = _fresh_cms()
    cms.store(
        "first fact",
        torch.randn(cms.config.embedding_dim),
        source="test",
        tags=["alpha", "beta"],
    )
    entry = _find_by_text(cms, "first fact")
    assert entry is not None
    assert entry.tags == ["alpha", "beta"]


def test_store_normalises_tags_lowercase_dedup_strip() -> None:
    cms = _fresh_cms()
    cms.store(
        "norm test",
        torch.randn(cms.config.embedding_dim),
        source="test",
        tags=["Alpha", " beta ", "alpha", "BETA"],
    )
    entry = _find_by_text(cms, "norm test")
    assert entry is not None
    assert entry.tags == ["alpha", "beta"]  # dedup preserves first-seen order


def test_store_with_no_tags_keeps_default_empty_list() -> None:
    cms = _fresh_cms()
    cms.store(
        "no tags here",
        torch.randn(cms.config.embedding_dim),
        source="test",
    )
    entry = _find_by_text(cms, "no tags here")
    assert entry is not None
    assert entry.tags == []


def test_store_with_explicit_none_tags_keeps_default_empty_list() -> None:
    cms = _fresh_cms()
    cms.store(
        "explicit none",
        torch.randn(cms.config.embedding_dim),
        source="test",
        tags=None,
    )
    entry = _find_by_text(cms, "explicit none")
    assert entry is not None
    assert entry.tags == []


# ── Retrieval tag/episode filters ────────────────────────────────────────


def _seed_bank(cms: ContinuumMemorySystem) -> dict[str, torch.Tensor]:
    """Seed a bank with 4 known entries spread across tags + episodes.

    Uses deliberately-similar embeddings (shared base + small jitter) so
    the test isolates the *filter* behaviour from embedding similarity
    chance. With ``min_score=0.0`` on the retrieve call, every entry
    that survives the filter set must surface — that's what the asserts
    rely on.
    """
    torch.manual_seed(42)
    dim = cms.config.embedding_dim
    base = torch.randn(dim)
    base = base / base.norm()

    def _near() -> torch.Tensor:
        v = base + 0.01 * torch.randn(dim)
        return v / v.norm()

    embs = {
        "alpha-tagged": _near(),
        "beta-tagged": _near(),
        "both-tagged": _near(),
        "untagged": _near(),
    }
    ep_a = cms.episodes.start("alpha-ep")
    cms.store("alpha-tagged", embs["alpha-tagged"], source="t", tags=["alpha"])
    cms.episodes.end()
    ep_b = cms.episodes.start("beta-ep")
    cms.store("beta-tagged", embs["beta-tagged"], source="t", tags=["beta"])
    cms.store("both-tagged", embs["both-tagged"], source="t", tags=["alpha", "beta"])
    cms.episodes.end()
    cms.store("untagged", embs["untagged"], source="t", tags=[])
    # Stash ids on the dict for the tests' convenience.
    embs["__ep_a__"] = ep_a.id  # type: ignore[assignment]
    embs["__ep_b__"] = ep_b.id  # type: ignore[assignment]
    return embs


def _texts(result) -> set[str]:
    return {e.text for e in result.entries}


def test_retrieve_with_tag_filter_returns_only_matching() -> None:
    cms = _fresh_cms()
    embs = _seed_bank(cms)
    result = cms.retrieve(
        embs["alpha-tagged"], top_k=10, min_score=0.0, tags=["alpha"],
    )
    surfaced = _texts(result)
    assert "alpha-tagged" in surfaced
    assert "both-tagged" in surfaced
    assert "beta-tagged" not in surfaced
    assert "untagged" not in surfaced


def test_retrieve_with_multi_tag_filter_is_or_within_list() -> None:
    cms = _fresh_cms()
    embs = _seed_bank(cms)
    result = cms.retrieve(
        embs["alpha-tagged"], top_k=10, min_score=0.0, tags=["alpha", "beta"],
    )
    surfaced = _texts(result)
    # All three tagged entries surface; the untagged one is dropped.
    assert "alpha-tagged" in surfaced
    assert "beta-tagged" in surfaced
    assert "both-tagged" in surfaced
    assert "untagged" not in surfaced


def test_retrieve_with_empty_tag_list_is_no_filter() -> None:
    """Explicit ``tags=[]`` is interpreted as 'no filter' — the safer
    default than 'drop everything'."""
    cms = _fresh_cms()
    embs = _seed_bank(cms)
    result = cms.retrieve(
        embs["alpha-tagged"], top_k=10, min_score=0.0, tags=[],
    )
    surfaced = _texts(result)
    assert "alpha-tagged" in surfaced
    assert "untagged" in surfaced


def test_retrieve_with_episode_filter_returns_only_matching() -> None:
    cms = _fresh_cms()
    embs = _seed_bank(cms)
    ep_a_id = embs["__ep_a__"]
    result = cms.retrieve(
        embs["alpha-tagged"], top_k=10, min_score=0.0, episodes=[ep_a_id],
    )
    surfaced = _texts(result)
    assert "alpha-tagged" in surfaced
    # Entries from ep_b or no-episode are filtered out.
    assert "beta-tagged" not in surfaced
    assert "both-tagged" not in surfaced
    assert "untagged" not in surfaced


def test_retrieve_combines_tag_and_episode_filters_with_and() -> None:
    cms = _fresh_cms()
    embs = _seed_bank(cms)
    ep_b_id = embs["__ep_b__"]
    # Both filters: tags must include "alpha" AND episode must be ep_b.
    # Only "both-tagged" qualifies (alpha+beta tags, ep_b).
    result = cms.retrieve(
        embs["both-tagged"],
        top_k=10,
        min_score=0.0,
        tags=["alpha"],
        episodes=[ep_b_id],
    )
    surfaced = _texts(result)
    assert "both-tagged" in surfaced
    assert "alpha-tagged" not in surfaced  # wrong episode
    assert "beta-tagged" not in surfaced  # missing alpha tag


def test_retrieve_trace_records_tag_and_episode_filters() -> None:
    cms = _fresh_cms()
    embs = _seed_bank(cms)
    ep_a_id = embs["__ep_a__"]
    _result, trace = cms.retrieve_with_trace(
        embs["alpha-tagged"],
        top_k=10,
        tags=["alpha"],
        episodes=[ep_a_id],
    )
    assert trace["filters"]["tags"] == ["alpha"]
    assert trace["filters"]["episodes"] == [ep_a_id]
