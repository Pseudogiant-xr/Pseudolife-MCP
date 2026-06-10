"""Recency base half-life is config-driven and ramps geometrically."""
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.service import MemoryService
from pseudolife_memory.utils.config import AppConfig, MemoryConfig


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(384, generator=g)
    return v / v.norm()


def test_half_life_uses_config_base():
    cfg = MemoryConfig()
    cfg.recency_base_half_life_s = 7200.0
    cms = ContinuumMemorySystem(cfg)
    cms.store("recency probe fact", _emb(3), source="t")
    _result, trace = cms.retrieve_with_trace(_emb(3), top_k=2)
    tiers = [t for t in trace["tiers"] if not t.get("filtered_out")]
    assert tiers[0]["half_life_s"] == 7200.0
    assert tiers[1]["half_life_s"] == 14400.0  # doubles per depth


def test_mcp_default_is_one_day():
    cfg = AppConfig()
    MemoryService._apply_mcp_defaults(cfg)
    assert cfg.memory.recency_base_half_life_s == 86400.0
