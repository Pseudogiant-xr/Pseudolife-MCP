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


def _record_onnx_probe(monkeypatch, result):
    """Replace the ONNX artifact probe; return the list of its calls."""
    from pseudolife_memory.memory import embedding

    calls: list[tuple[str, str]] = []

    def probe(model_name, file_name):
        calls.append((model_name, file_name))
        return result

    monkeypatch.setattr(embedding, "_resolve_onnx_source", probe)
    return calls


def _write_model_config(tmp_path, model_dir, extra=""):
    (tmp_path / "config.yaml").write_text(
        "embedding:\n"
        f"  model_name: '{model_dir.as_posix()}'\n"
        f"{extra}",
        encoding="utf-8",
    )


def test_onnx_backend_auto_selected_when_artifact_resolves(tmp_path, monkeypatch):
    """The daemon image ships optimum[onnxruntime] and bakes MiniLM's
    ``onnx/model.onnx``; a model whose configured artifact resolves locally
    gets the ~3x-faster ONNX backend. Unmocked probe over a real model dir."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    model_dir = tmp_path / "model"
    (model_dir / "onnx").mkdir(parents=True)
    (model_dir / "onnx" / "model.onnx").write_bytes(b"fixture")
    _write_model_config(tmp_path, model_dir)

    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "onnx"


def test_onnx_backend_not_auto_selected_when_artifact_absent(tmp_path, monkeypatch):
    """optimum installed but the configured model has no ONNX artifact (the
    Qwen3-Embedding default): choose torch up front instead of selecting a
    backend the loader can only warn about and fall back from on every boot.
    Unmocked probe over a real model dir without the artifact."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    _write_model_config(tmp_path, model_dir)

    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "torch"


def test_default_model_without_onnx_artifact_stays_torch(tmp_path, monkeypatch):
    """The shipped default (no config.yaml) probes the default model and
    default artifact name, and stays on torch when that artifact is absent."""
    import pseudolife_memory.service as service_mod
    from pseudolife_memory.utils.config import EmbeddingConfig

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    calls = _record_onnx_probe(monkeypatch, None)

    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "torch"
    default = EmbeddingConfig()
    assert calls == [(default.model_name, default.onnx_file_name)]


def test_onnx_probe_uses_configured_model_and_file(tmp_path, monkeypatch):
    """The probe sees the user's model and artifact name, not the defaults:
    a MiniLM config whose artifact resolves auto-selects ONNX."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    calls = _record_onnx_probe(monkeypatch, "/cache/snapshot")
    (tmp_path / "config.yaml").write_text(
        "embedding:\n"
        "  model_name: all-MiniLM-L6-v2\n"
        "  onnx_file_name: onnx/model_O2.onnx\n",
        encoding="utf-8",
    )

    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "onnx"
    assert calls == [("all-MiniLM-L6-v2", "onnx/model_O2.onnx")]


def test_onnx_probe_normalizes_file_name_like_the_loader(tmp_path, monkeypatch):
    """The loader accepts a backslash-separated ``onnx_file_name`` and
    normalizes it before resolving; the auto-select probe must resolve the
    same path, or a loadable artifact reads as absent and ONNX is lost."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    model_dir = tmp_path / "model"
    (model_dir / "custom").mkdir(parents=True)
    (model_dir / "custom" / "optimized.onnx").write_bytes(b"fixture")
    _write_model_config(
        tmp_path, model_dir, extra="  onnx_file_name: 'custom\\optimized.onnx'\n",
    )

    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "onnx"


def test_invalid_onnx_file_name_is_left_for_the_loader_to_report(
    tmp_path, monkeypatch,
):
    """A user-set ``onnx_file_name`` the probe rejects is a config error, not
    an absent artifact: construction must not raise, and ONNX stays selected
    so the loader's warning names the bad path instead of it vanishing into
    a silent torch choice."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    _write_model_config(
        tmp_path, model_dir, extra="  onnx_file_name: ../outside.onnx\n",
    )

    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "onnx"


def test_onnx_backend_stays_torch_without_optimum(tmp_path, monkeypatch):
    """A plain pip install (no [onnx] extra) must stay on torch — never
    default into a backend that can only warn-and-fall-back — and never
    pays for the artifact probe."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: False)
    calls = _record_onnx_probe(monkeypatch, "/cache/snapshot")
    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "torch"
    assert calls == []


def test_user_backend_choice_survives_mcp_defaults(tmp_path, monkeypatch):
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    calls = _record_onnx_probe(monkeypatch, "/cache/snapshot")
    (tmp_path / "config.yaml").write_text(
        "embedding:\n  backend: torch\n", encoding="utf-8",
    )
    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "torch"
    assert calls == []


def test_explicit_onnx_backend_kept_when_artifact_absent(tmp_path, monkeypatch):
    """An explicit ``backend: onnx`` is the user's call: it is not second-
    guessed by the probe, and the loader keeps its warn-and-fall-back."""
    import pseudolife_memory.service as service_mod

    monkeypatch.setattr(service_mod, "_onnx_embedding_available", lambda: True)
    calls = _record_onnx_probe(monkeypatch, None)
    (tmp_path / "config.yaml").write_text(
        "embedding:\n  backend: onnx\n", encoding="utf-8",
    )
    svc = MemoryService(data_dir=tmp_path)
    assert svc.config.embedding.backend == "onnx"
    assert calls == []
