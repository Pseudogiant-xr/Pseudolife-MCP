"""Schema v57: copy a pre-v57 chromadb document store into the bank, once.

Until v57, ``document_ingest`` wrote chunks into a chromadb store under
``<data_dir>/chromadb``. The bank's ``reference_chunks`` table holds them now,
and a service that starts on a bank without this module's meta marker copies
any such store in:

* **Read through chromadb, from a copy.** chromadb keeps the newest vectors
  in chroma.sqlite3's ``embeddings_queue`` and moves them into HNSW segment
  files on compaction, so only chromadb itself reads a store reliably, in
  either state. Opening a store rewrites chroma.sqlite3 (measured 2026-10-09
  with chromadb 1.5.5), so the store is copied to a scratch directory beside
  it and the copy is opened: the user's store stays byte-identical on disk.
  Nothing here deletes or writes it. The scratch copy has one fixed path,
  removed before and after each attempt, so a copy Windows would not let go
  of (chromadb can hold its files open) is cleared by the next attempt
  instead of piling up.
* **Idempotent and resumable.** Rows keep their chromadb ids and are
  inserted with ``ON CONFLICT DO NOTHING``, so a rerun adds nothing and
  never undoes a later ingest. Each page commits on its own; the marker is
  written last, after every chunk read from the store is checked to be in
  the bank. An import cut short leaves no marker, and the next start skips
  the chunks already in the bank (embedding none of them again) and carries
  on with the rest.
* **Once.** With the marker set, the store is never opened again (an open
  costs chromadb's per-process System cache: about 19 threads and ~3 MB).
  The marker is bank-local: a logical transfer does not carry it
  (``transfer_cli._META_SKIP_KEYS``), so a target host's own legacy store is
  still imported.
* **Vectors of another dimension** (documents ingested before the v25
  backbone swap carry 384-d vectors) are embedded again from their text with
  the bank's current model, the way ingest embeds them.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATION_META_KEY = "reference_chroma_import"
# Rows per chromadb ``get`` while reading the store, and per committed page
# while writing the bank.
_PAGE = 256
# The scratch copy's directory name, beside the store.
SCRATCH_NAME = ".chromadb-import-copy"


def _store_present(store: Path) -> bool:
    try:
        return (store / "chroma.sqlite3").is_file()
    except OSError:
        return False


def _remove_scratch(scratch: Path) -> None:
    shutil.rmtree(scratch, ignore_errors=True)
    if scratch.exists():
        logger.warning(
            "could not remove the chromadb import's scratch copy at %s; the "
            "next import attempt removes it, or delete it by hand", scratch)


def _read_store(store: Path, collection_name: str) -> list[tuple] | None:
    """Every chunk in the store's collection as ``(id, document, metadata,
    embedding)``, read from a scratch copy; None when the store holds no
    such collection."""
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError(
            f"a pre-v57 chromadb document store is at {store}, and reading it "
            "needs chromadb, which is not installed: `pip install chromadb`, "
            "then restart to import its documents") from exc

    scratch = store.parent / SCRATCH_NAME
    _remove_scratch(scratch)
    try:
        copy = scratch / "chromadb"
        shutil.copytree(store, copy)
        client = chromadb.PersistentClient(path=str(copy))
        try:
            # Looked up by name rather than by catching get_collection's
            # error, whose class differs across chromadb releases (ValueError,
            # InvalidCollectionException, NotFoundError). list_collections
            # returns names in 0.6 and Collection objects before and after.
            names = {getattr(c, "name", c) for c in client.list_collections()}
            if collection_name not in names:
                return None
            collection = client.get_collection(collection_name)
            out: list[tuple] = []
            offset = 0
            while True:
                page = collection.get(
                    include=["embeddings", "documents", "metadatas"],
                    limit=_PAGE, offset=offset)
                ids = page["ids"]
                if not ids:
                    break
                embeddings = page["embeddings"]
                for i, chunk in enumerate(ids):
                    out.append((
                        chunk,
                        page["documents"][i] if page["documents"] else None,
                        (page["metadatas"][i] if page["metadatas"] else None) or {},
                        None if embeddings is None else embeddings[i],
                    ))
                offset += len(ids)
            return out
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                close()
    finally:
        _remove_scratch(scratch)


def import_legacy_store(store, collection_name: str, storage,
                        embedder) -> dict:
    """Copy the chromadb store at ``store`` into ``reference_chunks``.

    Returns ``{"migrated": False, "reason": "already_imported" | "no_store"}``
    when there is nothing to do, else ``{"migrated": True, "chunks": N,
    "inserted": M, "reembedded": R, "resumed": P, "skipped": S}``: N chunks
    read, M rows new to the bank, R embedded again for a dimension change, P
    already in the bank (an earlier attempt cut short, or an ingest since),
    S skipped for having no text. Raises when the store cannot be read
    (nothing is written then) or a page fails to write (the pages before it
    stay, and there is no marker).
    """
    from pseudolife_memory.memory.reference_bank import INGEST_ENCODE_BATCH
    from pseudolife_memory.storage.schema import _EXPECTED_EMBEDDING_DIM

    marker = storage.meta_get(MIGRATION_META_KEY)
    if isinstance(marker, dict) and marker.get("status") == "done":
        return {"migrated": False, "reason": "already_imported"}
    store = Path(store)
    if not _store_present(store):
        return {"migrated": False, "reason": "no_store"}

    rows: list[dict] = []
    skipped = 0
    for chunk, document, metadata, embedding in (
            _read_store(store, collection_name) or []):
        if document is None:
            skipped += 1
            continue
        rows.append({
            "id": chunk,
            "source": str(metadata.get("source", "")),
            "chunk_index": int(metadata.get("chunk_index", 0)),
            "text": document,
            "embedding": embedding,
            "metadata": dict(metadata),
            "ingested_at": float(metadata.get("timestamp", 0.0)),
        })

    inserted = reembedded = resumed = 0
    for start in range(0, len(rows), _PAGE):
        page = rows[start:start + _PAGE]
        present = storage.reference_chunk_ids_present([r["id"] for r in page])
        todo = [r for r in page if r["id"] not in present]
        resumed += len(page) - len(todo)
        stale = [r for r in todo if r["embedding"] is None
                 or len(r["embedding"]) != _EXPECTED_EMBEDDING_DIM]
        for at in range(0, len(stale), INGEST_ENCODE_BATCH):
            batch = stale[at:at + INGEST_ENCODE_BATCH]
            vectors = embedder.encode([r["text"] for r in batch])
            if hasattr(vectors, "detach"):
                vectors = vectors.detach().cpu().numpy()
            for row, vector in zip(batch, vectors):
                row["embedding"] = vector
        reembedded += len(stale)
        inserted += storage.upsert_reference_chunks(todo, replace=False)

    # Verified before it is marked: every chunk read is in the bank now.
    ids = [r["id"] for r in rows]
    missing = len(ids) - len(storage.reference_chunk_ids_present(ids))
    if missing:
        raise RuntimeError(
            f"{missing} of {len(ids)} chunks read from the chromadb store at "
            f"{store} are not in the bank after the import; not marking it "
            "done, so the next start tries again")
    storage.meta_set(MIGRATION_META_KEY, {
        "status": "done", "chunks": len(rows), "inserted": inserted,
        "reembedded": reembedded, "resumed": resumed, "skipped": skipped,
        "at": time.time(),
    })
    if rows or skipped:
        logger.warning(
            "imported %d document chunk(s) from the chromadb store at %s into "
            "the bank (%d new, %d already there, %d embedded again, %d "
            "without text skipped); the store is left in place and no longer "
            "read", len(rows), store, inserted, resumed, reembedded, skipped)
    return {"migrated": True, "chunks": len(rows), "inserted": inserted,
            "reembedded": reembedded, "resumed": resumed, "skipped": skipped}
