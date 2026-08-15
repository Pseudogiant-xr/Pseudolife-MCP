"""Flat-band migration (2026-08-15) — the default flip and the four
repaired n=1 breaks.

The 2026-08-15 verdict (spec:
docs/superpowers/specs/2026-08-15-flat-band-migration-design.md) flips
the default MIRAS preset from the 8-band continuum to one flat band. The
multi-band machinery stays in the tree (its invariant suites are its
spec); these tests pin the flat default's shape and the four behaviors
the E4 smoke proved a naive flip would silently break: destructive
eviction visibility, silent-empty band filters, name-keyed state-restore
loss, and stale band stamps after hydrate.
"""

from __future__ import annotations

import pytest
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import MemoryConfig, MIRASBandSpec

DIM = 8


def _flat_cfg(cap: int = 5) -> MemoryConfig:
    cfg = MemoryConfig(embedding_dim=DIM)
    cfg.miras.preset = "custom"
    cfg.miras.bands = [MIRASBandSpec(
        name="flat", max_entries=cap, update_interval=1_000_000_000,
        promotion_access_count=1_000_000_000, promotion_surprise=1.1,
        retention_policy="balanced")]
    return cfg


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return torch.randn(DIM, generator=g)


class TestFlatDefault:
    def test_default_preset_is_flat_one_band(self):
        cfg = MemoryConfig()
        assert cfg.miras.preset == "flat"
        assert len(cfg.miras.bands) == 1
        band = cfg.miras.bands[0]
        assert band.name == "flat"
        # Capacity semantics unchanged: the continuum's measured total.
        assert band.max_entries == 5250
        assert band.retention_policy == "balanced"
        # Promotion is structurally unreachable, but the fields stay
        # meaningful for continuum/custom presets.
        assert band.promotion_surprise > 1.0

    def test_continuum_preset_retained_for_rollback(self):
        from pseudolife_memory.memory.miras.presets import preset_bands
        bands = preset_bands("continuum")
        assert len(bands) == 8
        assert sum(b.max_entries for b in bands) == 5250

    def test_flat_preset_matches_the_measured_ablation_arm(self):
        # The ablation's flat arm (band_ablation.write_flat_config) is
        # the thing the verdict measured — the shipped preset must be
        # that arm, not a near miss.
        from pseudolife_memory.memory.miras.presets import preset_bands
        (band,) = preset_bands("flat")
        assert (band.max_entries, band.retention_policy) == (5250, "balanced")
        assert band.promotion_access_count >= 10**9
        assert band.update_interval >= 10**9


class _DeleteRecorder:
    """Just enough storage for the store + eviction write-through paths."""

    def __init__(self):
        self.deleted: list[int] = []
        self._next_id = 100

    def insert_entry(self, *args, **fields):
        self._next_id += 1
        return self._next_id

    def delete_entry_ids(self, ids):
        self.deleted.extend(ids)
        return len(ids)

    def __getattr__(self, name):
        # Any other write-through hook is a no-op for this test.
        return lambda *a, **k: None


class TestN1Eviction:
    def test_true_drop_deletes_counts_and_stays_at_capacity(self):
        cms = ContinuumMemorySystem(_flat_cfg(cap=3))
        cms.storage = _DeleteRecorder()
        for i in range(3):
            cms.store(f"turn {i}", _emb(i), source="user")
        cms.store("turn 3 overflow", _emb(3), source="user")
        assert len(cms.bands[0].entries) == 3
        assert len(cms.storage.deleted) == 1
        assert cms.stats().get("true_drops") == 1

    def test_multiband_cascade_does_not_count_a_true_drop(self):
        cfg = MemoryConfig(embedding_dim=DIM)
        cfg.miras.preset = "custom"
        cfg.miras.bands = [
            MIRASBandSpec(name="a", max_entries=2, update_interval=10**9,
                          promotion_access_count=10**9,
                          promotion_surprise=1.1,
                          retention_policy="balanced"),
            MIRASBandSpec(name="b", max_entries=5, update_interval=10**9,
                          promotion_access_count=10**9,
                          promotion_surprise=1.1,
                          retention_policy="balanced"),
        ]
        cms = ContinuumMemorySystem(cfg)
        for i in range(4):
            cms.store(f"turn {i}", _emb(i), source="user")
        # Overflow demoted a->b: nothing left the system.
        assert sum(len(b.entries) for b in cms.bands) == 4
        assert cms.stats().get("true_drops", 0) == 0


