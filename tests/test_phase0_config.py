"""Phase 0 config knobs — meta_filter, neural ramp, recency base."""
from pseudolife_memory.utils.config import AppConfig, MemoryConfig, load_config


def test_meta_filter_config_defaults():
    cfg = MemoryConfig()
    assert cfg.meta_filter.enabled is True


def test_neural_ramp_defaults():
    cfg = MemoryConfig()
    assert cfg.neural_blend_weight == 0.6
    assert cfg.neural_warmup_updates == 50


def test_recency_base_default():
    cfg = MemoryConfig()
    assert cfg.recency_base_half_life_s == 3600.0


def test_yaml_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "memory:\n"
        "  neural_blend_weight: 0.4\n"
        "  neural_warmup_updates: 10\n"
        "  recency_base_half_life_s: 86400\n"
        "  meta_filter:\n"
        "    enabled: false\n"
    )
    cfg = load_config(p)
    assert cfg.memory.neural_blend_weight == 0.4
    assert cfg.memory.neural_warmup_updates == 10
    assert cfg.memory.recency_base_half_life_s == 86400
    assert cfg.memory.meta_filter.enabled is False
