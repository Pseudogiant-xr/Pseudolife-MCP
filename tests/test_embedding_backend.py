"""ONNX embedding backend (2026-07-12 perf work).

The embedder is the daemon's dominant per-request cost (~5ms/encode on
CPU torch). sentence-transformers' native ``backend="onnx"`` runs the
same MiniLM through onnxruntime at ~3x the speed with parity-checked
cosine geometry (benchmarked 2026-07-12: min cosine vs torch = 1.00000
over 20 texts), so the switch carries no measured retrieval-quality risk.

Contract pinned here:

* ``backend`` defaults to ``"torch"`` and the torch path must NOT pass
  a ``backend=`` kwarg to SentenceTransformer — the pyproject floor
  (sentence-transformers>=2.2) predates the kwarg;
* ``backend="onnx"`` preflights the configured local/cached artifact, then
  passes a local model root plus ``backend`` and
  ``model_kwargs={"file_name", "export": False}`` (default
  ``onnx/model.onnx``);
* an ONNX load failure (optimum missing, file not cached offline) falls
  back to torch with a warning — same fail-soft philosophy as the
  reranker: memory operations never break because of an optional
  accelerator;
* an unknown backend name is a config error and raises.

Unit tests stub SentenceTransformer (no model download); one
integration test loads the real model both ways and asserts parity.
"""
from __future__ import annotations

import json
import numpy as np
import pytest

from pseudolife_memory.utils.config import EmbeddingConfig


class _StubST:
    """Records constructor kwargs; returns deterministic embeddings."""

    def __init__(self, model_name: str, device: str | None = None, **kwargs) -> None:
        self.model_name = model_name
        self.device = device
        self.kwargs = kwargs

    def get_sentence_embedding_dimension(self) -> int:
        return 8

    def encode(self, texts, **kwargs):
        out = []
        for t in texts:
            rng = np.random.default_rng(sum(ord(c) for c in t))
            v = rng.standard_normal(8)
            out.append(v / np.linalg.norm(v))
        return np.array(out, dtype=np.float32)


