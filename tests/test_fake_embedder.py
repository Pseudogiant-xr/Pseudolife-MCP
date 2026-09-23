"""The suite's default embedder (tests/fake_embedder.py) and how conftest
chooses between it and the real weights. Only the real_model tests, and
the self-check that runs under PSEUDOLIFE_TEST_EMBEDDER=real, load a model."""

from __future__ import annotations

import itertools
import os
from types import SimpleNamespace

import numpy as np
import pytest

import tests.conftest as suite
from tests.fake_embedder import FakeSentenceTransformer, is_known

QWEN = "Qwen/Qwen3-Embedding-0.6B"
MINILM = "all-MiniLM-L6-v2"
# Tests that pin the default (fake) kind cannot hold when the whole run is
# asked for the real weights (the CI `test` lane).
default_mode_only = pytest.mark.skipif(
    os.environ.get(suite.EMBEDDER_ENV) == "real",
    reason="pins the default fake mode; this run uses the real weights")
QUERY_PREFIX = (
    "Instruct: Given a web search query, retrieve relevant passages that "
    "answer the query\nQuery:"
)


def _unit(model, texts):
    return model.encode(texts, normalize_embeddings=True)


def test_identical_text_gives_identical_vectors_in_every_instance():
    first = _unit(FakeSentenceTransformer(QWEN), ["the bench runs on 5433"])
    again = _unit(FakeSentenceTransformer(QWEN), ["the bench runs on 5433"])
    assert np.array_equal(first, again)


def test_known_models_report_their_real_dimensions():
    assert FakeSentenceTransformer(QWEN).get_sentence_embedding_dimension() == 1024
    assert FakeSentenceTransformer(MINILM).get_sentence_embedding_dimension() == 384
    assert _unit(FakeSentenceTransformer(QWEN), ["x"]).shape == (1, 1024)
    assert not is_known("some/local-snapshot-path")


def test_shared_words_raise_similarity():
    vecs = _unit(FakeSentenceTransformer(QWEN), [
        "the user prefers coffee in the morning",
        "the user prefers tea in the morning",
        "postgres listens on port 5433",
    ])
    sims = vecs @ vecs.T
    assert sims[0, 1] > sims[0, 2]
    assert sims[0, 2] < 0.4


@pytest.mark.parametrize("model", [QWEN, MINILM])
def test_no_two_texts_have_a_negative_cosine(model):
    """Searches over the cortex, world and lesson stores keep hits at a 0.0
    floor, so a fake whose unrelated cosines went negative would drop hits
    the real model keeps. Short texts are where hash collisions bite: over
    this vocabulary a signed hash gave hundreds of negative pairs."""
    words = ["".join(p) for p in itertools.product("aeiourstln", repeat=3)][:400]
    vecs = _unit(FakeSentenceTransformer(model), words)
    assert float((vecs @ vecs.T).min()) >= 0.0


def test_the_query_instruction_is_not_content():
    model = FakeSentenceTransformer(QWEN)
    plain, prefixed = _unit(model, ["coffee order", QUERY_PREFIX + "coffee order"])
    assert float(plain @ prefixed) == pytest.approx(1.0)


def test_unnormalised_output_is_not_unit_length():
    raw = FakeSentenceTransformer(QWEN).encode(["a longer sentence here"])
    assert abs(float(np.linalg.norm(raw)) - 1.0) > 1e-3


def test_unmarked_tests_get_the_fake_and_the_env_restores_real(monkeypatch):
    monkeypatch.delenv(suite.EMBEDDER_ENV, raising=False)
    monkeypatch.setattr(suite, "_real_model_test", False)
    assert suite._use_fake_embedder((QWEN,), {"device": "cpu"})
    assert not suite._use_fake_embedder((QWEN,), {"backend": "onnx"})
    assert not suite._use_fake_embedder(("some/local-snapshot-path",), {})
    monkeypatch.setenv(suite.EMBEDDER_ENV, "real")
    assert not suite._use_fake_embedder((QWEN,), {"device": "cpu"})


@pytest.mark.parametrize("value, mode", [(None, "fake"), ("fake", "fake"),
                                          ("real", "real")])
def test_the_embedder_mode_reads_the_override(value, mode):
    assert suite.embedder_mode({} if value is None else {suite.EMBEDDER_ENV: value}) == mode


@pytest.mark.parametrize("value", ["Real", "1", "true", ""])
def test_an_unknown_embedder_mode_is_refused(value):
    """A typo must not quietly turn CI's all-real lane into a second fake
    lane that still goes green."""
    with pytest.raises(pytest.UsageError, match=suite.EMBEDDER_ENV):
        suite.embedder_mode({suite.EMBEDDER_ENV: value})


@pytest.mark.skipif(os.environ.get(suite.EMBEDDER_ENV) != "real",
                    reason="proves the real-weights override; the default runs the fake")
def test_the_real_override_gives_an_unmarked_test_the_weights():
    """The all-real CI lane checks itself: an unmarked test must get the
    real model when PSEUDOLIFE_TEST_EMBEDDER=real."""
    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import EmbeddingConfig

    pipeline = EmbeddingPipeline(EmbeddingConfig(device="cpu"))
    assert not getattr(pipeline.model, "is_fake", False)


