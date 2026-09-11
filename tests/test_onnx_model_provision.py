"""Daemon-image provisioning keeps the shipped ONNX backend load-only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_PROVISIONER = _REPO / "ops" / "provision_embedding_models.py"
_DOCKERFILE = _REPO / "ops" / "Dockerfile.daemon"


def _load_provisioner(*, native_windows: bool = False):
    spec = importlib.util.spec_from_file_location(
        "provision_embedding_models", _PROVISIONER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._native_windows = lambda: native_windows
    return module


def test_native_windows_rejects_before_models_or_hub_are_called() -> None:
    provisioner = _load_provisioner(native_windows=True)
    model_calls = []
    hub_calls = []

    with pytest.raises(RuntimeError, match="not supported on native Windows"):
        provisioner.provision_embedding_models(
            sentence_transformer=lambda *args, **kwargs: model_calls.append(
                (args, kwargs),
            ),
            hf_download=lambda **kwargs: hub_calls.append(kwargs),
        )

    assert model_calls == []
    assert hub_calls == []


def test_provisioner_downloads_one_explicit_onnx_and_loads_its_snapshot(
    tmp_path: Path,
) -> None:
    provisioner = _load_provisioner()
    snapshot = tmp_path / "snapshots" / "fixed-revision"
    (snapshot / "onnx").mkdir(parents=True)
    artifact = snapshot / "onnx" / "model.onnx"
    artifact.write_bytes(b"onnx")
    calls: list[tuple[str, dict]] = []

    def fake_sentence_transformer(model_name: str, **kwargs):
        calls.append((model_name, kwargs))
        return object()

    def fake_hf_download(*, repo_id, filename):
        assert repo_id == "sentence-transformers/all-MiniLM-L6-v2"
        assert filename == "onnx/model.onnx"
        return str(artifact)

    provisioner.provision_embedding_models(
        sentence_transformer=fake_sentence_transformer,
        hf_download=fake_hf_download,
    )

    assert calls[:2] == [
        ("Qwen/Qwen3-Embedding-0.6B", {}),
        ("all-MiniLM-L6-v2", {}),
    ]
    assert calls[2] == (
        str(snapshot),
        {
            "backend": "onnx",
            "model_kwargs": {
                "file_name": "onnx/model.onnx",
                "export": False,
            },
        },
    )


def test_provisioner_does_not_hide_local_snapshot_metadata_failure(
    tmp_path: Path,
) -> None:
    provisioner = _load_provisioner()
    snapshot = tmp_path / "snapshots" / "drifted-revision"
    (snapshot / "onnx").mkdir(parents=True)
    artifact = snapshot / "onnx" / "model.onnx"
    artifact.write_bytes(b"onnx")

    def fake_sentence_transformer(model_name: str, **kwargs):
        if kwargs.get("backend") == "onnx":
            raise OSError("snapshot metadata is incomplete")
        return object()

    with pytest.raises(OSError, match="metadata is incomplete"):
        provisioner.provision_embedding_models(
            sentence_transformer=fake_sentence_transformer,
            hf_download=lambda **kwargs: str(artifact),
        )


def test_provisioner_missing_artifact_fails_before_onnx_constructor(
    tmp_path: Path,
) -> None:
    provisioner = _load_provisioner()
    calls: list[dict] = []

    def fake_sentence_transformer(model_name: str, **kwargs):
        calls.append(kwargs)
        return object()

    missing = tmp_path / "snapshot" / "onnx" / "model.onnx"
    with pytest.raises(RuntimeError, match="ONNX artifact is missing"):
        provisioner.provision_embedding_models(
            sentence_transformer=fake_sentence_transformer,
            hf_download=lambda **kwargs: str(missing),
        )
    assert calls == [{}, {}]


def test_dockerfile_executes_the_checked_provisioner() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    copy = "COPY ops/provision_embedding_models.py /app/"
    run = "python /app/provision_embedding_models.py"
    source_copy = "COPY pseudolife_memory /app/pseudolife_memory"
    assert copy in dockerfile
    assert run in dockerfile
    assert (
        dockerfile.index(copy)
        < dockerfile.index(run)
        < dockerfile.index(source_copy)
    )
    assert "SentenceTransformer('all-MiniLM-L6-v2', backend='onnx'" not in dockerfile
