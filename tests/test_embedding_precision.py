"""CPU embeddings retain FP32 even when model-loading defaults change."""
import pytest
import torch

from pseudolife_memory.memory import embedding
from pseudolife_memory.utils.config import EmbeddingConfig


@pytest.mark.parametrize(
    "device,cuda_available,backend,onnx_fails,expected_dtype",
    [
        ("cpu", False, "torch", False, torch.float32),
        ("cpu:0", False, "torch", False, torch.float32),
        ("cuda", False, "torch", False, torch.float32),
        ("cuda", True, "torch", False, torch.bfloat16),
        ("mps", False, "torch", False, torch.bfloat16),
        ("cpu", False, "onnx", False, torch.bfloat16),
        ("cpu", False, "onnx", True, torch.float32),
    ],
)
def test_precision_follows_resolved_device_and_backend(
    monkeypatch, device, cuda_available, backend, onnx_fails, expected_dtype,
):
    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.ones(2, dtype=torch.bfloat16))
            self.register_buffer("buffer", torch.ones(2, dtype=torch.bfloat16))

        def get_sentence_embedding_dimension(self):
            return 2

    model = Model()

    def load(*args, **kwargs):
        if kwargs.get("backend") == "onnx" and onnx_fails:
            raise RuntimeError("optional backend unavailable")
        return model

    monkeypatch.setattr(embedding, "SentenceTransformer", load)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda_available)
    monkeypatch.setattr(embedding, "_native_windows", lambda: False)
    monkeypatch.setattr(embedding, "_resolve_onnx_source", lambda *args: "/verified")
    pipeline = embedding.EmbeddingPipeline(EmbeddingConfig(device=device, backend=backend))

    assert pipeline.model is model
    assert model.weight.dtype == expected_dtype
    assert model.buffer.dtype == expected_dtype


@pytest.mark.real_model
def test_real_cpu_model_is_fp32():
    pipeline = embedding.EmbeddingPipeline(EmbeddingConfig(device="cpu"))
    assert next(pipeline.model.parameters()).dtype == torch.float32
    vector = pipeline.encode("CI retains the real embedding model.")
    assert vector.dtype == torch.float32
    assert torch.isfinite(vector).all()
