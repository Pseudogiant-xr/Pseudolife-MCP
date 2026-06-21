"""Schema v6 — additive ``episode_id`` / ``episode_title`` / ``tags`` on
``MemoryEntry`` (Tier C C-1).

Three concerns covered here:

* The dataclass defaults are right (no surprise leakage into the rest of
  the system when callers omit the new fields).
* A save/load cycle preserves the new fields. This is the integration
  point that breaks the most often when adding schema fields — the
  band-level ``get_state_dict``/``load_state_dict`` need parallel
  updates.
* Pre-v6 saves (schema_version=5) load cleanly with the new fields
  defaulted, so existing on-disk state isn't invalidated by the bump.

Also covers the pre-existing v5 bug where ``superseded_by_text`` was
declared part of the schema but not actually persisted by
``MIRASBand.get_state_dict`` — fixed on the same pass since C-1 touches
exactly that serialisation surface.
"""

from __future__ import annotations

from pathlib import Path

import torch

from pseudolife_memory.memory.cms import SCHEMA_VERSION, ContinuumMemorySystem
from pseudolife_memory.memory.titans_memory import MemoryEntry
from pseudolife_memory.utils.config import MemoryConfig


# ── Dataclass defaults ────────────────────────────────────────────────────


def test_memory_entry_episode_fields_default_to_none() -> None:
    entry = MemoryEntry(text="x", embedding=torch.zeros(4))
    assert entry.episode_id is None
    assert entry.episode_title is None


def test_memory_entry_tags_defaults_to_empty_list() -> None:
    entry = MemoryEntry(text="x", embedding=torch.zeros(4))
    assert entry.tags == []
    # Distinct list per instance — no shared mutable default.
    other = MemoryEntry(text="y", embedding=torch.zeros(4))
    entry.tags.append("a")
    assert other.tags == []


def test_memory_entry_accepts_explicit_schema_v6_fields() -> None:
    entry = MemoryEntry(
        text="x",
        embedding=torch.zeros(4),
        episode_id="ep-abc",
        episode_title="Tier C work",
        tags=["pseudolife", "tier-c"],
    )
    assert entry.episode_id == "ep-abc"
    assert entry.episode_title == "Tier C work"
    assert entry.tags == ["pseudolife", "tier-c"]


# ── Save / load round-trip ────────────────────────────────────────────────


def _append_entry(cms: ContinuumMemorySystem, **fields: object) -> MemoryEntry:
    """Inject an entry directly into the first band, bypassing the gates.

    The gating pipeline (meta filter, surprise, contradiction) is exercised
    elsewhere; here we're testing the persistence surface, so a direct
    append keeps the test independent of the band's tuning. The band is
    marked dirty so save sees the change.
    """
    cfg_dim = cms.config.embedding_dim
    entry = MemoryEntry(
        text=fields.pop("text", "round-trip entry"),  # type: ignore[arg-type]
        embedding=torch.zeros(cfg_dim),
        bank=cms.bands[0].name,
        **fields,  # type: ignore[arg-type]
    )
    cms.bands[0].entries.append(entry)
    cms.bands[0]._dirty = True  # noqa: SLF001
    return entry


def test_schema_version_constant_is_v6() -> None:
    """The bump itself — protects against regressions on the version field."""
    assert SCHEMA_VERSION == 6


def test_save_load_preserves_episode_and_tag_fields(tmp_path: Path) -> None:
    cfg = MemoryConfig()
    cms_a = ContinuumMemorySystem(cfg)
    _append_entry(
        cms_a,
        text="hello",
        source="test",
        episode_id="ep-42",
        episode_title="round-trip session",
        tags=["alpha", "beta"],
    )
    cms_a.save(tmp_path)

    cms_b = ContinuumMemorySystem(cfg)
    cms_b.load(tmp_path)
    restored = cms_b.bands[0].entries
    assert len(restored) == 1
    assert restored[0].episode_id == "ep-42"
    assert restored[0].episode_title == "round-trip session"
    assert restored[0].tags == ["alpha", "beta"]


def test_pre_v6_save_loads_with_defaults(tmp_path: Path) -> None:
    """Hand-craft a v5-shaped saved state and confirm it loads cleanly with
    ``episode_id=None`` / ``tags=[]`` defaults — i.e. existing on-disk
    state from a v0.1.0 install survives the bump.
    """
    cfg = MemoryConfig()
    cms = ContinuumMemorySystem(cfg)
    band_name = cms.bands[0].name
    # Build a v5-shaped state by hand — note: no episode_id / episode_title
    # / tags keys on the entry dict, schema_version=5.
    state = {
        "schema_version": 5,
        "preset_name": cfg.miras.preset,
        "chain_residual": getattr(cfg.miras, "chain_residual", False),
        "bands": {
            band_name: {
                # Legacy pre-v0.5 MLP weight blocks — the loader ignores these.
                "memory_state": {"net.0.weight": torch.zeros(2, 2)},
                "optimizer_state": {"name": "sgd_momentum"},
                "surprise_ema": 0.0,
                "axes": {},
                "entries": [
                    {
                        "text": "legacy entry",
                        "embedding": torch.zeros(cfg.embedding_dim),
                        "surprise_score": 0.0,
                        "timestamp": 0.0,
                        "access_count": 0,
                        "source": "legacy",
                        "superseded_at": None,
                        "last_logical_turn": None,
                        "slots": [],
                    }
                ],
            }
        },
        "interaction_count": 1,
        "logical_turn_count": 0,
        "surprise_history": {},
        "consolidation_events": [],
        "tier_hits": {},
        "tier_queries": 0,
    }
    save_dir = tmp_path
    save_dir.mkdir(exist_ok=True)
    torch.save(state, save_dir / "cms_state.pt")

    fresh = ContinuumMemorySystem(cfg)
    fresh.load(save_dir)
    entries = fresh.bands[0].entries
    assert len(entries) == 1
    assert entries[0].text == "legacy entry"
    # New v6 fields default to None / []:
    assert entries[0].episode_id is None
    assert entries[0].episode_title is None
    assert entries[0].tags == []