class TestBandFilterValidation:
    def test_unknown_band_name_raises_with_valid_names(
        self, pristine_service,
    ) -> None:
        pristine_service.store("a fact to have something resident")
        with pytest.raises(ValueError, match="flat"):
            pristine_service.search("anything", bands=["instant"])

    def test_valid_band_filter_still_works(self, pristine_service) -> None:
        pristine_service.store("the sky is cerulean today")
        out = pristine_service.search("sky colour", bands=["flat"])
        assert out["count"] >= 1

    def test_trace_validates_too(self, pristine_service) -> None:
        pristine_service.store("another resident fact")
        with pytest.raises(ValueError, match="flat"):
            pristine_service.trace("anything", bands=["working", "nope"])


class TestStateRestoreFallback:
    def test_v2_restore_routes_unknown_bands_into_first_band(self, tmp_path):
        two = MemoryConfig(embedding_dim=DIM)
        two.miras.preset = "custom"
        two.miras.bands = [
            MIRASBandSpec(name="working", max_entries=5,
                          update_interval=10**9,
                          promotion_access_count=10**9,
                          promotion_surprise=1.1,
                          retention_policy="balanced"),
            MIRASBandSpec(name="slow", max_entries=5, update_interval=10**9,
                          promotion_access_count=10**9,
                          promotion_surprise=1.1,
                          retention_policy="surprise_heavy"),
        ]
        src = ContinuumMemorySystem(two)
        src.store("kept one", _emb(1), source="user")
        src.store("kept two", _emb(2), source="user")
        src.bands[1].store("kept deep", _emb(3), source="user")
        src.save(tmp_path)

        dst = ContinuumMemorySystem(_flat_cfg(cap=50))
        dst.load(tmp_path)
        texts = {e.text for e in dst.bands[0].entries}
        assert texts == {"kept one", "kept two", "kept deep"}


class _StampStorage:
    """Storage double for hydrate: rows with stale band stamps."""

    def __init__(self, bands):
        self._bands = bands
        self.updates: list[tuple[int, dict]] = []

    def load_entries(self):
        rows = []
        for i, band in enumerate(self._bands):
            rows.append({
                "id": i + 1, "text": f"row {i}",
                "embedding": _emb(i).tolist(), "surprise": 0.4,
                "ts": 1000.0 + i, "access_count": 0, "source": "user",
                "band": band, "superseded_at": None,
                "superseded_by_text": None, "last_logical_turn": None,
                "slots": [], "episode_id": None, "episode_title": None,
                "tags": [], "reinforcements": 0,
            })
        return rows

    def load_episodes(self):
        return []

    def update_entry(self, entry_id, **fields):
        self.updates.append((entry_id, fields))


class TestHydrateStampReconciliation:
    def test_stale_stamps_rewritten_in_memory_and_storage(self):
        from pseudolife_memory.storage.sync import hydrate_cms
        cms = ContinuumMemorySystem(_flat_cfg(cap=50))
        storage = _StampStorage(["working", "slow", "flat"])
        n = hydrate_cms(cms, storage)
        assert n == 3
        assert all(e.bank == "flat" for e in cms.bands[0].entries)
        # Only the two stale rows get written through.
        assert sorted(u[0] for u in storage.updates) == [1, 2]
        assert all(u[1] == {"band": "flat"} for u in storage.updates)

    def test_second_hydrate_touches_nothing(self):
        from pseudolife_memory.storage.sync import hydrate_cms
        cms = ContinuumMemorySystem(_flat_cfg(cap=50))
        storage = _StampStorage(["flat", "flat"])
        hydrate_cms(cms, storage)
        assert storage.updates == []
