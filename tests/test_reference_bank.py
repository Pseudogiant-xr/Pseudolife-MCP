"""ReferenceBank scoring (2026-07-02 review M1) and its lazy ChromaDB client."""

import pytest
import torch

from pseudolife_memory.memory.reference_bank import (
    ReferenceBank,
    cosine_similarity_from_distance,
)
from pseudolife_memory.utils.config import ReferenceConfig


def test_chroma_distance_to_similarity():
    """ChromaDB cosine distance is 1 - cos (range [0,2]); similarity must be
    1 - dist. The old 1 - dist/2 mapped an ORTHOGONAL chunk to 0.5 — above
    the 0.25 retrieval floor — so unrelated documents were appended to
    essentially every search result."""
    assert cosine_similarity_from_distance(0.0) == 1.0     # identical
    assert cosine_similarity_from_distance(1.0) == 0.0     # orthogonal -> floor-fails
    assert cosine_similarity_from_distance(2.0) == 0.0     # opposite, clamped
    assert cosine_similarity_from_distance(0.3) == 0.7


class _Embedder:
    """``encode`` the way ``EmbeddingPipeline`` does: an (N, dim) tensor."""

    def encode(self, texts):
        rows = []
        for text in texts:
            gen = torch.Generator().manual_seed(sum(map(ord, text)))
            row = torch.randn(4, generator=gen)
            rows.append(row / row.norm())
        return torch.stack(rows)


def _count_opened_clients(monkeypatch):
    import chromadb

    opened = []
    real = chromadb.PersistentClient

    def counting(*args, **kwargs):
        opened.append(kwargs.get("path", args[0] if args else None))
        return real(*args, **kwargs)

    monkeypatch.setattr(chromadb, "PersistentClient", counting)
    return opened


def _bank(tmp_path):
    return ReferenceBank(
        ReferenceConfig(persist_dir=str(tmp_path / "chromadb")), embedding_dim=4,
    )


def test_an_unused_bank_never_opens_a_chromadb_client(tmp_path, monkeypatch):
    """Every ``MemoryService`` builds a ReferenceBank, and chromadb keeps each
    opened client's System (about 19 threads and ~3 MB on a 16-CPU host) in
    a process-wide cache for the life of the process, and the test suite
    builds services by the thousand. A bank nothing has been ingested into
    answers every read like an empty collection without opening a client."""
    opened = _count_opened_clients(monkeypatch)
    bank = _bank(tmp_path)

    assert bank.size == 0
    result = bank.retrieve(torch.ones(4))
    assert (result.entries, result.scores, result.surprises) == ([], [], [])
    assert bank.list_documents() == []
    assert bank.stats() == {
        "reference_bank_size": 0, "reference_document_count": 0,
    }
    bank.clear()
    assert opened == []
    assert not any((tmp_path / "chromadb").iterdir())


def test_ingest_opens_the_store_and_a_restarted_bank_reads_it(tmp_path, monkeypatch):
    opened = _count_opened_clients(monkeypatch)
    bank = _bank(tmp_path)
    embedder = _Embedder()

    stored = bank.ingest_text("alpha beta gamma", source="doc.txt", embedder=embedder)

    assert stored == {"chunks_total": 1, "chunks_stored": 1}
    assert len(opened) == 1
    # Closing releases chromadb's cached System for the path, so the
    # restarted bank reads the store back from disk, as a new process would.
    bank._client.close()  # noqa: SLF001
    restarted = _bank(tmp_path)
    assert restarted.size == 1
    assert len(opened) == 2
    hit = restarted.retrieve(embedder.encode(["alpha beta gamma"])[0])
    assert [entry.text for entry in hit.entries] == ["alpha beta gamma"]
    assert [doc["source"] for doc in restarted.list_documents()] == ["doc.txt"]


def test_a_store_that_cannot_open_reads_empty_and_refuses_ingest(tmp_path, monkeypatch):
    """Opening used to happen in the constructor, where MemoryService turned
    a failure into "reference bank disabled": searches kept working and
    ingest refused. The same holds when the failure happens on first use."""
    import chromadb

    attempts = []

    def broken(*args, **kwargs):
        attempts.append(kwargs.get("path"))
        raise RuntimeError("corrupt chroma.sqlite3")

    store = tmp_path / "chromadb"
    store.mkdir()
    (store / "chroma.sqlite3").write_bytes(b"not a database")
    monkeypatch.setattr(chromadb, "PersistentClient", broken)
    bank = _bank(tmp_path)

    assert bank.retrieve(torch.ones(4)).entries == []
    assert bank.size == 0
    assert bank.list_documents() == []
    with pytest.raises(RuntimeError, match="Reference bank disabled"):
        bank.ingest_text("alpha", source="doc.txt", embedder=_Embedder())
    assert len(attempts) == 1  # the failure is remembered, not retried per call


def test_a_store_that_cannot_be_listed_reads_empty_instead_of_raising(tmp_path, monkeypatch):
    """The store-exists check is part of opening: an unreadable directory
    disables the bank like any other open failure, so memory search (which
    reads the reference pool on every call) keeps working."""
    from pathlib import Path

    store = tmp_path / "chromadb"
    real_iterdir = Path.iterdir

    def unreadable(self):
        if self == store:
            raise PermissionError(13, "Access is denied", str(self))
        return real_iterdir(self)

    bank = _bank(tmp_path)
    monkeypatch.setattr(Path, "iterdir", unreadable)

    assert bank.retrieve(torch.ones(4)).entries == []
    assert bank.size == 0
    with pytest.raises(RuntimeError, match="Reference bank disabled"):
        bank.ingest_text("alpha", source="doc.txt", embedder=_Embedder())


def test_service_start_opens_no_chromadb_client(tmp_path, monkeypatch):
    from pseudolife_memory.service import MemoryService

    class _Embedding:
        embedding_dim = 1024

    monkeypatch.setattr(
        "pseudolife_memory.service.EmbeddingPipeline", lambda config: _Embedding(),
    )
    monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    opened = _count_opened_clients(monkeypatch)

    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()  # noqa: SLF001 — the start path under test

    assert svc._reference is not None  # noqa: SLF001
    assert opened == []