def test_a_real_model_test_gets_the_real_weights(monkeypatch):
    monkeypatch.delenv(suite.EMBEDDER_ENV, raising=False)
    monkeypatch.setattr(suite, "_real_model_test", True)
    assert not suite._use_fake_embedder((QWEN,), {"device": "cpu"})


@default_mode_only
def test_an_unmarked_test_embeds_through_the_fake():
    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import EmbeddingConfig

    pipeline = EmbeddingPipeline(EmbeddingConfig(device="cpu"))
    assert getattr(pipeline.model, "is_fake", False)
    assert pipeline.embedding_dim == 1024


@pytest.mark.real_model
def test_a_marked_test_embeds_through_the_real_weights():
    """End to end through conftest's setup hook and shared loader (this one
    does load the model, once per process, like every real_model test)."""
    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import EmbeddingConfig

    pipeline = EmbeddingPipeline(EmbeddingConfig(device="cpu"))
    assert not getattr(pipeline.model, "is_fake", False)


def test_a_real_model_test_embedding_through_the_fake_fails_loudly(monkeypatch):
    model = FakeSentenceTransformer(QWEN)
    monkeypatch.setattr(suite, "_real_model_test", True)
    with pytest.raises(RuntimeError, match="real_model test is embedding"):
        model.encode(["x"])


@default_mode_only
def test_a_cached_fake_vector_cannot_slip_past_the_guard(monkeypatch):
    """The pipeline's LRU answers repeats without calling the model, so the
    guard sits on the pipeline, not only on the fake's own encode."""
    from pseudolife_memory.memory.embedding import EmbeddingPipeline
    from pseudolife_memory.utils.config import EmbeddingConfig

    pipeline = EmbeddingPipeline(EmbeddingConfig(device="cpu"))
    pipeline.encode(["cached text"])
    monkeypatch.setattr(suite, "_real_model_test", True)
    with pytest.raises(RuntimeError, match="real_model test is embedding"):
        pipeline.encode(["cached text"])


@pytest.mark.parametrize("holds_fake, test_is_real, rebuilt", [
    (True, True, True),
    (False, False, True),
    (True, False, False),
    (False, True, False),
])
def test_pristine_service_rebuilds_the_pipeline_only_on_a_mode_change(
        monkeypatch, holds_fake, test_is_real, rebuilt):
    from pseudolife_memory.memory import embedding
    from pseudolife_memory.utils.config import EmbeddingConfig

    built = []
    monkeypatch.setattr(embedding, "EmbeddingPipeline",
                        lambda config: built.append(config) or "new pipeline")
    monkeypatch.delenv(suite.EMBEDDER_ENV, raising=False)
    monkeypatch.setattr(suite, "_real_model_test", test_is_real)
    held = SimpleNamespace(model=SimpleNamespace(is_fake=holds_fake))
    svc = SimpleNamespace(config=SimpleNamespace(embedding=EmbeddingConfig()),
                          _embedder=held)

    suite._match_embedder_to_test(svc)

    assert (svc._embedder == "new pipeline") is rebuilt
    assert len(built) == int(rebuilt)


def test_pristine_service_leaves_a_real_onnx_pipeline_alone(monkeypatch):
    """ONNX models are never faked, so a rebuild could not change the kind.
    The check reads the backend in use: the MCP defaults configure "onnx",
    and a config whose ONNX load fell back to torch must still be matched."""
    from pseudolife_memory.memory import embedding
    from pseudolife_memory.utils.config import EmbeddingConfig

    built = []
    monkeypatch.setattr(embedding, "EmbeddingPipeline",
                        lambda config: built.append(config) or "new pipeline")
    monkeypatch.delenv(suite.EMBEDDER_ENV, raising=False)
    monkeypatch.setattr(suite, "_real_model_test", False)
    config = EmbeddingConfig(backend="onnx")
    onnx = SimpleNamespace(backend="onnx", model=SimpleNamespace(is_fake=False))
    fell_back = SimpleNamespace(backend="torch", model=SimpleNamespace(is_fake=False))

    svc = SimpleNamespace(config=SimpleNamespace(embedding=config), _embedder=onnx)
    suite._match_embedder_to_test(svc)
    assert svc._embedder is onnx and built == []

    svc = SimpleNamespace(config=SimpleNamespace(embedding=config), _embedder=fell_back)
    suite._match_embedder_to_test(svc)
    assert svc._embedder == "new pipeline"


# The pair below shares conftest's module-scoped service. Run in file order
# (the whole module), the first builds it under the fake and the second
# proves pristine_service swaps it to the real weights; selected alone, the
# second still passes but no longer exercises the swap.
@default_mode_only
def test_pristine_service_starts_on_the_fake(pristine_service):
    assert getattr(pristine_service._embedder.model, "is_fake", False)  # noqa: SLF001


@pytest.mark.real_model
def test_pristine_service_switches_to_real_weights_for_a_marked_test(pristine_service):
    assert not getattr(pristine_service._embedder.model, "is_fake", False)  # noqa: SLF001
