"""Embedding pipeline for converting text to dense vectors."""

from __future__ import annotations

import logging
import sys
import threading
from collections import OrderedDict
from pathlib import Path, PurePosixPath


import torch
import numpy as np
from sentence_transformers import SentenceTransformer

from pseudolife_memory.onnx_artifacts import (
    has_nested_transformer_module,
    onnx_layout_available,
)
from pseudolife_memory.utils.config import EmbeddingConfig

logger = logging.getLogger(__name__)


def _native_windows() -> bool:
    return sys.platform == "win32"


def _cached_hf_file(
    repo_id: str,
    filename: str,
    local_files_only: bool = True,
) -> str:
    """Return one configured file from a locally cached Hub snapshot."""
    from huggingface_hub import hf_hub_download  # noqa: PLC0415

    return hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_files_only=local_files_only,
    )


def _onnx_file_parts(file_name: str) -> tuple[str, ...]:
    """Validate a model-relative ONNX path and return its components."""
    normalized = file_name.replace("\\", "/")
    raw_parts = normalized.split("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(not part or part in {".", ".."} or ":" in part for part in raw_parts)
        or path.suffix != ".onnx"
    ):
        raise ValueError(
            "embedding.onnx_file_name must be a relative .onnx path "
            "inside the configured model",
        )
    return path.parts


def _resolve_onnx_source(model_name: str, file_name: str) -> str | None:
    """Resolve a verified existing ONNX artifact to its local model root.

    Both SentenceTransformers and Optimum infer that a missing artifact should
    be exported, on every platform. The preflight verifies the configured
    artifact at every recognized Transformer module's effective path before
    either library sees the request.

    Hub models are resolved through ``hf_hub_download(local_files_only=True)``
    even when networking is enabled. It probes the configured artifact or the
    minimal ``modules.json`` / ``config.json`` metadata needed to find effective
    module paths, then returns their revision-specific cached snapshot. It does
    not list a mutable remote head or download every ONNX variant.
    """
    parts = _onnx_file_parts(file_name)
    local_root = Path(model_name).expanduser()
    if local_root.is_dir():
        return str(local_root) if onnx_layout_available(local_root, file_name) else None

    # Mirror sentence-transformers' short-id resolution: bare names live
    # under the sentence-transformers/ org.
    candidates = (
        [model_name]
        if "/" in model_name
        else [f"sentence-transformers/{model_name}", model_name]
    )
    lookups = ("/".join(parts), "modules.json", "config.json")
    for repo_id in candidates:
        for lookup in lookups:
            try:
                artifact = Path(_cached_hf_file(
                    repo_id,
                    lookup,
                    local_files_only=True,
                ))
            except Exception:  # noqa: BLE001 — absent from this local cache key
                continue
            lookup_parts = PurePosixPath(lookup).parts
            if tuple(artifact.parts[-len(lookup_parts):]) != lookup_parts:
                continue
            snapshot_root = artifact
            for _ in lookup_parts:
                snapshot_root = snapshot_root.parent
            if onnx_layout_available(
                snapshot_root,
                file_name,
                allow_hub_blob_links=True,
            ):
                return str(snapshot_root)
    return None


class EmbeddingPipeline:
    """Encodes text into dense vector embeddings using sentence-transformers.

    Two perf levers (both config-driven, both fail-soft):

    * ``backend = "onnx"`` uses sentence-transformers' native ONNX backend.
      The historical MiniLM fp32 artifact measured ~3x faster single-text CPU
      encoding with cosine 1.00000 against torch; other configured artifacts
      carry no equivalence claim. The backend is load-only: its configured
      artifact must already exist in a local model or cached Hub snapshot. It
      falls back to torch when the artifact is missing, and when a nested
      module layout would be mis-detected on native Windows.
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
                file_name = getattr(
                    config, "onnx_file_name", "onnx/model.onnx",
                )
                file_name = "/".join(_onnx_file_parts(file_name))
                onnx_source = _resolve_onnx_source(
                    config.model_name,
                    file_name,
                )
                if onnx_source is None:
                    raise FileNotFoundError(
                        f"configured ONNX artifact {file_name!r} is not "
                        "available in the local model or Hub cache",
                    )
                if _native_windows() and has_nested_transformer_module(
                    onnx_source,
                ):
                    # Narrower than a platform gate: the pinned Optimum stack
                    # matches a POSIX subfolder pattern against OS-native path
                    # strings, so only a nested module subfolder goes
                    # undetected here and re-enables export. A flat layout
                    # resolves on both platforms and keeps the accelerator.
                    logger.warning(
                        "ONNX embedding backend is disabled on native Windows "
                        "for this model's nested module layout because the "
                        "pinned Optimum stack mis-detects a nested "
                        "subfolder's existing ONNX artifact and enables "
                        "export; falling back to torch before ONNX "
                        "construction.",
                    )
                else:
                    self.model = SentenceTransformer(
                        onnx_source,
                        device=device,
                        backend="onnx",
                        model_kwargs={
                            "file_name": file_name, "export": False,
                        },
                    )
                    self.backend = "onnx"
            except Exception as exc:  # noqa: BLE001 — optional accelerator
                logger.warning(
                    "ONNX embedding backend failed to load (%s) — falling "
                    "back to torch.",
                    exc,
                )
        if self.model is None:
            self.model = SentenceTransformer(
                config.model_name,
                device=device,
            )
        # Cap the tokenizer's max sequence length. Applies to both backends:
        # SentenceTransformer.max_seq_length delegates to the underlying
        # Transformer module regardless of which runtime (torch/onnx) does
        # the forward pass, so one assignment covers both — there is no
        # separate ONNX knob to hack around. A cap only (min with whatever
        # the model shipped with), never a raise, so a model whose native
        # default is already shorter than the configured cap is untouched.
        existing_max_seq_len = getattr(self.model, "max_seq_length", None)
        self.model.max_seq_length = min(
            existing_max_seq_len or 512, config.max_seq_length,
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
        if not texts:
            # torch.stack raises on an empty list; preserve the pre-cache
            # contract of returning an empty (0, dim) tensor.
            return torch.empty((0, self._dim))

        rows: list[torch.Tensor | None] = [None] * len(texts)
        misses: list[int] = []
        if self._cache_size:
            with self._cache_lock:
                for i, text in enumerate(texts):
                    key = (text, normalize)
                    cached = self._cache.get(key)
                    if cached is None:
                        misses.append(i)
                    else:
                        self._cache.move_to_end(key)
                        rows[i] = cached
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

    def encode_query(self, text: str, normalize: bool = True) -> torch.Tensor:
        """Encode a retrieval QUERY (the asymmetric-model, query side).

        Prepends ``config.query_prefix`` before encoding and delegates to
        ``encode_single`` — so it flows through the exact same LRU cache as
        document encodes, keyed on the PREFIXED text. That makes the
        keyspace disjoint from document-side ``encode``/``encode_single``
        calls of identical raw text (no second cache to keep in sync).

        With ``query_prefix=""`` (a symmetric model, e.g. the current
        MiniLM default) this is byte-identical to ``encode_single(text)``.

        Returns shape (embedding_dim,).
        """
        return self.encode_single(self.config.query_prefix + text, normalize=normalize)
