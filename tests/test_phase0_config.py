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


def test_yaml_memory_block_omitted_keys_keep_dataclass_defaults(tmp_path):
    """The loader hand-rolls MemoryConfig with literal fallbacks; each one
    must mirror the dataclass default, or a config.yaml that has a memory:
    block but omits a key silently runs a different value than a config
    with no file at all. Found live 2026-08-01: the surprise_threshold
    fallback said 0.3 while the dataclass (and the docs) say 0.0."""
    p = tmp_path / "config.yaml"
    p.write_text("memory:\n  top_k: 8\n")
    loaded = load_config(p).memory
    defaults = MemoryConfig()
    for field in ("embedding_dim", "surprise_threshold", "top_k",
                  "ref_top_k", "save_dir", "hide_superseded",
                  "search_confidence_floor", "recency_base_half_life_s",
                  "slot_index_shadow_rate"):
        assert getattr(loaded, field) == getattr(defaults, field), field


def test_yaml_memory_scalar_key_present_is_read(tmp_path):
    """Companion to the omitted-keys pin above: a key PRESENT in the
    memory: block must actually reach the dataclass. The hand-rolled
    kwarg list silently drops any field it doesn't name — found in
    review 2026-08-13: slot_index_shadow_rate was documented as a yaml
    knob while the loader never read it."""
    p = tmp_path / "config.yaml"
    p.write_text("memory:\n  slot_index_shadow_rate: 0.0\n  top_k: 11\n")
    loaded = load_config(p).memory
    assert loaded.top_k == 11
    assert loaded.slot_index_shadow_rate == 0.0
