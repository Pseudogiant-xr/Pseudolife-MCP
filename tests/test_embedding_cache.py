"""Embedding LRU cache (2026-07-12 perf work).

``service.py`` calls ``encode_single`` from ~25 sites, and the same
strings recur constantly — the query text is embedded for search *and*
slot ops in one request, dedup sweeps re-embed ``"entity attribute"``
keys, warmup re-embeds the probe. Each repeat was a full model forward
(~5ms torch / ~1.6ms onnx). A small LRU keyed on ``(text, normalize)``
makes repeats free.

Contract pinned here:

* a repeat ``encode`` of the same (text, normalize) never reaches the
  model;
* ``cache_size=0`` disables the cache entirely (bitwise-old behavior);
* cached and fresh results are equal, and mutating a returned tensor
  cannot poison the cache (every call returns fresh storage);
* ``normalize`` partitions the key space — the two variants of one text
  are distinct entries;
* batch calls split into hits + misses: only misses reach the model,
  and result order matches the input order;
* eviction is LRU on capacity overflow.
"""
from __future__ import annotations

import numpy as np
import pytest
import torch

from pseudolife_memory.utils.config import EmbeddingConfig


class _CountingST:
    """Deterministic stub that records every text the model encodes."""

    def __init__(self, model_name: str, device: str | None = None, **kwargs) -> None:
        self.encoded: list[list[str]] = []

    def get_sentence_embedding_dimension(self) -> int:
        return 8

    def encode(self, texts, normalize_embeddings=True, **kwargs):
        self.encoded.append(list(texts))
        out = []
        for t in texts:
            rng = np.random.default_rng(sum(ord(c) for c in t))
            v = rng.standard_normal(8)
            if normalize_embeddings:
                v = v / np.linalg.norm(v)
            out.append(v)
        return np.array(out, dtype=np.float32)


@pytest.fixture
def stub_model(monkeypatch: pytest.MonkeyPatch) -> list[_CountingST]:
    from pseudolife_memory.memory import embedding

    instances: list[_CountingST] = []

    def _factory(model_name, device=None, **kwargs):
        inst = _CountingST(model_name, device=device, **kwargs)
        instances.append(inst)
        return inst

    monkeypatch.setattr(embedding, "SentenceTransformer", _factory)
    return instances


def _pipeline(cache_size: int):
    from pseudolife_memory.memory.embedding import EmbeddingPipeline

    return EmbeddingPipeline(EmbeddingConfig(device="cpu", cache_size=cache_size))


def _model_calls(stub_model: list[_CountingST]) -> int:
    return len(stub_model[0].encoded)


def test_repeat_encode_hits_cache(stub_model) -> None:
    pipe = _pipeline(cache_size=16)
    pipe.encode_single("the bench postgres runs on port 5433")
    pipe.encode_single("the bench postgres runs on port 5433")
    assert _model_calls(stub_model) == 1


def test_cache_size_zero_disables_cache(stub_model) -> None:
    pipe = _pipeline(cache_size=0)
    pipe.encode_single("same text")
    pipe.encode_single("same text")
    assert _model_calls(stub_model) == 2


def test_cached_result_equals_fresh(stub_model) -> None:
    pipe = _pipeline(cache_size=16)
    fresh = pipe.encode_single("determinism check")
    cached = pipe.encode_single("determinism check")
    assert torch.allclose(fresh, cached)


def test_mutating_returned_tensor_does_not_poison_cache(stub_model) -> None:
    pipe = _pipeline(cache_size=16)
    first = pipe.encode_single("mutation check")
    pristine = first.clone()
    first += 1.0  # caller misbehaves
    second = pipe.encode_single("mutation check")
    assert torch.allclose(second, pristine), (
        "cache handed out shared storage — a caller mutation corrupted it")


def test_normalize_flag_partitions_cache(stub_model) -> None:
    pipe = _pipeline(cache_size=16)
    normed = pipe.encode_single("partition check", normalize=True)
    raw = pipe.encode_single("partition check", normalize=False)
    assert _model_calls(stub_model) == 2
    assert not torch.allclose(normed, raw)


def test_batch_encode_only_misses_reach_model(stub_model) -> None:
    pipe = _pipeline(cache_size=16)
    a_alone = pipe.encode_single("alpha text")
    out = pipe.encode(["alpha text", "beta text"])
    # Second model call carried only the miss.
    assert stub_model[0].encoded[1] == ["beta text"]
    # Order and values are preserved: row 0 is the cached alpha.
    assert out.shape == (2, 8)
    assert torch.allclose(out[0], a_alone)
    assert torch.allclose(out[1], pipe.encode_single("beta text"))


def test_lru_eviction_on_capacity(stub_model) -> None:
    pipe = _pipeline(cache_size=2)
    pipe.encode_single("one")
    pipe.encode_single("two")
    pipe.encode_single("three")  # evicts "one" (least recently used)
    calls_before = _model_calls(stub_model)
    pipe.encode_single("three")  # still cached
    assert _model_calls(stub_model) == calls_before
    pipe.encode_single("one")  # was evicted -> model call
    assert _model_calls(stub_model) == calls_before + 1
