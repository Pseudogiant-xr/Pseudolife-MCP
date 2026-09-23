"""document_ingest encodes a document in small, bounded slices.

A document's chunks are all ~512 tokens (``chunk_size`` 512, ~4 chars per
token), and ingest used to hand the WHOLE chunk list to one ``encode()``
call, which sentence-transformers then batched at ``embedding.batch_size``
(16 in the daemon, 64 in the library). Measured 2026-09-23 in a throwaway
container from the production image with the fp32 Qwen3-Embedding-0.6B
embedder: a 104-chunk document ingested the old way peaked +2,565 MB over
steady state (slices of 8: +1,028 MB, same speed), and a 32 x 512-token
batch +4.4 GB — against a daemon that then had ~200 MB of headroom under
its cgroup cap. Any real document would have OOM-killed it. Production held
no documents, so the path had simply never run there.
"""
from __future__ import annotations

import torch

from pseudolife_memory.memory import reference_bank as rb
from pseudolife_memory.utils.config import ReferenceConfig


class _RecordingEmbedder:
    """Records each encode() call; each row is derived from its own text,
    so a stored vector can be checked against the chunk it belongs to."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @staticmethod
    def vector(text: str) -> list[float]:
        return [float(len(text)), float(sum(map(ord, text)) % 997), 1.0, 0.0]

    def encode(self, texts):
        self.calls.append(list(texts))
        return torch.tensor([self.vector(t) for t in texts])


class _FakeCollection:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def upsert(self, **kwargs) -> None:
        self.upserts.append(kwargs)


def _bank() -> rb.ReferenceBank:
    # ingest_text touches only config and the collection; skip ChromaDB.
    bank = rb.ReferenceBank.__new__(rb.ReferenceBank)
    bank.config = ReferenceConfig()
    bank.embedding_dim = 4
    bank._collection = _FakeCollection()
    return bank


def _document() -> str:
    return " ".join(f"paragraph-{i} of a long operator manual" for i in range(3000))


def test_ingest_never_encodes_more_than_the_bound_per_call() -> None:
    bank, emb = _bank(), _RecordingEmbedder()
    result = bank.ingest_text(_document(), "manual.md", emb)

    assert result["chunks_total"] > 3 * rb.INGEST_ENCODE_BATCH
    assert max(len(call) for call in emb.calls) <= rb.INGEST_ENCODE_BATCH
    assert len(emb.calls) > 1


def test_the_ingest_bound_stays_small() -> None:
    assert 1 <= rb.INGEST_ENCODE_BATCH <= 8, (
        "a 32 x 512-token fp32 encode measured +4.4 GB (2026-09-23); the "
        "ingest slice is what keeps a document from OOM-killing the daemon")


def test_slicing_keeps_every_chunk_paired_with_its_own_vector() -> None:
    bank, emb = _bank(), _RecordingEmbedder()
    text = _document()
    result = bank.ingest_text(text, "manual.md", emb)

    chunks = rb._chunk_text(text, bank.config.chunk_size, bank.config.chunk_overlap)
    assert [t for call in emb.calls for t in call] == chunks
    (upsert,) = bank._collection.upserts
    assert upsert["documents"] == chunks
    assert len(upsert["ids"]) == len(set(upsert["ids"])) == len(chunks)
    for doc, vec in zip(upsert["documents"], upsert["embeddings"]):
        assert vec == _RecordingEmbedder.vector(doc)
    assert result == {"chunks_total": len(chunks), "chunks_stored": len(chunks)}
