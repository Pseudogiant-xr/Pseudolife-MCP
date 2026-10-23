"""The reference bank (ingested documents) in the Postgres bank, schema v57.

Until v57 the documents lived in a chromadb store under ``<data_dir>/chromadb``.
They now live in the ``reference_chunks`` table, searched by exact cosine
order, and a bank that reaches v57 copies an existing chromadb store in once
(``storage/migrate_chroma.py``), leaving the store itself untouched on disk.
"""

from __future__ import annotations

import hashlib
import tarfile

import pytest
import torch

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.memory import reference_bank as rb
from pseudolife_memory.memory.reference_bank import (
    ReferenceBank,
    cosine_similarity_from_distance,
)
from pseudolife_memory.utils.config import ReferenceConfig

DIM = 1024
COLLECTION = ReferenceConfig().collection_name


def test_chroma_distance_to_similarity():
    """Cosine distance is 1 - cos (range [0,2]) in chromadb and in pgvector's
    ``<=>`` alike; similarity must be 1 - dist. The old 1 - dist/2 mapped an
    ORTHOGONAL chunk to 0.5 — above the 0.25 retrieval floor — so unrelated
    documents were appended to essentially every search result."""
    assert cosine_similarity_from_distance(0.0) == 1.0     # identical
    assert cosine_similarity_from_distance(1.0) == 0.0     # orthogonal -> floor-fails
    assert cosine_similarity_from_distance(2.0) == 0.0     # opposite, clamped
    assert cosine_similarity_from_distance(0.3) == 0.7


class _Embedder:
    """``encode`` the way ``EmbeddingPipeline`` does: an (N, dim) tensor of
    unit rows, each seeded by its own text so it is reproducible."""

    embedding_dim = DIM

    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def row(text: str) -> torch.Tensor:
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        row = torch.randn(DIM, generator=torch.Generator().manual_seed(seed))
        return row / row.norm()

    def encode(self, texts):
        self.calls += 1
        return torch.stack([self.row(t) for t in texts])

    def encode_query(self, text):
        return self.encode([text])[0] if isinstance(text, str) else self.encode(text)


# A fixed corpus of single-chunk documents (each well under one 2048-char
# chunk), so every document is exactly one row.
CORPUS = {f"doc-{i}.md": f"Document {i}: notes on subject {i * 7 % 11} "
          f"and its follow-up item {i}." for i in range(12)}
QUERIES = ["notes on subject 3", "follow-up item 9", "Document 0",
           "an unrelated question about weather"]


@pytest.fixture()
def storage(pg_url, pg_conn):  # noqa: F811 — pg_conn truncates first
    from pseudolife_memory.storage.postgres import PostgresStorage

    store = PostgresStorage(pg_url)
    try:
        yield store
    finally:
        store.close()


def _bank(storage) -> ReferenceBank:
    return ReferenceBank(ReferenceConfig(), storage, embedding_dim=DIM)


# ── the bank on Postgres ─────────────────────────────────────────────────


def test_ingest_then_retrieve_reads_the_chunk_back(storage):
    bank, emb = _bank(storage), _Embedder()
    assert bank.size == 0
    assert bank.retrieve(emb.row("anything")).entries == []

    stored = bank.ingest_text("alpha beta gamma", source="doc.txt", embedder=emb)

    assert stored == {"chunks_total": 1, "chunks_stored": 1}
    assert bank.size == 1
    hit = bank.retrieve(emb.row("alpha beta gamma"))
    assert [e.text for e in hit.entries] == ["alpha beta gamma"]
    assert [e.source for e in hit.entries] == ["doc.txt"]
    assert [e.bank for e in hit.entries] == ["reference"]
    assert hit.scores == [pytest.approx(1.0, abs=1e-5)]
    assert hit.entries[0].timestamp > 0
    assert [d["source"] for d in bank.list_documents()] == ["doc.txt"]
    assert bank.stats() == {"reference_bank_size": 1,
                            "reference_document_count": 1}
    # A fresh bank over the same storage (a restarted daemon) reads it too.
    assert _bank(storage).size == 1


