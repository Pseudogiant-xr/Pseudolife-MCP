"""Phase 0 config knobs — meta_filter, recency base, continuum preset."""
from pseudolife_memory.utils.config import AppConfig, MemoryConfig, load_config


def test_meta_filter_config_defaults():
    cfg = MemoryConfig()
    assert cfg.meta_filter.enabled is True


def test_recency_base_default():
    cfg = MemoryConfig()
    assert cfg.recency_base_half_life_s == 3600.0


def test_continuum_preset_yields_eight_cosine_bands():
    """v0.5: the default preset is the 8-tier continuum; band specs carry only
    capacity / cadence / promotion / eviction (no neural axes)."""
    cfg = MemoryConfig()
    assert cfg.miras.preset == "continuum"
    assert len(cfg.miras.bands) == 8
    spec = cfg.miras.bands[0]
    assert spec.retention_policy in {"balanced", "recency_heavy", "surprise_heavy"}
    assert not hasattr(spec, "objective")
    assert not hasattr(spec, "memory_module")


def test_yaml_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "memory:\n"
        "  recency_base_half_life_s: 86400\n"
        "  meta_filter:\n"
        "    enabled: false\n"
    )
    cfg = load_config(p)
    assert cfg.memory.recency_base_half_life_s == 86400
    assert cfg.memory.meta_filter.enabled is False
