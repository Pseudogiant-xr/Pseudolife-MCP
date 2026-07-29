"""The suite loads each distinct embedding model once, not once per module.

Found 2026-07-29: `pytest tests/` reached ~49.5 GB private commit on a 61.6 GB
host and killed a run outright — a fatal interpreter crash with a faulthandler
dump, not a test failure. Two causes, both measured:

* ``warm_service`` is module-scoped, and ``MemoryService._ensure_init()``
  builds its own ``EmbeddingPipeline`` -> its own ``SentenceTransformer`` with
  no shared cache anywhere. Under the schema-v25 default
  (``Qwen/Qwen3-Embedding-0.6B``) that is **2.52 GB per service**, roughly 26x
  the MiniLM it replaced, times ~90 test modules.
* Dropping the last reference frees nothing — the services sit in reference
  cycles, so refcounting cannot touch them — and an explicit ``gc.collect()``
  returned only 1.89 of 7.55 GB, because the allocator keeps freed arenas.

``conftest.pytest_configure`` therefore memoizes the model LOAD for the
session. It deliberately does **not** share the ``EmbeddingPipeline``: each
pipeline keeps its own encode LRU and dim state, so no cache crosses a test
boundary. The weights are read-only at inference.

Measured on the full suite: peak 49.5 GB -> 5.3 GB, runtime 13:23 -> 6:50.

These tests pin both halves of the contract — that sharing happens, and that
it stops where sharing would be unsafe.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pseudolife_memory.memory.embedding import EmbeddingPipeline
from pseudolife_memory.utils.config import EmbeddingConfig


def test_pipelines_with_the_same_config_share_one_loaded_model() -> None:
    """The optimization itself. Without it every test module pays 2.52 GB."""
    first = EmbeddingPipeline(EmbeddingConfig())
    second = EmbeddingPipeline(EmbeddingConfig())

    assert first is not second, "the pipelines themselves must stay distinct"
    assert first.model is second.model, (
        "two EmbeddingPipelines built from an identical config did not share "
        "one loaded model — conftest's session memoization is not in effect, "
        "and the suite is back to ~2.52 GB per test module (~49.5 GB peak, "
        "enough to kill the run on a 64 GB host)"
    )


def test_pipelines_keep_their_own_encode_cache() -> None:
    """Sharing weights must not share per-pipeline state. The encode LRU is
    keyed on ``(text, normalize)`` with no notion of which bank asked, so a
    shared cache would let one test serve another test's embedding."""
    first = EmbeddingPipeline(EmbeddingConfig())
    second = EmbeddingPipeline(EmbeddingConfig())

    assert first._cache is not second._cache, (  # noqa: SLF001 — the contract
        "EmbeddingPipelines are sharing an encode cache; memoize the model "
        "load, never the pipeline"
    )


def test_a_different_max_seq_length_gets_its_own_model() -> None:
    """THE HAZARD, pinned.

    ``EmbeddingPipeline.__init__`` MUTATES the model it just built::

        self.model.max_seq_length = min(existing_max_seq_len or 512,
                                        config.max_seq_length)

    ``existing_max_seq_len`` is read off the model, so on a *shared* model it
    is the previous pipeline's already-capped value, and the cap ratchets
    downward permanently: cap 512 then 128 leaves the first pipeline's model
    at 128 too, and a later 512 can never restore it.

    This is the one piece of state separate instances provided implicitly, so
    the memoization key must include it. Caught by inspection before landing —
    the full suite passed 1789 tests *with* the naive key, because nothing
    currently varies this cap. That is precisely what makes it a trap: correct
    today, silently wrong for whoever first varies it.

    Uses MiniLM, not the session default: the hazard lives in the keying
    logic, and proving it on the 2.5 GB Qwen3 model would make this guard the
    single most expensive module in the suite (measured: it alone raised the
    full-suite peak 5.3 -> 7.7 GB before the switch). The deliberately-capped
    second load costs ~90 MB this way.
    """
    base = replace(EmbeddingConfig(), model_name="all-MiniLM-L6-v2")
    short = replace(base, max_seq_length=max(8, base.max_seq_length // 4))
    if short.max_seq_length == base.max_seq_length:  # pragma: no cover
        pytest.skip("default max_seq_length too small to halve meaningfully")

    default_pipe = EmbeddingPipeline(base)
    capped_pipe = EmbeddingPipeline(short)

    assert default_pipe.model is not capped_pipe.model, (
        "pipelines with different max_seq_length shared one model; the "
        "post-construction cap in EmbeddingPipeline.__init__ would then "
        "ratchet down and silently shorten the other pipeline's inputs"
    )
    # The config promises a CAP (min with the model's native length), not an
    # exact value — MiniLM's native 256 undercuts the 512 default, so assert
    # the ratchet did not cross between the two models instead.
    assert capped_pipe.model.max_seq_length == short.max_seq_length
    assert default_pipe.model.max_seq_length > short.max_seq_length, (
        "the capped pipeline's max_seq_length leaked onto the default "
        "pipeline's model — exactly the ratchet this test exists to prevent"
    )