@pytest.fixture(autouse=True)
def _simulate_supported_onnx_platform(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the stubbed backend tests on the Linux/Docker path.

    ``real_model`` tests load actual models and must see the host platform
    they run on, so the simulation stops at the stub tests.
    """
    if request.node.get_closest_marker("real_model") is not None:
        return
    from pseudolife_memory.memory import embedding

    monkeypatch.setattr(embedding, "_native_windows", lambda: False, raising=False)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[_StubST]:
    """Patch the embedding module's SentenceTransformer; collect instances."""
    from pseudolife_memory.memory import embedding

    instances: list[_StubST] = []

    def _factory(model_name, device=None, **kwargs):
        inst = _StubST(model_name, device=device, **kwargs)
        instances.append(inst)
        return inst

    monkeypatch.setattr(embedding, "SentenceTransformer", _factory)
    return instances


def _pipeline(config: EmbeddingConfig):
    from pseudolife_memory.memory.embedding import EmbeddingPipeline

    return EmbeddingPipeline(config)


def _modules_json(model, module_path: str) -> None:
    model.mkdir(parents=True, exist_ok=True)
    (model / "modules.json").write_text(json.dumps([
        {"idx": 0, "name": "0", "path": module_path,
         "type": "sentence_transformers.models.Transformer"},
        {"idx": 1, "name": "1", "path": "1_Pooling",
         "type": "sentence_transformers.models.Pooling"},
    ]), encoding="utf-8")


def test_default_backend_is_torch_and_omits_backend_kwarg(captured) -> None:
    pipe = _pipeline(EmbeddingConfig(device="cpu"))
    assert pipe.backend == "torch"
    assert len(captured) == 1
    assert "backend" not in captured[0].kwargs, (
        "torch path must not pass backend= — sentence-transformers 2.x "
        "(the pyproject floor) does not accept the kwarg")


def test_onnx_backend_loads_verified_local_artifact(
    captured, tmp_path,
) -> None:
    model = tmp_path / "model"
    (model / "onnx").mkdir(parents=True)
    (model / "onnx" / "model.onnx").write_bytes(b"onnx")

    pipe = _pipeline(EmbeddingConfig(
        device="cpu", backend="onnx", model_name=str(model),
    ))
    assert pipe.backend == "onnx"
    assert captured[0].model_name == str(model)
    assert captured[0].kwargs["backend"] == "onnx"
    assert captured[0].kwargs["model_kwargs"] == {
        "file_name": "onnx/model.onnx",
        "export": False,
    }


def test_native_windows_nested_layout_falls_back_before_onnx_constructor(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    """Only a NESTED module subfolder trips the Windows discovery defect.

    The pinned Optimum stack matches a POSIX subfolder pattern against
    OS-native path strings, so ``0_Transformer/onnx`` never matches on
    native Windows and export is re-enabled despite ``export=False``.
    """
    from pseudolife_memory.memory import embedding

    model = tmp_path / "model"
    (model / "0_Transformer" / "onnx").mkdir(parents=True)
    (model / "0_Transformer" / "onnx" / "model.onnx").write_bytes(b"onnx")
    _modules_json(model, "0_Transformer")
    calls: list[dict] = []

    def factory(model_name, device=None, **kwargs):
        calls.append(kwargs)
        if kwargs.get("backend") == "onnx":
            raise AssertionError("native Windows reached ONNX construction")
        return _StubST(model_name, device=device, **kwargs)

    monkeypatch.setattr(embedding, "_native_windows", lambda: True, raising=False)
    monkeypatch.setattr(embedding, "SentenceTransformer", factory)

    with caplog.at_level("WARNING"):
        pipe = _pipeline(EmbeddingConfig(
            device="cpu", backend="onnx", model_name=str(model),
        ))

    assert pipe.backend == "torch"
    assert calls == [{}]
    assert "native windows" in caplog.text.lower()
    assert "nested" in caplog.text.lower()
    assert "before onnx construction" in caplog.text.lower()


def test_native_windows_flat_layout_still_loads_onnx(
    captured, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """The flat ``onnx`` subfolder matches on both platforms.

    Disabling the backend for every Windows process was broader than the
    defect: a root Transformer module keeps the load-only ONNX path.
    """
    from pseudolife_memory.memory import embedding

    model = tmp_path / "model"
    (model / "onnx").mkdir(parents=True)
    (model / "onnx" / "model.onnx").write_bytes(b"onnx")
    _modules_json(model, "")

    monkeypatch.setattr(embedding, "_native_windows", lambda: True, raising=False)

    pipe = _pipeline(EmbeddingConfig(
        device="cpu", backend="onnx", model_name=str(model),
    ))
    assert pipe.backend == "onnx"
    assert captured[0].model_name == str(model)
    assert captured[0].kwargs["backend"] == "onnx"


def test_onnx_backend_honors_custom_file_name(captured, tmp_path) -> None:
    model = tmp_path / "model"
    (model / "custom").mkdir(parents=True)
    (model / "custom" / "optimized.onnx").write_bytes(b"onnx")
    cfg = EmbeddingConfig(
        device="cpu", backend="onnx", model_name=str(model),
        onnx_file_name="custom\\optimized.onnx",
    )
    pipe = _pipeline(cfg)
    assert captured[0].kwargs["model_kwargs"] == {
        "file_name": "custom/optimized.onnx",
        "export": False,
    }


def test_missing_local_onnx_with_named_template_falls_back_before_constructor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A malicious named template is inert when its ONNX file is absent.

    Optimum 0.1.0 recomputes ``_export = len(onnx_files) == 0`` in
    ``ORTModel.from_pretrained`` even when its caller supplied
    ``export=False``.  The preflight therefore has to stop before the ONNX
    SentenceTransformer constructor; merely asserting its kwargs would leave
    the vulnerable tokenizer/processor save path reachable.
    """
    from pseudolife_memory.memory import embedding

    model = tmp_path / "malicious-model"
    model.mkdir()
    (model / "tokenizer_config.json").write_text(json.dumps({
        "chat_template": {"../../outside": "attacker-controlled template"},
    }), encoding="utf-8")
    calls: list[dict] = []

    def _factory(model_name, device=None, **kwargs):
        calls.append(kwargs)
        if kwargs.get("backend") == "onnx":
            raise AssertionError("ONNX construction could enter auto-export")
        return _StubST(model_name, device=device, **kwargs)

    monkeypatch.setattr(embedding, "SentenceTransformer", _factory)

    pipe = _pipeline(EmbeddingConfig(
        device="cpu", backend="onnx", model_name=str(model),
    ))
    assert pipe.backend == "torch"
    assert calls == [{}]


@pytest.mark.parametrize("offline", [False, True], ids=["online", "offline"])
def test_missing_cached_onnx_falls_back_before_constructor(
    monkeypatch: pytest.MonkeyPatch,
    offline: bool,
) -> None:
    from pseudolife_memory.memory import embedding

    if offline:
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    else:
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    cache_checks: list[tuple[str, str, bool]] = []

    def _cache_miss(repo_id, filename, local_files_only=False):
        cache_checks.append((repo_id, filename, local_files_only))
        raise FileNotFoundError("not cached")

    calls: list[dict] = []

    def _factory(model_name, device=None, **kwargs):
        calls.append(kwargs)
        if kwargs.get("backend") == "onnx":
            raise AssertionError("cache miss reached ONNX construction")
        return _StubST(model_name, device=device, **kwargs)

    monkeypatch.setattr(embedding, "_cached_hf_file", _cache_miss)
    monkeypatch.setattr(embedding, "SentenceTransformer", _factory)

    pipe = _pipeline(EmbeddingConfig(
        device="cpu", backend="onnx", model_name="all-MiniLM-L6-v2",
    ))
    assert pipe.backend == "torch"
    assert calls == [{}]
    assert cache_checks == [
        ("sentence-transformers/all-MiniLM-L6-v2", "onnx/model.onnx", True),
        ("sentence-transformers/all-MiniLM-L6-v2", "modules.json", True),
        ("sentence-transformers/all-MiniLM-L6-v2", "config.json", True),
        ("all-MiniLM-L6-v2", "onnx/model.onnx", True),
        ("all-MiniLM-L6-v2", "modules.json", True),
        ("all-MiniLM-L6-v2", "config.json", True),
    ]


def test_onnx_file_name_cannot_escape_local_model(
    captured, tmp_path,
) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (tmp_path / "outside.onnx").write_bytes(b"onnx")

    pipe = _pipeline(EmbeddingConfig(
        device="cpu",
        backend="onnx",
        model_name=str(model),
        onnx_file_name="../outside.onnx",
    ))
    assert pipe.backend == "torch"
    assert len(captured) == 1
    assert "backend" not in captured[0].kwargs


def test_onnx_load_failure_falls_back_to_torch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    """optimum missing / weights not cached must not break the embedder."""
    from pseudolife_memory.memory import embedding

    instances: list[_StubST] = []

    def _factory(model_name, device=None, **kwargs):
        if kwargs.get("backend") == "onnx":
            raise RuntimeError("optimum is not installed")
        inst = _StubST(model_name, device=device, **kwargs)
        instances.append(inst)
        return inst

    monkeypatch.setattr(embedding, "SentenceTransformer", _factory)

    model = tmp_path / "model"
    (model / "onnx").mkdir(parents=True)
    (model / "onnx" / "model.onnx").write_bytes(b"onnx")

    with caplog.at_level("WARNING"):
        pipe = _pipeline(EmbeddingConfig(
            device="cpu", backend="onnx", model_name=str(model),
        ))
    assert pipe.backend == "torch"
    assert len(instances) == 1  # the fallback torch construction
    assert any("onnx" in r.message.lower() for r in caplog.records), (
        "the silent-fallback must at least log a warning")
    assert "identical" not in caplog.text.lower()
    # And the pipeline actually works post-fallback.
    assert pipe.encode_single("still alive").shape == (8,)


def test_unknown_backend_raises(captured) -> None:
    with pytest.raises(ValueError, match="backend"):
        _pipeline(EmbeddingConfig(device="cpu", backend="banana"))


def test_onnx_offline_resolves_local_snapshot_path(
    captured, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Deployment-critical: the ONNX loader (optimum) lists the hub repo
    tree even when every file is cached, which raises under
    HF_HUB_OFFLINE=1 — the Docker daemon's runtime contract. In offline
    mode the pipeline must resolve the repo to its local snapshot
    directory and pass THAT path, sidestepping the hub entirely.

    Pinned to an explicit BARE model name (not the config default, which
    is namespaced ``Qwen/...`` since schema v25 and so never enters the
    bare-name branch below) — this is a regression guard on the
    ``sentence-transformers/{name}`` expansion itself, independent of
    which model ships as the default."""
    from pseudolife_memory.memory import embedding

    snapshot = tmp_path / "snapshots" / "deadbeef"
    (snapshot / "onnx").mkdir(parents=True)
    artifact = snapshot / "onnx" / "model.onnx"
    artifact.write_bytes(b"onnx")

    def _fake_cached_file(
        repo_id: str, filename: str, local_files_only: bool = False,
    ) -> str:
        assert local_files_only is True
        assert repo_id == "sentence-transformers/all-MiniLM-L6-v2", (
            "short model ids must be tried under the sentence-transformers/ "
            "org first, mirroring sentence-transformers' own resolution")
        assert filename == "onnx/model.onnx"
        return str(artifact)

    monkeypatch.setattr(embedding, "_cached_hf_file", _fake_cached_file)

    pipe = _pipeline(EmbeddingConfig(
        device="cpu", backend="onnx", model_name="all-MiniLM-L6-v2",
    ))
    assert pipe.backend == "onnx"
    assert captured[0].model_name == str(snapshot)


def test_onnx_online_uses_cached_snapshot_without_remote_constructor(
    captured, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Online mode may inspect only the configured cached artifact.

    Passing the Hub id after preflight would let SentenceTransformers and
    Optimum list a newer, unverified repository head (including all nine
    MiniLM ONNX variants).  The constructor must receive the snapshot that
    owns the already-cached configured file instead.
    """
    from pseudolife_memory.memory import embedding

    snapshot = tmp_path / "snapshots" / "cafebabe"
    (snapshot / "onnx").mkdir(parents=True)
    artifact = snapshot / "onnx" / "model.onnx"
    artifact.write_bytes(b"onnx")

    def _fake_cached_file(repo_id, filename, local_files_only=False):
        assert local_files_only is True
        return str(artifact)

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(embedding, "_cached_hf_file", _fake_cached_file)

    pipe = _pipeline(EmbeddingConfig(
        device="cpu", backend="onnx", model_name="all-MiniLM-L6-v2",
    ))
    assert pipe.backend == "onnx"
    assert captured[0].model_name == str(snapshot)


# ---------------------------------------------------------------------------
# Integration: real model, both backends, parity
# ---------------------------------------------------------------------------


@pytest.mark.real_model
def test_real_onnx_parity_with_torch() -> None:
    """The whole point of the switch: identical cosine geometry.

    Loads the real MiniLM twice (torch + onnx). Pinned to MiniLM
    explicitly (not the config default, Qwen/Qwen3-Embedding-0.6B since
    schema v25, which has no in-repo ONNX export) — this is a non-default
    regression test on ONNX/torch bit-parity for the one model that ships
    the ONNX weights, not a claim about whatever the current default is.
    Skips when optimum isn't installed or the ONNX weights aren't in the
    offline HF cache.
    """
    pytest.importorskip("optimum")
    from pseudolife_memory.memory.embedding import EmbeddingPipeline

    minilm = EmbeddingConfig(device="cpu", model_name="all-MiniLM-L6-v2")
    torch_pipe = _pipeline(minilm)
    try:
        onnx_pipe = EmbeddingPipeline(EmbeddingConfig(
            device="cpu", backend="onnx", model_name="all-MiniLM-L6-v2",
        ))
    except Exception as exc:  # noqa: BLE001 — offline cache miss
        pytest.skip(f"onnx weights unavailable: {exc}")
    if onnx_pipe.backend != "onnx":
        pytest.skip("onnx backend fell back to torch (weights not cached)")

    texts = [
        "the bench postgres runs on port 5433",
        "deploy only via ops/update.ps1 with a rollback tag",
        "what did we decide about the slot token index?",
    ]
    a = torch_pipe.encode(texts)
    b = onnx_pipe.encode(texts)
    cos = (a * b).sum(dim=1)
    assert float(cos.min()) > 0.9999, (
        f"onnx embeddings diverge from torch: min cosine {float(cos.min()):.5f}")
