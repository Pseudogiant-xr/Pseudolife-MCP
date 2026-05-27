"""Reference Bank — ChromaDB-backed RAG document store.

The 4th tier of the Continuum Memory System: a persistent vector store
for uploaded documents. Unlike the neural banks (instant/short-term/long-term),
the reference bank does not learn via gradient descent. It uses pure cosine
similarity search via ChromaDB, but participates in the same retrieval
merging pipeline as the neural banks.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import torch

from pseudolife_memory.memory.titans_memory import MemoryEntry, RetrievalResult
from pseudolife_memory.utils.config import ReferenceConfig


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
                from PyPDF2 import PdfReader
                reader = PdfReader(str(path))
                pages = [page.extract_text() or "" for page in reader.pages]
                return "\n\n".join(pages)
            except Exception as e:
                raise ValueError(f"Failed to read PDF: {e}")
        else:
            return path.read_text(encoding="utf-8", errors="replace")


class ReferenceBank:
    """ChromaDB-backed reference document store.

    Stores document chunks as embeddings in a persistent ChromaDB collection.
    Uses pre-computed embeddings from the shared EmbeddingPipeline (not
    ChromaDB's built-in embedding function).
    """

    def __init__(
        self,
        config: ReferenceConfig,
        embedding_dim: int = 384,
    ) -> None:
        self.config = config
        self.embedding_dim = embedding_dim

        import chromadb

        persist_dir = Path(config.persist_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)

        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=config.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def size(self) -> int:
        return self._collection.count()

    def ingest_text(
        self,
        text: str,
        source: str,
        embedder,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> dict:
        """Chunk text, embed, and store in ChromaDB.

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

        # Embed all chunks
        embeddings = embedder.encode(chunks)  # (N, dim) tensor
        if isinstance(embeddings, torch.Tensor):
            embeddings = embeddings.cpu().numpy().tolist()

        # Build IDs, documents, metadatas
        ids = []
        documents = []
        metadatas = []
        emb_list = []
        now = time.time()

        for i, chunk in enumerate(chunks):
            chunk_id = hashlib.md5(f"{source}:{i}:{chunk[:100]}".encode()).hexdigest()
            ids.append(chunk_id)
            documents.append(chunk)
            metadatas.append({
                "source": source,
                "chunk_index": i,
                "timestamp": now,
            })
            emb_list.append(embeddings[i])

        # Upsert into ChromaDB (handles duplicates by ID)
        self._collection.upsert(
            ids=ids,
            embeddings=emb_list,
            documents=documents,
            metadatas=metadatas,
        )

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
    ) -> RetrievalResult:
        """Query ChromaDB and return results as RetrievalResult."""
        k = top_k or self.config.max_results
        if self._collection.count() == 0:
            return RetrievalResult(entries=[], scores=[], surprises=[])

        # Convert tensor to list for ChromaDB
        q = query_embedding.cpu().numpy().tolist()
        if isinstance(q[0], list):
            q = q[0]  # Handle batch dimension

        results = self._collection.query(
            query_embeddings=[q],
            n_results=min(k, self._collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        entries = []
        scores = []
        surprises = []

        if results["documents"] and results["documents"][0]:
            docs = results["documents"][0]
            metas = results["metadatas"][0] if results["metadatas"] else [{}] * len(docs)
            dists = results["distances"][0] if results["distances"] else [0.0] * len(docs)

            for doc, meta, dist in zip(docs, metas, dists):
                # ChromaDB cosine distance: 0 = identical, 2 = opposite
                # Convert to similarity: 1 - (dist / 2)
                similarity = max(0.0, 1.0 - dist / 2.0)
                entry = MemoryEntry(
                    text=doc,
                    embedding=torch.zeros(self.embedding_dim),  # Placeholder
                    surprise_score=0.0,
                    timestamp=meta.get("timestamp", 0.0),
                    access_count=0,
                    source=meta.get("source", ""),
                    bank="reference",
                )
                entries.append(entry)
                scores.append(similarity)
                surprises.append(0.0)

        return RetrievalResult(entries=entries, scores=scores, surprises=surprises)

    def list_documents(self) -> list[dict]:
        """List unique documents with their chunk counts."""
        if self._collection.count() == 0:
            return []

        # Get all metadatas
        all_data = self._collection.get(include=["metadatas"])
        source_map: dict[str, dict] = {}

        for meta in (all_data["metadatas"] or []):
            src = meta.get("source", "unknown")
            if src not in source_map:
                source_map[src] = {
                    "source": src,
                    "chunk_count": 0,
                    "uploaded_at": meta.get("timestamp", 0),
                }
            source_map[src]["chunk_count"] += 1

        docs = sorted(source_map.values(), key=lambda d: d["uploaded_at"], reverse=True)
        return docs

    def delete_document(self, source: str) -> bool:
        """Delete all chunks from a specific source document."""
        if self._collection.count() == 0:
            return False

        # Find IDs with matching source
        all_data = self._collection.get(include=["metadatas"])
        ids_to_delete = []
        for id_, meta in zip(all_data["ids"], all_data["metadatas"] or []):
            if meta.get("source") == source:
                ids_to_delete.append(id_)

        if ids_to_delete:
            self._collection.delete(ids=ids_to_delete)
            return True
        return False

    def clear(self) -> None:
        """Delete all entries from the collection."""
        if self._collection.count() > 0:
            all_ids = self._collection.get()["ids"]
            if all_ids:
                self._collection.delete(ids=all_ids)

    def stats(self) -> dict:
        """Return reference bank statistics."""
        docs = self.list_documents()
        return {
            "reference_bank_size": self._collection.count(),
            "reference_document_count": len(docs),
        }