# ── EpisodeManager wired into CMS ─────────────────────────────────────────


def test_cms_has_episode_manager_on_construct() -> None:
    from pseudolife_memory.memory.episodes import EpisodeManager
    cfg = MemoryConfig()
    cms = ContinuumMemorySystem(cfg)
    assert isinstance(cms.episodes, EpisodeManager)
    assert cms.episodes.current_id is None


def _find_by_text(cms: ContinuumMemorySystem, text: str) -> MemoryEntry | None:
    """Locate an entry across all bands by exact text.

    Robust against the promotion chain: continuum-preset stores may move
    the entry past ``bands[0]`` before ``store()`` returns.
    """
    for band in cms.bands:
        for entry in band.entries:
            if entry.text == text:
                return entry
    return None


def test_store_stamps_entry_with_open_episode() -> None:
    cfg = MemoryConfig()
    # Lower the surprise threshold so the test store doesn't get gated out.
    cfg.surprise_threshold = -1.0
    cms = ContinuumMemorySystem(cfg)
    ep = cms.episodes.start("active session")
    stored, _ = cms.store(
        "claude noticed something",
        torch.randn(cfg.embedding_dim),
        source="test",
    )
    assert stored
    entry = _find_by_text(cms, "claude noticed something")
    assert entry is not None
    assert entry.episode_id == ep.id
    assert entry.episode_title == "active session"


def test_store_does_not_stamp_when_no_episode_open() -> None:
    cfg = MemoryConfig()
    cfg.surprise_threshold = -1.0
    cms = ContinuumMemorySystem(cfg)
    stored, _ = cms.store(
        "drifting thought",
        torch.randn(cfg.embedding_dim),
        source="test",
    )
    assert stored
    entry = _find_by_text(cms, "drifting thought")
    assert entry is not None
    assert entry.episode_id is None
    assert entry.episode_title is None


def test_save_load_round_trips_episode_manager_state(tmp_path: Path) -> None:
    cfg = MemoryConfig()
    cms_a = ContinuumMemorySystem(cfg)
    a = cms_a.episodes.start("first")
    cms_a.episodes.end()
    b = cms_a.episodes.start("second-open")  # leave open
    cms_a.save(tmp_path)

    cms_b = ContinuumMemorySystem(cfg)
    cms_b.load(tmp_path)
    assert set(cms_b.episodes.episodes.keys()) == {a.id, b.id}
    assert cms_b.episodes.current_id == b.id
    assert cms_b.episodes.get(a.id).ended_at is not None  # type: ignore[union-attr]
    assert cms_b.episodes.get(b.id).ended_at is None  # type: ignore[union-attr]


def test_pre_v6_save_loads_with_empty_episode_manager(tmp_path: Path) -> None:
    """Pre-v6 saves have no ``episodes`` block — confirm CMS load handles
    this gracefully (empty EpisodeManager, no crash)."""
    cfg = MemoryConfig()
    cms = ContinuumMemorySystem(cfg)
    band_name = cms.bands[0].name
    state = {
        "schema_version": 5,
        "preset_name": cfg.miras.preset,
        "chain_residual": False,
        "bands": {
            band_name: {
                # Legacy pre-v0.5 MLP weight blocks — the loader ignores these.
                "memory_state": {"net.0.weight": torch.zeros(2, 2)},
                "optimizer_state": {"name": "sgd_momentum"},
                "surprise_ema": 0.0,
                "axes": {},
                "entries": [],
            }
        },
        "interaction_count": 0,
        "logical_turn_count": 0,
        "surprise_history": {},
        "consolidation_events": [],
        "tier_hits": {},
        "tier_queries": 0,
        # No ``episodes`` key at all — simulates a v5 save.
    }
    save_dir = tmp_path
    torch.save(state, save_dir / "cms_state.pt")

    fresh = ContinuumMemorySystem(cfg)
    fresh.load(save_dir)
    assert fresh.episodes.episodes == {}
    assert fresh.episodes.current_id is None


def test_save_load_preserves_superseded_by_text(tmp_path: Path) -> None:
    """Pre-existing v5 bug: ``superseded_by_text`` was declared in the
    schema but not actually persisted by ``MIRASBand.get_state_dict``.
    C-1 touches the band's serialisation surface, so include the fix on
    the same pass.
    """
    cfg = MemoryConfig()
    cms_a = ContinuumMemorySystem(cfg)
    _append_entry(
        cms_a,
        text="old fact",
        source="test",
        superseded_at=1234.0,
        superseded_by_text="corrected by new fact",
    )
    cms_a.save(tmp_path)

    cms_b = ContinuumMemorySystem(cfg)
    cms_b.load(tmp_path)
    restored = cms_b.bands[0].entries
    assert len(restored) == 1
    assert restored[0].superseded_at == 1234.0
    assert restored[0].superseded_by_text == "corrected by new fact"