def test_a_chunk_row_keeps_the_chromadb_shape(storage):
    """The row carries what the chromadb store held for a chunk: the same
    id (md5 of source, index and text head), the text, the vector, and the
    metadata dict ``{source, chunk_index, timestamp}`` verbatim."""
    bank, emb = _bank(storage), _Embedder()
    bank.ingest_text("alpha beta gamma", source="doc.txt", embedder=emb)

    row = storage.conn.execute(
        "SELECT id, source, chunk_index, text, metadata, ingested_at, "
        "created_at FROM reference_chunks").fetchone()
    chunk_id, source, index, text, metadata, ingested_at, created_at = row
    assert chunk_id == hashlib.md5(b"doc.txt:0:alpha beta gamma").hexdigest()
    assert (source, index, text) == ("doc.txt", 0, "alpha beta gamma")
    assert metadata == {"source": "doc.txt", "chunk_index": 0,
                        "timestamp": ingested_at}
    assert created_at == ingested_at


def test_reingest_replaces_a_chunk_in_place_and_keeps_its_creation_time(storage):
    bank, emb = _bank(storage), _Embedder()
    prefix = "Stable introduction. " * 6
    bank.ingest_text(prefix + "Original ending.", source="doc", embedder=emb)
    (created, first) = storage.conn.execute(
        "SELECT created_at, ingested_at FROM reference_chunks").fetchone()

    bank.ingest_text(prefix + "Replacement ending.", source="doc", embedder=emb)

    rows = storage.conn.execute(
        "SELECT text, created_at, ingested_at FROM reference_chunks").fetchall()
    assert [r[0] for r in rows] == [prefix + "Replacement ending."]
    assert rows[0][1] == created
    assert rows[0][2] >= first


def test_retrieve_is_exact_cosine_order_with_a_chunk_id_tie_break(storage):
    """Exact search (no approximate index) with a total order: distance, then
    chunk id in byte order. Two chunks with one vector tie on distance; the
    smaller id comes first on every call."""
    emb = _Embedder()
    vec = emb.row("shared")
    rows = [
        {"id": "ff" * 16, "source": "b", "chunk_index": 0, "text": "second",
         "embedding": vec, "metadata": {}, "ingested_at": 1.0},
        {"id": "0a" * 16, "source": "a", "chunk_index": 0, "text": "first",
         "embedding": vec, "metadata": {}, "ingested_at": 1.0},
        {"id": "55" * 16, "source": "c", "chunk_index": 0, "text": "far",
         "embedding": emb.row("elsewhere"), "metadata": {}, "ingested_at": 1.0},
    ]
    storage.upsert_reference_chunks(rows)
    bank = _bank(storage)

    for _ in range(3):
        hit = bank.retrieve(vec, top_k=3)
        assert [e.text for e in hit.entries] == ["first", "second", "far"]
    assert hit.scores[0] == hit.scores[1]
    indexes = storage.conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'reference_chunks'"
    ).fetchall()
    assert not any(("hnsw" in d or "ivfflat" in d) for (d,) in indexes)


def test_list_documents_is_newest_ingest_first(storage):
    bank = _bank(storage)
    emb = _Embedder()
    storage.upsert_reference_chunks([
        {"id": "a1", "source": "old.md", "chunk_index": 0, "text": "o",
         "embedding": emb.row("o"), "metadata": {}, "ingested_at": 10.0},
        {"id": "b1", "source": "new.md", "chunk_index": 0, "text": "n0",
         "embedding": emb.row("n0"), "metadata": {}, "ingested_at": 20.0},
        {"id": "b2", "source": "new.md", "chunk_index": 1, "text": "n1",
         "embedding": emb.row("n1"), "metadata": {}, "ingested_at": 20.0},
    ])
    assert bank.list_documents() == [
        {"source": "new.md", "chunk_count": 2, "uploaded_at": 20.0},
        {"source": "old.md", "chunk_count": 1, "uploaded_at": 10.0},
    ]


