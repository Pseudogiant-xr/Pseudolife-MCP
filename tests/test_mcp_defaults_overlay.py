"""_apply_mcp_defaults must overlay, not clobber, user-set config.yaml keys.

2026-07-02 review fix: the MCP-tuned defaults (surprise_threshold=0.0,
meta_filter off, 24h recency half-life, retention_boost=1.0, batch_size=16)
were applied unconditionally AFTER load_config, so the corresponding YAML
knobs were dead — a user raising surprise_threshold in config.yaml silently
got 0.0 back. Defaults may only fill keys the user did not set.
"""

from __future__ import annotations

from pseudolife_memory.service import MemoryService


def test_user_yaml_survives_mcp_defaults(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "memory:\n"
        "  surprise_threshold: 0.3\n"
        "  traces:\n"
        "    retention_boost: 0.0\n",
        encoding="utf-8",
    )
    svc = MemoryService(data_dir=tmp_path)

    # Deliberate user choices survive (0.0 == the library default for
    # retention_boost, so it also proves "explicitly set" beats "absent").
    assert svc.config.memory.surprise_threshold == 0.3
    assert svc.config.memory.traces.retention_boost == 0.0

    # Keys the user did NOT set still get the MCP-tuned defaults.
    assert svc.config.memory.meta_filter.enabled is False
    assert svc.config.memory.recency_base_half_life_s == 86400.0
    assert svc.config.embedding.batch_size == 16


def test_mcp_defaults_apply_when_no_config_file(tmp_path):
    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.memory.surprise_threshold == 0.0
    assert svc.config.memory.traces.retention_boost == 1.0
    assert svc.config.memory.meta_filter.enabled is False
