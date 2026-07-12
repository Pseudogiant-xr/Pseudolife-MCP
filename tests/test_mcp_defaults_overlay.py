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


def test_onnx_backend_defaults_on_when_optimum_installed(tmp_path, monkeypatch):
    """The daemon image ships optimum[onnxruntime]; with it installed the
    MCP default flips the embedder to the ~3x-faster ONNX backend
    (bit-identical embeddings, fail-soft back to torch)."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "onnx"


def test_onnx_backend_stays_torch_without_optimum(tmp_path, monkeypatch):
    """A plain pip install (no [onnx] extra) must stay on torch — never
    default into a backend that can only warn-and-fall-back."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: False)
    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "torch"


def test_user_backend_choice_survives_mcp_defaults(tmp_path, monkeypatch):
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    (tmp_path / "config.yaml").write_text(
        "embedding:\n  backend: torch\n", encoding="utf-8",
    )
    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "torch"
