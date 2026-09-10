"""ONNX preflight checks the artifact paths used by model modules."""

import json
from pathlib import Path

import pytest

from pseudolife_memory.memory import embedding
from pseudolife_memory.memory.embedding import _resolve_onnx_source
from pseudolife_memory.onnx_artifacts import onnx_layout_available


def _layout(tmp_path, module_path="0_Transformer"):
    root = tmp_path / "model"
    (root / "onnx").mkdir(parents=True)
    (root / "onnx" / "model.onnx").write_bytes(b"fixture")
    (root / "modules.json").write_text(json.dumps([
        {"idx": 0, "name": "0", "path": module_path,
         "type": "sentence_transformers.models.Transformer"},
        {"idx": 1, "name": "1", "path": "1_Pooling",
         "type": "sentence_transformers.models.Pooling"},
    ]), encoding="utf-8")
    return root


def test_root_artifact_does_not_cover_missing_transformer_subdirectory(tmp_path):
    root = _layout(tmp_path)
    assert _resolve_onnx_source(str(root), "onnx/model.onnx") is None


def test_transformer_subdirectory_requires_its_own_artifact(tmp_path):
    root = _layout(tmp_path)
    target = root / "0_Transformer" / "onnx"
    target.mkdir(parents=True)
    (target / "model.onnx").write_bytes(b"fixture")
    assert _resolve_onnx_source(str(root), "onnx/model.onnx") == str(root)


def test_nested_only_transformer_layout_resolves(tmp_path):
    root = _layout(tmp_path)
    (root / "onnx" / "model.onnx").unlink()
    target = root / "0_Transformer" / "onnx"
    target.mkdir(parents=True)
    (target / "model.onnx").write_bytes(b"fixture")

    assert _resolve_onnx_source(str(root), "onnx/model.onnx") == str(root)


@pytest.mark.parametrize("kind", [
    "sentence_transformers.base.modules.transformer.Transformer",
    "sentence_transformers.sentence_transformer.modules.pooling.Pooling",
    "sentence_transformers.sentence_transformer.modules.normalize.Normalize",
    "sentence_transformers.base.modules.dense.Dense",
])
def test_current_sentence_transformer_module_references_are_supported(
    tmp_path, kind,
):
    root = tmp_path / "model"
    (root / "onnx").mkdir(parents=True)
    (root / "onnx" / "model.onnx").write_bytes(b"fixture")
    module_kind = (
        "sentence_transformers.base.modules.transformer.Transformer"
        if kind.endswith(".Transformer")
        else kind
    )
    modules = [{"idx": 0, "name": "0", "path": "", "type": module_kind}]
    if not kind.endswith(".Transformer"):
        modules.insert(0, {
            "idx": -1,
            "name": "transformer",
            "path": "",
            "type": "sentence_transformers.base.modules.transformer.Transformer",
        })
    (root / "modules.json").write_text(json.dumps(modules), encoding="utf-8")

    assert onnx_layout_available(root, "onnx/model.onnx")


def test_transformer_module_directory_link_outside_root_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    root = _layout(tmp_path)
    linked_path = root / "0_Transformer" / "onnx"
    linked_path.mkdir(parents=True)
    (linked_path / "model.onnx").write_bytes(b"fixture")
    outside = tmp_path / "outside"
    (outside / "onnx").mkdir(parents=True)
    (outside / "onnx" / "model.onnx").write_bytes(b"fixture")
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == root / "0_Transformer" / "onnx":
            return outside / "onnx"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert not onnx_layout_available(root, "onnx/model.onnx")


def test_local_snapshot_lookalike_leaf_link_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    repo = tmp_path / "models--example--model"
    blob = repo / "blobs" / "digest"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"fixture")
    snapshot = repo / "snapshots" / "revision"
    artifact = snapshot / "onnx" / "model.onnx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fixture")
    real_resolve = Path.resolve
    real_is_symlink = Path.is_symlink

    def resolve(path, *args, **kwargs):
        if path == artifact:
            return blob
        return real_resolve(path, *args, **kwargs)

    def is_symlink(path):
        return path == artifact or real_is_symlink(path)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)

    assert _resolve_onnx_source(str(snapshot), "onnx/model.onnx") is None