def test_a_failing_read_serves_no_reference_hits_in_lenient_mode(storage):
    """memory_search reads the reference pool on every call; a failed
    document query must not take the memory search down with it, as an
    unavailable chromadb store never did. document_search, whose whole job
    is that query, still raises."""
    bank = _bank(storage)

    def broken(*args, **kwargs):
        raise RuntimeError("connection lost")

    storage.search_reference_chunks = broken
    assert bank.retrieve(torch.ones(DIM)).entries == []
    with pytest.raises(RuntimeError, match="connection lost"):
        bank.retrieve(torch.ones(DIM), strict=True)


# ── the one-time chromadb migration ──────────────────────────────────────


def _legacy_ingest(store, docs: dict[str, str], *, compact: bool,
                   embedder=None, dim: int = DIM) -> None:
    """Write a chromadb store exactly the way the pre-v57 ReferenceBank did
    (same collection, cosine space, chunk ids and metadata). ``compact``
    sets HNSW thresholds low enough that the vectors move out of
    chroma.sqlite3's embeddings_queue into the HNSW segment files."""
    import chromadb

    embedder = embedder or _Embedder()
    meta = {"hnsw:space": "cosine"}
    if compact:
        meta |= {"hnsw:batch_size": 2, "hnsw:sync_threshold": 4}
    client = chromadb.PersistentClient(path=str(store))
    try:
        col = client.get_or_create_collection(name=COLLECTION, metadata=meta)
        for source, text in docs.items():
            chunks = rb._chunk_text(text)
            vecs = embedder.encode(chunks)[:, :dim].tolist()
            now = 1_700_000_000.0 + len(source)
            col.upsert(
                ids=[hashlib.md5(f"{source}:{i}:{c[:100]}".encode()).hexdigest()
                     for i, c in enumerate(chunks)],
                embeddings=vecs, documents=chunks,
                metadatas=[{"source": source, "chunk_index": i, "timestamp": now}
                           for i in range(len(chunks))])
    finally:
        client.close()


def _chroma_results(store, query_vec, k: int) -> list[tuple]:
    """What the pre-v57 ReferenceBank.retrieve served for a query."""
    import chromadb

    client = chromadb.PersistentClient(path=str(store))
    try:
        col = client.get_collection(COLLECTION)
        res = col.query(query_embeddings=[query_vec.tolist()],
                        n_results=min(k, col.count()),
                        include=["documents", "metadatas", "distances"])
    finally:
        client.close()
    return [(doc, cosine_similarity_from_distance(dist), meta["source"],
             meta["timestamp"])
            for doc, meta, dist in zip(res["documents"][0],
                                       res["metadatas"][0],
                                       res["distances"][0])]


def _chroma_rows(store) -> dict[str, tuple]:
    import chromadb

    client = chromadb.PersistentClient(path=str(store))
    try:
        got = client.get_collection(COLLECTION).get(
            include=["embeddings", "documents", "metadatas"])
    finally:
        client.close()
    return {i: (d, m, [float(x) for x in e]) for i, d, m, e in
            zip(got["ids"], got["documents"], got["metadatas"],
                got["embeddings"])}


def _queue_rows(store) -> int:
    import sqlite3

    con = sqlite3.connect(store / "chroma.sqlite3")
    try:
        return con.execute("SELECT count(*) FROM embeddings_queue").fetchone()[0]
    finally:
        con.close()


def _tree_digest(root) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _migrate(store, storage, embedder=None):
    from pseudolife_memory.storage import migrate_chroma

    return migrate_chroma.import_legacy_store(
        store, COLLECTION, storage, embedder or _Embedder())


def _count_opened_clients(monkeypatch):
    import chromadb

    opened = []
    real = chromadb.PersistentClient

    def counting(*args, **kwargs):
        opened.append(kwargs.get("path", args[0] if args else None))
        return real(*args, **kwargs)

    monkeypatch.setattr(chromadb, "PersistentClient", counting)
    return opened


@pytest.mark.parametrize("compact", [False, True],
                         ids=["before-compaction", "after-compaction"])
