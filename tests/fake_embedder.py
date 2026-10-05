"""A deterministic, weight-free stand-in for ``SentenceTransformer``.

The suite's default embedder: loading Qwen3-Embedding-0.6B costs ~2.5 GB per
process and every forward pass is tens of milliseconds of CPU, while most
tests only need an embedder that is deterministic and keeps distinct texts
apart. Tests whose assertions depend on the real model's geometry carry
``@pytest.mark.real_model`` and get the real weights (``tests/conftest.py``).

The vectors are feature hashes of the text's lower-cased word tokens plus
their character trigrams, L2-normalised: identical text gives an identical
vector, texts that share words have a cosine that grows with the overlap, and
unrelated texts sit at the floor. A fixed component shared by every vector
sets that floor near the real model's typical unrelated cosine. The hashing
is unsigned, so every component is non-negative and no cosine falls below the
floor (a signed hash let short texts collide to negative cosines, which the
0.0 search floors of the cortex, world and lesson stores would drop). Hashing
uses BLAKE2b, not ``hash()``, so the vectors are identical across processes
and runs.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

import numpy as np

from pseudolife_memory.utils.config import EmbeddingConfig

# Models the fake stands in for, with the dimensions and native sequence
# caps the real checkpoints report. Anything else loads for real.
KNOWN_MODELS: dict[str, tuple[int, int]] = {
    "Qwen/Qwen3-Embedding-0.6B": (1024, 32768),
    "all-MiniLM-L6-v2": (384, 256),
    "sentence-transformers/all-MiniLM-L6-v2": (384, 256),
}

# Share of every vector's squared norm on the common component, i.e. the
# cosine two texts with no token in common end up with (the minimum).
_FLOOR = 0.2
_TRIGRAM_WEIGHT = 0.5
_WORD = re.compile(r"\w+", re.UNICODE)
_QUERY_PREFIX = EmbeddingConfig().query_prefix


def is_known(model_name: object) -> bool:
    return isinstance(model_name, str) and model_name in KNOWN_MODELS


def _strip_instruction(text: str) -> str:
    # EmbeddingPipeline.encode_query prepends the configured instruction; the
    # real model reads it as an instruction, not as content to match.
    if text.startswith(_QUERY_PREFIX):
        return text[len(_QUERY_PREFIX):]
    return text


def _features(text: str) -> list[tuple[str, float]]:
    feats: list[tuple[str, float]] = []
    for word in _WORD.findall(_strip_instruction(text).lower()):
        feats.append((word, 1.0))
        padded = f"<{word}>"
        for i in range(len(padded) - 2):
            feats.append(("#" + padded[i:i + 3], _TRIGRAM_WEIGHT))
    return feats


@lru_cache(maxsize=65536)
def _bucket(feature: str, dim: int) -> int:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    # Bucket 0 is reserved for the common component.
    return 1 + int.from_bytes(digest, "little") % (dim - 1)


def raw_vector(text: str, dim: int) -> np.ndarray:
    """Unnormalised embedding (the real models' ``normalize=False`` output
    is not unit-length either)."""
    vec = np.zeros(dim, dtype=np.float32)
    for feature, weight in _features(text):
        vec[_bucket(feature, dim)] += weight
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        # Empty or punctuation-only text: the common component alone.
        vec[0] = 1.0
        return vec
    vec *= np.sqrt(1.0 - _FLOOR) / norm
    vec[0] = np.sqrt(_FLOOR)
    # A length-dependent scale, so unnormalised output is not accidentally
    # unit-length; normalising removes it.
    return vec * (1.0 + 0.01 * (len(text) % 97))


class FakeSentenceTransformer:
    """The subset of ``SentenceTransformer`` that ``EmbeddingPipeline`` uses."""

    is_fake = True
    # Called before every encode; tests/conftest.py installs a check that a
    # real_model test never embeds through the fake.
    guard = None

    def __init__(self, model_name_or_path: str, device: str | None = None,
                 **kwargs) -> None:
        dim, native_cap = KNOWN_MODELS[model_name_or_path]
        self.model_name = model_name_or_path
        self.device = device or "cpu"
        self.max_seq_length = native_cap
        self._dim = dim

    def float(self) -> "FakeSentenceTransformer":
        return self

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    get_embedding_dimension = get_sentence_embedding_dimension

    def encode(self, sentences, batch_size: int = 32,
               normalize_embeddings: bool = False,
               convert_to_numpy: bool = True,
               show_progress_bar: bool = False, **kwargs):
        if self.guard is not None:
            self.guard()
        single = isinstance(sentences, str)
        texts = [sentences] if single else list(sentences)
        if not texts:
            out = np.zeros((0, self._dim), dtype=np.float32)
        else:
            out = np.stack([raw_vector(t, self._dim) for t in texts])
            if normalize_embeddings:
                out /= np.linalg.norm(out, axis=1, keepdims=True)
        return out[0] if single else out
