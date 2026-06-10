"""Meta-filter: config gate + pruned domain-colliding patterns."""
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.memory.meta_filter import is_meta_statement
from pseudolife_memory.service import MemoryService
from pseudolife_memory.utils.config import AppConfig, MemoryConfig


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(384, generator=g)
    return v / v.norm()


def test_pruned_patterns_no_longer_match():
    # These are legitimate dev facts about a memory system project.
    assert not is_meta_statement("Decided to keep the surprise threshold at 0.2")
    assert not is_meta_statement(
        "The memory bank module stores entries in SQLite now"
    )


def test_genuine_meta_still_matches():
    assert is_meta_statement("I don't have any cat-related material saved")
    assert is_meta_statement("No relevant memories")


def test_cms_store_respects_disabled_filter():
    cfg = MemoryConfig()
    cfg.meta_filter.enabled = False
    cms = ContinuumMemorySystem(cfg)
    stored, _surprise = cms.store(
        "I don't have any cat-related material saved", _emb(1), source="claude",
    )
    # With the filter off, the only gate left is surprise — a fresh bank
    # has surprise 1.0, so this stores.
    assert stored is True


def test_cms_store_respects_enabled_filter():
    cfg = MemoryConfig()
    cfg.meta_filter.enabled = True
    cms = ContinuumMemorySystem(cfg)
    stored, surprise = cms.store(
        "I don't have any cat-related material saved", _emb(2), source="claude",
    )
    assert stored is False and surprise == 0.0


def test_mcp_defaults_disable_filter():
    cfg = AppConfig()
    MemoryService._apply_mcp_defaults(cfg)
    assert cfg.memory.meta_filter.enabled is False
