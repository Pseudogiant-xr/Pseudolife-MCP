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
  Nothing here deletes or writes it. The copy is refused, before it starts,
  when the volume lacks room for it. Each attempt copies into its own
  subdirectory of one fixed scratch directory, which is removed before and
  after every attempt, so a copy Windows would not let go of (chromadb can
  hold its files open) is cleared by a later attempt instead of piling up or
  blocking the next copy.
* **Idempotent and resumable.** Every chunk id is listed first; the chunks
  are then read and written page by page, by id, so no order chromadb gives
  an offset can skip or repeat one, and no more than a page is held in
  memory. Rows keep their chromadb ids and are inserted with
  ``ON CONFLICT DO NOTHING``, so a rerun adds nothing and never undoes a
  later ingest. Each page commits on its own; the marker is written last,
  after every listed id is checked to be in the bank. An import cut short
  leaves no marker, and the next start skips the chunks already in the bank
  (embedding none of them again) and carries on with the rest.
* **Once.** With the marker set, the store is never opened again (an open
  costs chromadb's per-process System cache: about 19 threads and ~3 MB,
  released here by closing the client once the read ends). The marker is bank-local: a logical
  transfer does not carry it (``transfer_cli._META_SKIP_KEYS``), so a target
  host's own legacy store is still imported.
* **Vectors of another dimension** (documents ingested before the v25
  backbone swap carry 384-d vectors) are embedded again from their text with
  the bank's current model, the way ingest embeds them. That runs while the
  daemon starts, so each page logs its progress.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATION_META_KEY = "reference_chroma_import"
# Chunks per chromadb ``get`` while reading the store, and per committed page
# while writing the bank.
_PAGE = 256
# The scratch directory's name, beside the store.
SCRATCH_NAME = ".chromadb-import-copy"
# Free space the copy needs: twice the store (the copy, and room for chromadb
# to rewrite an older store's files as it opens it) plus this much.
_COPY_HEADROOM_BYTES = 256 * 1024 * 1024


def _store_present(store: Path) -> bool:
    try:
        return (store / "chroma.sqlite3").is_file()
    except OSError:
        return False


def _remove_scratch(scratch: Path) -> None:
    shutil.rmtree(scratch, ignore_errors=True)
    if scratch.exists():
        logger.warning(
            "could not remove the chromadb import's scratch copy at %s; a "
            "later start removes it, or delete it by hand", scratch)


