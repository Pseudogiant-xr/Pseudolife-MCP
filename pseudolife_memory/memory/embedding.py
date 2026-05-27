"""Embedding pipeline for converting text to dense vectors."""

from __future__ import annotations

import torch
import numpy as np
from sentence_transformers import SentenceTransformer

from pseudolife_memory.utils.config import EmbeddingConfig


class EmbeddingPipeline:
    """Encodes text into dense vector embeddings using sentence-transformers.

    Runs on GPU by default (RTX 4090). The embedding model is lightweight
    (~0.3 GB VRAM) and leaves plenty of room for the Hopfield memory and LLM.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        # Auto-fallback to CPU if CUDA requested but not available
        device = config.device
        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        self._device = device
        self.model = SentenceTransformer(
            config.model_name,
            device=device,
        )
        self._dim = self.model.get_sentence_embedding_dimension()

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

        embeddings = self.model.encode(
            texts,
            batch_size=self.config.batch_size,
            normalize_embeddings=normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )

        tensor = torch.from_numpy(np.array(embeddings)).float()
        if self._device == "cuda":
            tensor = tensor.cuda()

        return tensor

    def encode_single(self, text: str, normalize: bool = True) -> torch.Tensor:
        """Encode a single text string. Returns shape (embedding_dim,)."""
        return self.encode(text, normalize=normalize).squeeze(0)
