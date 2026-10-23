"""Reference Bank — the document store (``document_ingest``).

The 4th tier of the Continuum Memory System: a persistent vector store
for uploaded documents. Unlike the neural banks (instant/short-term/long-term),
the reference bank does not learn via gradient descent. It uses pure cosine
similarity search, but participates in the same retrieval merging pipeline
as the neural banks.

Since schema v57 the chunks live in the Postgres bank (``reference_chunks``),
so every backup and transfer of the bank carries them. Before v57 they lived
in a chromadb store under ``<data_dir>/chromadb``; ``storage/migrate_chroma.py``
copies such a store in once.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

import torch

from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.utils.config import ReferenceConfig

logger = logging.getLogger(__name__)

# Chunks per encode() call while ingesting a document. Every chunk is ~512
# tokens, and the encode's activation memory grows with the batch. Ingest
# used to hand the whole document to one encode(), batched at
# embedding.batch_size (16 in the daemon). Measured 2026-09-23 with the
# Qwen3-Embedding-0.6B default in a throwaway container from the 0.15.0
# image, a 104-chunk document: one fp32 encode peaked 5,397 MB (+2,565 MB
# over steady state, past the then 4g cap on its own); slices of 8 peaked
# 3,886 MB (+1,028 MB) at the same speed (167 s vs 171 s). bf16 single
# encodes of 4/8/16 chunks: +372/+519/+1,000 MB.
INGEST_ENCODE_BATCH = 8


def cosine_similarity_from_distance(dist: float) -> float:
    """Cosine *distance* (chromadb's, and pgvector's ``<=>``) is ``1 − cos``
    (range [0, 2]), so the similarity is ``1 − dist``, clamped at 0. The old
    ``1 − dist/2`` mapped an orthogonal chunk to 0.5 — above the 0.25
    retrieval floor — so unrelated documents were appended to essentially
    every search result (2026-07-02 review M1)."""
    return max(0.0, 1.0 - float(dist))


def _chunk_text(text: str, chunk_size: int = 512, chunk_overlap: int = 64) -> list[str]:
    """Split text into overlapping chunks by token-approximate character count."""
    # Rough heuristic: 1 token ~ 4 chars
    char_size = chunk_size * 4
    char_overlap = chunk_overlap * 4
    step = max(char_size - char_overlap, 1)
    chunks = []
    for i in range(0, len(text), step):
        chunk = text[i:i + char_size].strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def chunk_id(source: str, index: int, chunk: str) -> str:
    """A chunk's id: unchanged from the chromadb store, so a re-ingest
    replaces the rows an earlier ingest (or the v57 import) wrote."""
    return hashlib.md5(f"{source}:{index}:{chunk[:100]}".encode()).hexdigest()


def _read_file(path: Path) -> str:
    """Read text content from a file using the document parser."""
    try:
        from pseudolife_memory.memory.document_parser import extract_text
        return extract_text(path)
    except ImportError:
        # Fallback if document_parser unavailable
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(path))
                pages = [page.extract_text() or "" for page in reader.pages]
                return "\n\n".join(pages)
            except Exception as e:
                raise ValueError(f"Failed to read PDF: {e}")
        else:
            return path.read_text(encoding="utf-8", errors="replace")


class ReferenceBank:
    """The document store over the Postgres bank's ``reference_chunks``.

    Stores document chunks with pre-computed embeddings from the shared
    EmbeddingPipeline. Search is an exact cosine scan in SQL (no approximate
    index), ordered by distance and then chunk id, so equal inputs serve
    equal results in a deterministic order.

    What the chromadb store provided implicitly, and what stands now:

    * **Chunk identity.** The id is still md5 of ``source:index:text[:100]``,
      and a re-ingest still replaces rows with the same id (chromadb's
      upsert). A shorter re-ingest still leaves the old tail chunks in place.
    * **Order among equal distances.** chromadb left it to its index
      (insertion order before compaction, HNSW order after). Now: chunk id,
      byte order.
    * **Lazy open.** There is no client to open: the bank reads the storage
      the service already holds. A bank with no documents costs one empty
      query per search.
    * **Disabled when unavailable.** A store that failed to open made reads
      empty and ingest refuse. Now a failed read in the memory-search pool
      (``strict=False``) logs and serves no reference hits, retrying on the
      next call; ``document_search`` (``strict=True``) and ingest raise. File
      mode has no bank at all (the service builds none without Postgres).
    """

    def __init__(
        self,
        config: ReferenceConfig,
        storage,
        embedding_dim: int = 1024,
    ) -> None:
        self.config = config
        self.storage = storage
        self.embedding_dim = embedding_dim

    @property
    def size(self) -> int:
        return self.storage.count_reference_chunks()

    def ingest_text(
        self,
        text: str,
        source: str,
        embedder,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> dict:
        """Chunk text, embed, and store the chunks in the bank.

        Args:
            text: Raw text content.
            source: Source identifier (filename, URL, etc.).
            embedder: EmbeddingPipeline instance with encode() method.
            chunk_size: Override config chunk_size.
            chunk_overlap: Override config chunk_overlap.

        Returns:
            {"chunks_total": N, "chunks_stored": M}
        """
        cs = chunk_size or self.config.chunk_size
        co = chunk_overlap or self.config.chunk_overlap
        chunks = _chunk_text(text, cs, co)

        if not chunks:
            return {"chunks_total": 0, "chunks_stored": 0}

        # Embed in bounded slices, never the whole document in one call.
        embeddings: list = []
        for start in range(0, len(chunks), INGEST_ENCODE_BATCH):
            batch = embedder.encode(chunks[start:start + INGEST_ENCODE_BATCH])
            if isinstance(batch, torch.Tensor):
                batch = batch.cpu().numpy().tolist()
            embeddings.extend(batch)

        now = time.time()
        rows = [
            {
                "id": chunk_id(source, i, chunk),
                "source": source,
                "chunk_index": i,
                "text": chunk,
                "embedding": embeddings[i],
                "metadata": {"source": source, "chunk_index": i,
                             "timestamp": now},
                "ingested_at": now,
            }
            for i, chunk in enumerate(chunks)
        ]
        self.storage.upsert_reference_chunks(rows)

        return {"chunks_total": len(chunks), "chunks_stored": len(chunks)}

    def ingest_file(
        self,
        file_path: Path,
        source: str | None,
        embedder,
    ) -> dict:
        """Read a file and ingest its content.

        Supports .txt, .md, .pdf files.
        """
        file_path = Path(file_path)
        text = _read_file(file_path)
        src = source or file_path.name
        return self.ingest_text(text, src, embedder)

    def retrieve(
        self,
        query_embedding: torch.Tensor,
        top_k: int | None = None,
        *,
        strict: bool = False,
    ) -> RetrievalResult:
        """The ``top_k`` nearest chunks as a RetrievalResult.

        ``strict=False`` (the memory-search reference pool) turns a failed
        read into no hits; ``strict=True`` raises it."""
        k = top_k or self.config.max_results
        q = query_embedding
        if isinstance(q, torch.Tensor):
            q = q.detach().cpu().reshape(-1, q.shape[-1])[0].numpy()
        try:
            rows = self.storage.search_reference_chunks(q, k)
        except Exception as exc:  # noqa: BLE001 — optional tier
            if strict:
                raise
            logger.warning("reference pool skipped: document query failed: %s",
                           exc)
            return RetrievalResult(entries=[], scores=[], surprises=[])

        entries = []
        scores = []
        surprises = []
        for text, source, ingested_at, dist in rows:
            entries.append(MemoryEntry(
                text=text,
                embedding=torch.zeros(self.embedding_dim),  # Placeholder
                surprise_score=0.0,
                timestamp=ingested_at,
                access_count=0,
                source=source,
                bank="reference",
            ))
            scores.append(cosine_similarity_from_distance(dist))
            surprises.append(0.0)

        return RetrievalResult(entries=entries, scores=scores, surprises=surprises)

    def list_documents(self) -> list[dict]:
        """Unique documents with their chunk counts, newest ingest first."""
        return self.storage.reference_documents()

    def clear(self) -> None:
        """Delete every chunk."""
        self.storage.clear_reference_chunks()

    def stats(self) -> dict:
        """Return reference bank statistics."""
        docs = self.list_documents()
        return {
            "reference_bank_size": sum(d["chunk_count"] for d in docs),
            "reference_document_count": len(docs),
        }