def test_migration_copies_every_chunk_verbatim(tmp_path, storage, compact):
    store = tmp_path / "chromadb"
    _legacy_ingest(store, CORPUS, compact=compact)
    if compact:
        # The point of this fixture: the vectors are no longer in the
        # sqlite write-ahead queue, only in the HNSW segment files.
        assert _queue_rows(store) < len(CORPUS)
        assert list(store.rglob("index_metadata.pickle"))
    else:
        assert _queue_rows(store) == len(CORPUS)
    legacy = _chroma_rows(store)
    on_disk = _tree_digest(store)

    result = _migrate(store, storage)

    assert result["migrated"] is True
    assert result["chunks"] == result["inserted"] == len(CORPUS)
    assert result["reembedded"] == 0
    rows = storage.conn.execute(
        "SELECT id, text, metadata, embedding, source, chunk_index, "
        "ingested_at FROM reference_chunks").fetchall()
    assert len(rows) == len(legacy)
    for chunk_id, text, metadata, vec, source, index, ingested_at in rows:
        doc, meta, emb = legacy[chunk_id]
        assert (text, metadata) == (doc, meta)
        assert (source, index, ingested_at) == (
            meta["source"], meta["chunk_index"], meta["timestamp"])
        vec = vec.to_numpy() if hasattr(vec, "to_numpy") else vec
        assert [float(x) for x in vec] == emb  # float32, verbatim
    # The chromadb store is never written or removed.
    assert _tree_digest(store) == on_disk


@pytest.mark.parametrize("compact", [False, True],
                         ids=["before-compaction", "after-compaction"])
def test_search_serves_the_same_hits_before_and_after_migration(
        tmp_path, storage, compact):
    store = tmp_path / "chromadb"
    emb = _Embedder()
    _legacy_ingest(store, CORPUS, compact=compact, embedder=emb)
    before = {(q, k): _chroma_results(store, emb.row(q), k)
              for q in QUERIES for k in (3, 5)}

    _migrate(store, storage)
    bank = _bank(storage)

    for (q, k), old in before.items():
        hit = bank.retrieve(emb.row(q), top_k=k)
        new = list(zip([e.text for e in hit.entries], hit.scores,
                       [e.source for e in hit.entries],
                       [e.timestamp for e in hit.entries]))
        assert [n[0] for n in new] == [o[0] for o in old], (q, k)
        assert [n[2:] for n in new] == [o[2:] for o in old], (q, k)
        for (_, s_new, *_), (_, s_old, *_) in zip(new, old):
            assert s_new == pytest.approx(s_old, abs=1e-5), (q, k)


def test_migration_runs_once_and_never_clobbers_newer_rows(
        tmp_path, storage, monkeypatch):
    store = tmp_path / "chromadb"
    _legacy_ingest(store, CORPUS, compact=False)
    assert _migrate(store, storage)["inserted"] == len(CORPUS)
    marker = storage.meta_get("reference_chroma_import")
    assert marker["status"] == "done" and marker["chunks"] == len(CORPUS)

    opened = _count_opened_clients(monkeypatch)
    again = _migrate(store, storage)
    assert again == {"migrated": False, "reason": "already_imported"}
    assert opened == []  # the store is not even opened again

    # With the marker gone (a hand-edited bank), a rerun inserts nothing and
    # leaves a document re-ingested since the import as it now stands.
    source, text = next(iter(CORPUS.items()))
    chunk_id = hashlib.md5(f"{source}:0:{text[:100]}".encode()).hexdigest()
    storage.conn.execute(
        "UPDATE reference_chunks SET text = 'edited since' WHERE id = %s",
        (chunk_id,))
    storage.conn.execute("DELETE FROM meta WHERE key = 'reference_chroma_import'")
    rerun = _migrate(store, storage)
    assert rerun["migrated"] is True and rerun["inserted"] == 0
    assert storage.conn.execute(
        "SELECT text FROM reference_chunks WHERE id = %s",
        (chunk_id,)).fetchone() == ("edited since",)
    assert _bank(storage).size == len(CORPUS)


