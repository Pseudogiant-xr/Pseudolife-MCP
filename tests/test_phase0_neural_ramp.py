"""Neural blend warmup: w = blend * min(1, update_count / warmup)."""
import torch

from pseudolife_memory.memory.cms import ContinuumMemorySystem
from pseudolife_memory.utils.config import MemoryConfig


def _emb(seed: int) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    v = torch.randn(384, generator=g)
    return v / v.norm()


def _band(cfg: MemoryConfig):
    return ContinuumMemorySystem(cfg).bands[0]


def test_update_count_increments_and_persists():
    cms = ContinuumMemorySystem(MemoryConfig())
    band = cms.bands[0]
    assert band.update_count == 0
    band.store("a fact", _emb(1), source="t")
    band.store("another fact", _emb(2), source="t")
    assert band.update_count == 2
    state = band.get_state_dict()
    band2 = ContinuumMemorySystem(MemoryConfig()).bands[0]
    band2.load_state_dict(state)
    assert band2.update_count == 2
    # Pre-v8 saves have no update_count — defaults to 0.
    del state["update_count"]
    band3 = ContinuumMemorySystem(MemoryConfig()).bands[0]
    band3.load_state_dict(state)
    assert band3.update_count == 0


def test_cold_band_scores_pure_exact_cosine():
    cfg = MemoryConfig()
    cfg.neural_warmup_updates = 1000  # keep ramp ~0 after 2 updates
    band = _band(cfg)
    e1, e2 = _emb(10), _emb(11)
    band.store("target fact", e1, source="t")
    band.store("distractor fact", e2, source="t")
    result = band.retrieve(_emb(10), top_k=2)
    # With w ~ 0.001, scores ~ exact cosine: query == e1 -> ~1.0 for target.
    by_text = dict(zip([e.text for e in result.entries], result.scores))
    assert by_text["target fact"] > 0.99
    assert by_text["distractor fact"] < 0.5


def test_warmup_zero_restores_fixed_blend():
    cfg = MemoryConfig()
    cfg.neural_warmup_updates = 0
    band = _band(cfg)
    band.store("some fact", _emb(20), source="t")
    # w must be exactly neural_blend_weight (v0.1 behavior).
    assert band._effective_neural_weight() == cfg.neural_blend_weight


def test_ramp_saturates_at_warmup():
    cfg = MemoryConfig()
    cfg.neural_warmup_updates = 2
    band = _band(cfg)
    band.store("f1", _emb(30), source="t")
    assert abs(band._effective_neural_weight() - 0.3) < 1e-9   # 0.6 * 1/2
    band.store("f2", _emb(31), source="t")
    assert abs(band._effective_neural_weight() - 0.6) < 1e-9   # saturated
    band.store("f3", _emb(32), source="t")
    assert abs(band._effective_neural_weight() - 0.6) < 1e-9   # capped