def test_cached_hf_leaf_link_into_same_repo_blobs_is_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    repo = tmp_path / "models--example--model"
    blob = repo / "blobs" / "digest"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"fixture")
    snapshot = repo / "snapshots" / "revision"
    artifact = snapshot / "onnx" / "model.onnx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fixture")
    real_resolve = Path.resolve
    real_is_symlink = Path.is_symlink

    def resolve(path, *args, **kwargs):
        if path == artifact:
            return blob
        return real_resolve(path, *args, **kwargs)

    def is_symlink(path):
        return path == artifact or real_is_symlink(path)

    def cached_file(repo_id, filename, local_files_only=False):
        assert (repo_id, filename, local_files_only) == (
            "example/model", "onnx/model.onnx", True,
        )
        return str(artifact)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(embedding, "_cached_hf_file", cached_file)

    assert _resolve_onnx_source("example/model", "onnx/model.onnx") == str(snapshot)


def test_local_leaf_link_outside_model_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    root = tmp_path / "model"
    artifact = root / "onnx" / "model.onnx"
    artifact.parent.mkdir(parents=True)
    outside = tmp_path / "outside.onnx"
    outside.write_bytes(b"fixture")
    artifact.write_bytes(b"fixture")
    real_resolve = Path.resolve
    real_is_symlink = Path.is_symlink

    def resolve(path, *args, **kwargs):
        if path == artifact:
            return outside
        return real_resolve(path, *args, **kwargs)

    def is_symlink(path):
        return path == artifact or real_is_symlink(path)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)

    assert _resolve_onnx_source(str(root), "onnx/model.onnx") is None


def test_hf_snapshot_leaf_link_into_another_repo_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    repo = tmp_path / "models--example--model"
    other_blob = tmp_path / "models--other--model" / "blobs" / "digest"
    other_blob.parent.mkdir(parents=True)
    other_blob.write_bytes(b"fixture")
    (repo / "blobs").mkdir(parents=True)
    snapshot = repo / "snapshots" / "revision"
    artifact = snapshot / "onnx" / "model.onnx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fixture")
    real_resolve = Path.resolve
    real_is_symlink = Path.is_symlink

    def resolve(path, *args, **kwargs):
        if path == artifact:
            return other_blob
        return real_resolve(path, *args, **kwargs)

    def is_symlink(path):
        return path == artifact or real_is_symlink(path)

    def cached_file(repo_id, filename, local_files_only=False):
        if filename == "onnx/model.onnx":
            return str(artifact)
        raise FileNotFoundError(filename)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(embedding, "_cached_hf_file", cached_file)

    assert _resolve_onnx_source("example/model", "onnx/model.onnx") is None


def test_cached_nested_layout_resolves_from_modules_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    repo = tmp_path / "models--example--model"
    snapshot = repo / "snapshots" / "revision"
    target = snapshot / "0_Transformer" / "onnx"
    target.mkdir(parents=True)
    (target / "model.onnx").write_bytes(b"fixture")
    modules_path = snapshot / "modules.json"
    modules_path.write_text(json.dumps([{
        "idx": 0,
        "name": "0",
        "path": "0_Transformer",
        "type": "sentence_transformers.models.Transformer",
    }]), encoding="utf-8")
    requests = []

    def cached_file(repo_id, filename, local_files_only=False):
        requests.append((repo_id, filename, local_files_only))
        if filename == "modules.json":
            return str(modules_path)
        raise FileNotFoundError(filename)

    monkeypatch.setattr(embedding, "_cached_hf_file", cached_file)

    assert _resolve_onnx_source("example/model", "onnx/model.onnx") == str(snapshot)
    assert requests == [
        ("example/model", "onnx/model.onnx", True),
        ("example/model", "modules.json", True),
    ]


def test_case_sensitive_onnx_suffix_is_required(tmp_path):
    root = _layout(tmp_path, "")
    (root / "onnx" / "alternate.ONNX").write_bytes(b"fixture")
    with pytest.raises(ValueError, match="relative .onnx"):
        _resolve_onnx_source(str(root), "onnx/alternate.ONNX")


@pytest.mark.parametrize("modules", [[], {}, [{"path": "", "type": "custom.Encoder"}]])
def test_unknown_module_layout_is_not_assumed_load_only(tmp_path, modules):
    root = _layout(tmp_path, "")
    (root / "modules.json").write_text(json.dumps(modules), encoding="utf-8")
    assert _resolve_onnx_source(str(root), "onnx/model.onnx") is None


def test_provisioner_checks_transformer_module_artifact_before_loading(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "provision_fixture", Path(__file__).parents[1] / "ops/provision_embedding_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._native_windows = lambda: False
    root = _layout(tmp_path)
    calls = []
    with pytest.raises(RuntimeError, match="ONNX.*layout"):
        module.provision_embedding_models(
            sentence_transformer=lambda *args, **kwargs: calls.append(kwargs),
            hf_download=lambda **kwargs: str(root / "onnx/model.onnx"),
        )
    assert all(call.get("backend") != "onnx" for call in calls)
