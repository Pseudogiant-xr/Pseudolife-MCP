"""Asymmetric encode API (embedding-backbone-v25, Task 1).

Qwen3-Embedding-0.6B (the default since Task 2's schema v25 swap) is
instruction-asymmetric: retrieval QUERIES carry a card-verbatim prefix,
stored DOCUMENTS do not. This pins the pipeline-level API using a stubbed
model, so every test below must hold under a SYMMETRIC model too (that's
the whole point of ``query_prefix`` being config-driven and empty-able,
independent of which model is actually loaded).

Contract pinned here:

* ``encode_query`` prepends ``config.query_prefix`` to the text, then
  flows through the exact same cache/model path as ``encode_single`` —
  no second cache;
* the cache key is the PREFIXED text, so a query and a document sharing
  identical raw text occupy different cache slots whenever the prefix is
  non-empty;
* ``query_prefix=""`` makes ``encode_query(t)`` byte-identical to
  ``encode_single(t)`` — the symmetric-model compatibility guarantee;
* ``encode``/``encode_single`` are untouched — still bare, document-side;
* ``config.max_seq_length`` is applied to the loaded (torch-backend)
  model, capping whatever the model shipped with.
"""
from __future__ import annotations

import numpy as np
import pytest
import torch

from pseudolife_memory.utils.config import EmbeddingConfig


class _CountingST:
    """Deterministic stub that records every text the model encodes.

    Mirrors the stub in tests/test_embedding_cache.py, plus a settable
    ``max_seq_length`` attribute so the seq-length-capping contract can be
    pinned without downloading the real model.
    """

    def __init__(self, model_name: str, device: str | None = None, **kwargs) -> None:
        self.encoded: list[list[str]] = []
        self.max_seq_length = 8192  # simulate a long-context model's default

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


def _pipeline(**overrides):
    from pseudolife_memory.memory.embedding import EmbeddingPipeline

    cfg = EmbeddingConfig(device="cpu", cache_size=16, **overrides)
    return EmbeddingPipeline(cfg)


def _model_calls(stub_model: list[_CountingST]) -> int:
    return len(stub_model[0].encoded)


# ---------------------------------------------------------------------------
# Default query_prefix value (exact-string contract)
# ---------------------------------------------------------------------------


def test_default_query_prefix_is_the_exact_qwen3_string() -> None:
    cfg = EmbeddingConfig()
    assert cfg.query_prefix == (
        "Instruct: Given a web search query, retrieve relevant passages "
        "that answer the query\nQuery:"
    )
    assert not cfg.query_prefix.endswith("Query: "), (
        "no space after 'Query:' — instruction-tuned embedders swing on wording")


# ---------------------------------------------------------------------------
# Prefix applied on query only
# ---------------------------------------------------------------------------


def test_encode_query_sends_prefixed_text_to_model(stub_model) -> None:
    pipe = _pipeline()
    pipe.encode_query("what port does the bench postgres use")
    assert stub_model[0].encoded[-1] == [
        pipe.config.query_prefix + "what port does the bench postgres use",
    ]


def test_encode_single_sends_bare_text_to_model(stub_model) -> None:
    pipe = _pipeline()
    pipe.encode_single("what port does the bench postgres use")
    assert stub_model[0].encoded[-1] == ["what port does the bench postgres use"]


def test_encode_query_result_matches_manually_prefixed_encode_single(stub_model) -> None:
    pipe = _pipeline()
    text = "the bench postgres runs on port 5433"
    via_query = pipe.encode_query(text)
    via_manual_prefix = pipe.encode_single(pipe.config.query_prefix + text)
    assert torch.allclose(via_query, via_manual_prefix)
    # And it must be the SAME cache slot (no second call for the second line).
    assert _model_calls(stub_model) == 1


# ---------------------------------------------------------------------------
# Empty-prefix identity (symmetric-model compatibility guarantee)
# ---------------------------------------------------------------------------


def test_empty_query_prefix_is_byte_identical_to_encode_single(stub_model) -> None:
    pipe = _pipeline(query_prefix="")
    text = "the bench postgres runs on port 5433"
    via_query = pipe.encode_query(text)
    via_single = pipe.encode_single(text)
    assert torch.equal(via_query, via_single)
    # Same cache key -> only one model call for both.
    assert _model_calls(stub_model) == 1


def test_empty_query_prefix_sends_bare_text_to_model(stub_model) -> None:
    pipe = _pipeline(query_prefix="")
    pipe.encode_query("bare text check")
    assert stub_model[0].encoded[-1] == ["bare text check"]


# ---------------------------------------------------------------------------
# Cache disjointness: identical raw text, query side vs document side
# ---------------------------------------------------------------------------


def test_query_and_document_of_identical_raw_text_occupy_different_cache_slots(
    stub_model,
) -> None:
    pipe = _pipeline()  # default non-empty query_prefix
    text = "payments database host is db-3"

    pipe.encode_single(text)       # document-side: bare text is the key
    pipe.encode_single(text)       # repeat -> cache hit, no new model call
    assert _model_calls(stub_model) == 1

    pipe.encode_query(text)        # query-side: prefixed text is a DIFFERENT key
    assert _model_calls(stub_model) == 2, (
        "query and document encodes of identical raw text must not share a "
        "cache slot — the prefix must make the keyspace disjoint")

    pipe.encode_query(text)        # repeat query -> cache hit again
    assert _model_calls(stub_model) == 2


def test_query_and_document_embeddings_of_identical_raw_text_differ(stub_model) -> None:
    pipe = _pipeline()
    text = "payments database host is db-3"
    doc_vec = pipe.encode_single(text)
    query_vec = pipe.encode_query(text)
    assert not torch.allclose(doc_vec, query_vec)


# ---------------------------------------------------------------------------
# max_seq_length applied to the (torch-backend) model
# ---------------------------------------------------------------------------


def test_max_seq_length_caps_a_long_context_model(stub_model) -> None:
    # _CountingST starts at max_seq_length=8192 (simulating a long-context
    # model); the pipeline must cap it down to the configured value.
    pipe = _pipeline(max_seq_length=200)
    assert pipe.model.max_seq_length == 200


def test_max_seq_length_default_512_caps_long_context_model(stub_model) -> None:
    pipe = _pipeline()  # max_seq_length defaults to 512
    assert pipe.model.max_seq_length == 512


def test_max_seq_length_does_not_raise_a_shorter_model_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model whose native default (e.g. MiniLM's 256) is already below
    the configured cap must NOT be raised back up to the cap."""
    from pseudolife_memory.memory import embedding

    class _ShortST(_CountingST):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            self.max_seq_length = 256

    monkeypatch.setattr(embedding, "SentenceTransformer", _ShortST)
    pipe = _pipeline(max_seq_length=512)
    assert pipe.model.max_seq_length == 256