def test_no_store_and_an_empty_store_open_nothing_and_mark_nothing(
        tmp_path, storage, monkeypatch):
    opened = _count_opened_clients(monkeypatch)
    missing = tmp_path / "absent"
    empty = tmp_path / "empty"
    empty.mkdir()

    assert _migrate(missing, storage) == {"migrated": False, "reason": "no_store"}
    assert _migrate(empty, storage) == {"migrated": False, "reason": "no_store"}
    assert opened == []
    assert not missing.exists() and not any(empty.iterdir())
    assert storage.meta_get("reference_chroma_import") is None


def test_a_store_without_the_collection_is_marked_done_with_no_chunks(
        tmp_path, storage):
    import chromadb

    store = tmp_path / "chromadb"
    client = chromadb.PersistentClient(path=str(store))
    client.get_or_create_collection("some_other_collection")
    client.close()

    result = _migrate(store, storage)

    assert result["migrated"] is True and result["chunks"] == 0
    assert storage.meta_get("reference_chroma_import")["chunks"] == 0
    assert _bank(storage).size == 0


def test_vectors_of_another_dimension_are_re_embedded(tmp_path, storage):
    """Documents ingested before the v25 backbone swap carry 384-d vectors,
    which the vector(1024) column cannot hold: their text is embedded again
    with the bank's current model."""
    store = tmp_path / "chromadb"
    docs = dict(list(CORPUS.items())[:3])
    _legacy_ingest(store, docs, compact=False, dim=384)
    emb = _Embedder()

    result = _migrate(store, storage, emb)

    assert result["reembedded"] == result["inserted"] == 3
    bank = _bank(storage)
    for text in docs.values():
        hit = bank.retrieve(emb.row(text), top_k=1)
        assert [e.text for e in hit.entries] == [text]
        assert hit.scores[0] == pytest.approx(1.0, abs=1e-5)


def test_an_unreadable_store_leaves_the_bank_unmarked(tmp_path, storage):
    store = tmp_path / "chromadb"
    store.mkdir()
    (store / "chroma.sqlite3").write_bytes(b"not a database")

    with pytest.raises(Exception):
        _migrate(store, storage)

    assert storage.meta_get("reference_chroma_import") is None
    assert _bank(storage).size == 0


# ── the service: Postgres tiers and file mode ────────────────────────────


class _ServiceEmbedder(_Embedder):
    backend = "torch"


def _service(tmp_path, monkeypatch, url=None):
    from pseudolife_memory.service import MemoryService

    monkeypatch.setattr("pseudolife_memory.service.EmbeddingPipeline",
                        lambda config: _ServiceEmbedder())
    if url is None:
        monkeypatch.delenv("PSEUDOLIFE_MCP_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", url)
    svc = MemoryService(data_dir=tmp_path)
    svc._ensure_init()  # noqa: SLF001 — the start path under test
    return svc


def _close(svc) -> None:
    if svc._storage is not None:  # noqa: SLF001
        svc._storage.close()  # noqa: SLF001