def _tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@contextmanager
def _open_copy(store: Path, collection_name: str):
    """The store's collection, opened on a scratch copy; None when the store
    holds no such collection."""
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError(
            f"a pre-v57 chromadb document store is at {store}, and reading it "
            "needs chromadb, which is not installed: `pip install chromadb`, "
            "then restart to import its documents") from exc

    scratch = store.parent / SCRATCH_NAME
    _remove_scratch(scratch)
    need = 2 * _tree_bytes(store) + _COPY_HEADROOM_BYTES
    free = shutil.disk_usage(store.parent).free
    if free < need:
        raise RuntimeError(
            f"importing the chromadb store at {store} needs a scratch copy of "
            f"about {need // 2**20} MB beside it and only {free // 2**20} MB "
            "is free; free some space on that volume and restart. Nothing "
            "was copied or written.")
    copy = scratch / uuid.uuid4().hex / "chromadb"
    try:
        shutil.copytree(store, copy)
        client = chromadb.PersistentClient(path=str(copy))
        try:
            # Looked up by name rather than by catching get_collection's
            # error, whose class differs across chromadb releases (ValueError,
            # InvalidCollectionException, NotFoundError). list_collections
            # returns names in 0.6 and Collection objects before and after.
            names = {getattr(c, "name", c) for c in client.list_collections()}
            yield (client.get_collection(collection_name)
                   if collection_name in names else None)
        finally:
            # close() stops this path's cached System (threads, open
            # files), so the scratch copy can go. Not clear_system_cache():
            # it drops every chromadb System in the process without stopping
            # them. Releases without close() keep the System, and the copy,
            # until the process exits; the next attempt copies into a fresh
            # subdirectory and a later one removes the old.
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

    store = Path(store)
    marker = storage.meta_get(MIGRATION_META_KEY)
    if isinstance(marker, dict) and marker.get("status") == "done":
        # A finished import can leave its scratch copy behind (a chromadb
        # without close() holds it until the process exits), and no later
        # start opens the store again to clear it, so clear it here.
        scratch = store.parent / SCRATCH_NAME
        if scratch.exists():
            _remove_scratch(scratch)
        return {"migrated": False, "reason": "already_imported"}
    if not _store_present(store):
        return {"migrated": False, "reason": "no_store"}

    with _open_copy(store, collection_name) as collection:
        ids = [] if collection is None else list(
            collection.get(include=[])["ids"])
        listed = set(ids)
        textless: set[str] = set()
        inserted = reembedded = resumed = 0
        for start in range(0, len(ids), _PAGE):
            page_ids = ids[start:start + _PAGE]
            present = storage.reference_chunk_ids_present(page_ids)
            resumed += len(present)
            todo_ids = [i for i in page_ids if i not in present]
            if not todo_ids:
                continue
            page = collection.get(
                ids=todo_ids, include=["embeddings", "documents", "metadatas"])
            documents = page["documents"] or [None] * len(page["ids"])
            metadatas = page["metadatas"] or [None] * len(page["ids"])
            embeddings = page["embeddings"]
            rows: list[dict] = []
            for i, chunk in enumerate(page["ids"]):
                if documents[i] is None:
                    textless.add(chunk)
                    continue
                metadata = dict(metadatas[i] or {})
                rows.append({
                    "id": chunk,
                    "source": str(metadata.get("source", "")),
                    "chunk_index": _as_int(metadata.get("chunk_index", 0)),
                    "text": documents[i],
                    "embedding": None if embeddings is None else embeddings[i],
                    "metadata": metadata,
                    "ingested_at": _as_float(metadata.get("timestamp", 0.0)),
                })
            stale = [r for r in rows if r["embedding"] is None
                     or len(r["embedding"]) != _EXPECTED_EMBEDDING_DIM]
            for at in range(0, len(stale), INGEST_ENCODE_BATCH):
                batch = stale[at:at + INGEST_ENCODE_BATCH]
                vectors = embedder.encode([r["text"] for r in batch])
                if hasattr(vectors, "detach"):
                    vectors = vectors.detach().cpu().numpy()
                for row, vector in zip(batch, vectors):
                    row["embedding"] = vector
            reembedded += len(stale)
            inserted += storage.upsert_reference_chunks(rows, replace=False)
            logger.warning(
                "chromadb document import: %d of %d chunks done (%d embedded "
                "again so far)", min(start + _PAGE, len(ids)), len(ids),
                reembedded)

    # Verified before it is marked: every listed chunk with text is in the
    # bank, including any id the store listed but a page did not return.
    expected = sorted(listed - textless)
    missing = len(expected) - len(storage.reference_chunk_ids_present(expected))
    if missing:
        raise RuntimeError(
            f"{missing} of {len(expected)} chunks the chromadb store at "
            f"{store} lists are not in the bank after the import; not marking "
            "it done, so the next start tries again")
    chunks, skipped = len(expected), len(textless)
    storage.meta_set(MIGRATION_META_KEY, {
        "status": "done", "chunks": chunks, "inserted": inserted,
        "reembedded": reembedded, "resumed": resumed, "skipped": skipped,
        "at": time.time(),
    })
    if chunks or skipped:
        logger.warning(
            "imported %d document chunk(s) from the chromadb store at %s into "
            "the bank (%d new, %d already there, %d embedded again, %d "
            "without text skipped); the store is left in place and no longer "
            "read", chunks, store, inserted, resumed, reembedded, skipped)
    return {"migrated": True, "chunks": chunks, "inserted": inserted,
            "reembedded": reembedded, "resumed": resumed, "skipped": skipped}
