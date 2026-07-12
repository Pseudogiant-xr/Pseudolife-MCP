"""Embedding pipeline for converting text to dense vectors."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

import torch
import numpy as np
from sentence_transformers import SentenceTransformer

from pseudolife_memory.utils.config import EmbeddingConfig

logger = logging.getLogger(__name__)


def _hf_offline() -> bool:
    """True when the HF hub is in offline mode (HF_HUB_OFFLINE=1)."""
    from huggingface_hub import constants  # noqa: PLC0415

    return bool(getattr(constants, "HF_HUB_OFFLINE", False))


def _local_snapshot(repo_id: str, local_files_only: bool = True) -> str:
    """Resolve a hub repo id to its local cache snapshot directory."""
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    return snapshot_download(repo_id, local_files_only=local_files_only)


def _resolve_onnx_source(model_name: str) -> str:
    """Pick what to hand SentenceTransformer for an ONNX load.

    The ONNX loader (optimum) lists the hub repo tree even when every
    file is already cached — that API call has no offline cache and
    raises under HF_HUB_OFFLINE=1, the Docker daemon's runtime contract.
    In offline mode, resolve the repo to its local snapshot directory
    and load from that path: no hub calls at all. Online, keep the hub
    id so first use can still download.
    """
    if not _hf_offline():
        return model_name
    # Mirror sentence-transformers' short-id resolution: bare names live
    # under the sentence-transformers/ org.
    candidates = (
        [model_name]
        if "/" in model_name
        else [f"sentence-transformers/{model_name}", model_name]
    )
    for repo_id in candidates:
        try:
            return _local_snapshot(repo_id, local_files_only=True)
        except Exception:  # noqa: BLE001 — not cached under this id
            continue
    return model_name


class EmbeddingPipeline:
    """Encodes text into dense vector embeddings using sentence-transformers.

    Two perf levers (both config-driven, both fail-soft):

    * ``backend = "onnx"`` runs the same model through onnxruntime via
      sentence-transformers' native ONNX backend — ~3x faster single-text
      encode on CPU with bit-identical embeddings (fp32 ONNX cosine vs
      torch = 1.00000). Falls back to torch with a warning when optimum
      is missing or the ONNX weights aren't in the (offline) HF cache.
    * ``cache_size > 0`` keeps an LRU of ``(text, normalize)`` →
      embedding. The service embeds the same strings repeatedly within
      and across requests (query text for search + slot ops, dedup keys,
      warmup probes); repeats skip the model forward entirely.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        # Auto-fallback to CPU if CUDA requested but not available
        device = config.device
        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        self._device = device

        requested = getattr(config, "backend", "torch")
        if requested not in ("torch", "onnx"):
            raise ValueError(
                f"embedding backend must be 'torch' or 'onnx', got {requested!r}",
            )
        self.model = None
        self.backend = "torch"
        if requested == "onnx":
            try:
                self.model = SentenceTransformer(
                    _resolve_onnx_source(config.model_name),
                    device=device,
                    backend="onnx",
                    model_kwargs={
                        "file_name": getattr(
                            config, "onnx_file_name", "onnx/model.onnx",
                        ),
                    },
                )
                self.backend = "onnx"
            except Exception as exc:  # noqa: BLE001 — optional accelerator
                logger.warning(
                    "ONNX embedding backend failed to load (%s) — falling "
                    "back to torch. Embeddings are identical either way; "
                    "only encode latency differs.",
                    exc,
                )
        if self.model is None:
            self.model = SentenceTransformer(
                config.model_name,
                device=device,
            )
        self._dim = self.model.get_sentence_embedding_dimension()
        # Positive confirmation of the active backend: the ONNX path fails
        # soft, so without this line a broken accelerator in the deployed
        # container would silently revert to torch while /health stays
        # green. "verify live" = grep the daemon log for this.
        logger.info(
            "Embedding backend: %s (model=%s, dim=%d, device=%s)",
            self.backend, config.model_name, self._dim, self._device,
        )

        self._cache_size = max(0, int(getattr(config, "cache_size", 0) or 0))
        self._cache: OrderedDict[tuple[str, bool], torch.Tensor] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0

    @property
    def embedding_dim(self) -> int:
        """Dimensionality of the output embeddings."""
        return self._dim

    def encode(
        self,
        texts: str | list[str],
        normalize: bool = True,
    ) -> torch.Tensor:
        """Encode text(s) into embedding vectors.

        Args:
            texts: A single string or list of strings to encode.
            normalize: If True, L2-normalize the embeddings (recommended for
                       Hopfield retrieval where we use dot-product similarity).

        Returns:
            torch.Tensor of shape (N, embedding_dim) on the configured device.
        """
        if isinstance(texts, str):
            texts = [texts]

        rows: list[torch.Tensor | None] = [None] * len(texts)
        misses: list[int] = []
        if self._cache_size:
            with self._cache_lock:
                for i, text in enumerate(texts):
                    key = (text, normalize)
                    cached = self._cache.get(key)
                    if cached is None:
                        misses.append(i)
                        self.cache_misses += 1
                    else:
                        self._cache.move_to_end(key)
                        rows[i] = cached
                        self.cache_hits += 1
        else:
            misses = list(range(len(texts)))

        if misses:
            embeddings = self.model.encode(
                [texts[i] for i in misses],
                batch_size=self.config.batch_size,
                normalize_embeddings=normalize,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            fresh = torch.from_numpy(np.array(embeddings)).float()
            for row, i in enumerate(misses):
                rows[i] = fresh[row]
            if self._cache_size:
                with self._cache_lock:
                    for i in misses:
                        key = (texts[i], normalize)
                        self._cache[key] = rows[i]
                        self._cache.move_to_end(key)
                    while len(self._cache) > self._cache_size:
                        self._cache.popitem(last=False)

        # stack COPIES each row — callers can never mutate cached storage
        # through the returned tensor.
        tensor = torch.stack(rows)
        if self._device == "cuda":
            tensor = tensor.cuda()

        return tensor

    def encode_single(self, text: str, normalize: bool = True) -> torch.Tensor:
        """Encode a single text string. Returns shape (embedding_dim,)."""
        return self.encode(text, normalize=normalize).squeeze(0)