def test_service_start_migrates_an_existing_store_and_serves_it(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """The path every Postgres tier takes (the Docker daemon, and the lite
    tier's embedded Postgres, both start MemoryService on a DSN)."""
    store = tmp_path / "chromadb"
    _legacy_ingest(store, CORPUS, compact=True)
    on_disk = _tree_digest(store)

    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        found = svc.search_documents(QUERIES[0], top_k=3)
        assert found["count"] == 3
        assert {e["source"] for e in found["entries"]} <= set(CORPUS)
        assert svc.stats()["reference_document_count"] == len(CORPUS)
    finally:
        _close(svc)
    assert _tree_digest(store) == on_disk


def test_service_start_opens_no_chromadb_client(tmp_path, monkeypatch):
    """A bank with no legacy store (and every file-mode start) never opens a
    chromadb client: chromadb keeps each opened client's System (about 19
    threads and ~3 MB) for the life of the process."""
    opened = _count_opened_clients(monkeypatch)
    svc = _service(tmp_path, monkeypatch)
    assert opened == []
    assert not (tmp_path / "chromadb").exists()


def test_file_mode_has_no_document_store(tmp_path, monkeypatch):
    """Documents live in the Postgres bank, like the cortex: a file-mode
    service (no DSN, no lite tier) serves no reference hits and refuses
    ingest with the reason."""
    svc = _service(tmp_path, monkeypatch)
    doc = tmp_path / "doc.txt"
    doc.write_text("alpha", encoding="utf-8")

    assert svc._reference is None  # noqa: SLF001
    assert svc.search_documents("alpha") == {"count": 0, "entries": []}
    with pytest.raises(RuntimeError, match="Postgres"):
        svc.ingest_document(str(doc))


def test_an_old_backup_restores_with_its_documents(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    """A pre-v57 backup carries the documents only in its state archive's
    chromadb directory, and its dump has no reference_chunks table. Restoring
    both and starting the daemon brings the documents into the bank."""
    from pseudolife_memory.backup_cli import _archive_state

    old_data = tmp_path / "old-host"
    (old_data / "memory_state").mkdir(parents=True)
    _legacy_ingest(old_data / "chromadb", CORPUS, compact=True)
    archive = _archive_state(old_data, tmp_path, "20261001-000000")

    # The restored dump: a v56 bank, which never had the tables or marker.
    pg_conn.execute("DROP TABLE reference_chunks")
    pg_conn.execute("DROP TABLE cms_counters")
    pg_conn.execute("UPDATE meta SET value = '56'::jsonb "
                    "WHERE key = 'schema_version'")
    pg_conn.commit()
    restored = tmp_path / "restored"
    restored.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(restored, filter="data")

    svc = _service(restored, monkeypatch, pg_url)
    try:
        assert svc._reference.size == len(CORPUS)  # noqa: SLF001
        hit = svc.search_documents(next(iter(CORPUS.values())), top_k=1)
        assert hit["entries"][0]["source"] == next(iter(CORPUS))
        assert svc._storage.meta_get("schema_version") == 57  # noqa: SLF001
    finally:
        _close(svc)


# ── hardening: NUL characters, resumable import, failed reads ────────────


def test_nul_characters_are_stored_as_replacement_characters(storage):
    """PostgreSQL text and jsonb refuse NUL, which a PDF's extracted text and
    a chromadb store can both carry: one such chunk used to fail the whole
    write. It is written as U+FFFD, one character for one, in the text, the
    source and nested metadata strings; the chunk id still comes from the
    original text. A backslash-x-zero spelled out in the text is not NUL and
    stays as it is."""
    row = {"id": "c1", "source": "bad\x00name.pdf", "chunk_index": 0,
           "text": "page one\x00page two, literal \\x00 kept",
           "embedding": _Embedder.row("nul"),
           "metadata": {"source": "bad\x00name.pdf", "notes": ["a\x00b"]},
           "ingested_at": 1.0}

    assert storage.upsert_reference_chunks([row]) == 1

    source, text, metadata = storage.conn.execute(
        "SELECT source, text, metadata FROM reference_chunks").fetchone()
    assert source == "bad\ufffdname.pdf"
    assert text == "page one\ufffdpage two, literal \\x00 kept"
    assert metadata == {"source": "bad\ufffdname.pdf", "notes": ["a\ufffdb"]}


def test_an_ingest_of_text_with_nul_characters_succeeds(storage):
    bank, emb = _bank(storage), _Embedder()
    text = "header\x00body of the document"

    assert bank.ingest_text(text, source="doc.pdf", embedder=emb) == {
        "chunks_total": 1, "chunks_stored": 1}
    stored = storage.conn.execute("SELECT id, text FROM reference_chunks").fetchone()
    assert stored == (rb.chunk_id("doc.pdf", 0, text),
                      "header\ufffdbody of the document")


def test_an_import_cut_short_resumes_without_embedding_anything_twice(
        tmp_path, storage, monkeypatch):
    """Each page commits on its own and the marker comes last, so a start
    that dies part-way leaves the pages it wrote, no marker, and a next start
    that embeds only the chunks still missing."""
    from pseudolife_memory.storage import migrate_chroma

    monkeypatch.setattr(migrate_chroma, "_PAGE", 2)
    store = tmp_path / "chromadb"
    docs = dict(list(CORPUS.items())[:5])
    _legacy_ingest(store, docs, compact=False, dim=384)  # every row re-embeds
    real = storage.upsert_reference_chunks
    pages = []

    def dies_on_the_second_page(rows, **kwargs):
        pages.append(len(rows))
        if len(pages) == 2:
            raise RuntimeError("daemon stopped mid-import")
        return real(rows, **kwargs)

    monkeypatch.setattr(storage, "upsert_reference_chunks", dies_on_the_second_page)
    first = _Embedder()
    with pytest.raises(RuntimeError, match="mid-import"):
        _migrate(store, storage, first)
    assert storage.meta_get("reference_chroma_import") is None
    assert _bank(storage).size == 2

    monkeypatch.setattr(storage, "upsert_reference_chunks", real)
    second = _Embedder()
    texts = []
    real_encode = second.encode
    second.encode = lambda batch: texts.extend(batch) or real_encode(batch)
    result = _migrate(store, storage, second)

    assert result == {"migrated": True, "chunks": 5, "inserted": 3,
                      "reembedded": 3, "resumed": 2, "skipped": 0}
    assert len(texts) == 3
    assert _bank(storage).size == 5
    assert storage.meta_get("reference_chroma_import")["status"] == "done"


def test_an_import_is_marked_only_once_every_chunk_is_in_the_bank(
        tmp_path, storage, monkeypatch):
    store = tmp_path / "chromadb"
    _legacy_ingest(store, dict(list(CORPUS.items())[:3]), compact=False)
    monkeypatch.setattr(storage, "upsert_reference_chunks",
                        lambda rows, **kwargs: len(rows))  # writes nothing

    with pytest.raises(RuntimeError, match="not in the bank"):
        _migrate(store, storage)

    assert storage.meta_get("reference_chroma_import") is None


def test_the_scratch_copy_is_cleared_before_and_after_an_import(
        tmp_path, storage):
    """A scratch copy an earlier attempt could not remove (Windows keeps a
    file chromadb held open) is cleared by the next attempt, which leaves
    none of its own behind."""
    from pseudolife_memory.storage import migrate_chroma

    store = tmp_path / "chromadb"
    _legacy_ingest(store, dict(list(CORPUS.items())[:2]), compact=False)
    leftover = tmp_path / migrate_chroma.SCRATCH_NAME
    (leftover / "chromadb").mkdir(parents=True)
    (leftover / "chromadb" / "stale.bin").write_bytes(b"x")

    assert _migrate(store, storage)["inserted"] == 2

    assert not leftover.exists()


def test_a_failed_lenient_read_leaves_an_open_transaction_usable(storage):
    """The memory-search pool swallows a failed document query. Inside an
    open transaction that failure must not abort the caller's transaction,
    so the query runs under a savepoint there."""
    bank = _bank(storage)
    bank.ingest_text("alpha beta gamma", source="doc.txt", embedder=_Embedder())
    with storage.transaction():
        # A vector of the wrong dimension makes the SQL itself fail (once a
        # row is there to compare it with).
        assert bank.retrieve(torch.ones(8)).entries == []
        assert storage.conn.execute("SELECT 1").fetchone() == (1,)


def test_a_postgres_start_with_no_store_opens_nothing_and_marks_nothing(
        tmp_path, pg_url, pg_conn, monkeypatch):  # noqa: F811
    opened = _count_opened_clients(monkeypatch)
    svc = _service(tmp_path, monkeypatch, pg_url)
    try:
        assert opened == []
        assert svc._reference is not None  # noqa: SLF001
        assert svc._storage.meta_get("reference_chroma_import") is None  # noqa: SLF001
        assert svc.search_documents("anything") == {"count": 0, "entries": []}
    finally:
        _close(svc)
