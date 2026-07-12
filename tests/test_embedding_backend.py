"""ONNX embedding backend (2026-07-12 perf work).

The embedder is the daemon's dominant per-request cost (~5ms/encode on
CPU torch). sentence-transformers' native ``backend="onnx"`` runs the
same MiniLM through onnxruntime at ~3x the speed with *bit-identical*
cosine geometry (benchmarked: min cosine vs torch = 1.00000 over 20
texts), so the switch carries zero retrieval-quality risk.

Contract pinned here:

* ``backend`` defaults to ``"torch"`` and the torch path must NOT pass
  a ``backend=`` kwarg to SentenceTransformer — the pyproject floor
  (sentence-transformers>=2.2) predates the kwarg;
* ``backend="onnx"`` passes ``backend`` + ``model_kwargs={"file_name"}``
  (default ``onnx/model.onnx`` — without it ST warns and picks a file
  nondeterministically from the repo's nine ONNX variants);
* an ONNX load failure (optimum missing, file not cached offline) falls
  back to torch with a warning — same fail-soft philosophy as the
  reranker: memory operations never break because of an optional
  accelerator;
* an unknown backend name is a config error and raises.

Unit tests stub SentenceTransformer (no model download); one
integration test loads the real model both ways and asserts parity.
"""
from __future__ import annotations

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


def test_default_backend_is_torch_and_omits_backend_kwarg(captured) -> None:
    pipe = _pipeline(EmbeddingConfig(device="cpu"))
    assert pipe.backend == "torch"
    assert len(captured) == 1
    assert "backend" not in captured[0].kwargs, (
        "torch path must not pass backend= — sentence-transformers 2.x "
        "(the pyproject floor) does not accept the kwarg")


def test_onnx_backend_passes_backend_and_default_file_name(captured) -> None:
    pipe = _pipeline(EmbeddingConfig(device="cpu", backend="onnx"))
    assert pipe.backend == "onnx"
    assert captured[0].kwargs["backend"] == "onnx"
    assert captured[0].kwargs["model_kwargs"] == {"file_name": "onnx/model.onnx"}


def test_onnx_backend_honors_custom_file_name(captured) -> None:
    cfg = EmbeddingConfig(
        device="cpu", backend="onnx",
        onnx_file_name="onnx/model_qint8_avx512_vnni.onnx",
    )
    pipe = _pipeline(cfg)
    assert captured[0].kwargs["model_kwargs"] == {
        "file_name": "onnx/model_qint8_avx512_vnni.onnx",
    }


def test_onnx_load_failure_falls_back_to_torch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
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

    with caplog.at_level("WARNING"):
        pipe = _pipeline(EmbeddingConfig(device="cpu", backend="onnx"))
    assert pipe.backend == "torch"
    assert len(instances) == 1  # the fallback torch construction
    assert any("onnx" in r.message.lower() for r in caplog.records), (
        "the silent-fallback must at least log a warning")
    # And the pipeline actually works post-fallback.
    assert pipe.encode_single("still alive").shape == (8,)


def test_unknown_backend_raises(captured) -> None:
    with pytest.raises(ValueError, match="backend"):
        _pipeline(EmbeddingConfig(device="cpu", backend="banana"))


def test_onnx_offline_resolves_local_snapshot_path(
    captured, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deployment-critical: the ONNX loader (optimum) lists the hub repo
    tree even when every file is cached, which raises under
    HF_HUB_OFFLINE=1 — the Docker daemon's runtime contract. In offline
    mode the pipeline must resolve the repo to its local snapshot
    directory and pass THAT path, sidestepping the hub entirely."""
    from pseudolife_memory.memory import embedding

    def _fake_snapshot(repo_id: str, local_files_only: bool = False) -> str:
        assert local_files_only is True
        assert repo_id == "sentence-transformers/all-MiniLM-L6-v2", (
            "short model ids must be tried under the sentence-transformers/ "
            "org first, mirroring sentence-transformers' own resolution")
        return "/opt/hf/snapshots/deadbeef"

    monkeypatch.setattr(embedding, "_hf_offline", lambda: True)
    monkeypatch.setattr(embedding, "_local_snapshot", _fake_snapshot)

    pipe = _pipeline(EmbeddingConfig(device="cpu", backend="onnx"))
    assert pipe.backend == "onnx"
    assert captured[0].model_name == "/opt/hf/snapshots/deadbeef"


# ---------------------------------------------------------------------------
# Integration: real model, both backends, parity
# ---------------------------------------------------------------------------


def test_real_onnx_parity_with_torch() -> None:
    """The whole point of the switch: identical cosine geometry.

    Loads the real MiniLM twice (torch + onnx). Skips when optimum isn't
    installed or the ONNX weights aren't in the offline HF cache.
    """
    pytest.importorskip("optimum")
    from pseudolife_memory.memory.embedding import EmbeddingPipeline

    torch_pipe = _pipeline(EmbeddingConfig(device="cpu"))
    try:
        onnx_pipe = EmbeddingPipeline(EmbeddingConfig(device="cpu", backend="onnx"))
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
