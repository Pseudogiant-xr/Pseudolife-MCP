### Changed (2026-10-23 — ingested documents live in the bank, schema v57)

- **Schema v57:** documents ingested with `document_ingest` are stored in the
  Postgres bank's new `reference_chunks` table instead of a chromadb store
  under `<data_dir>/chromadb`. Every backup (`ops/backup.*`, `pseudolife-mcp
  backup`) and every logical transfer (`pseudolife-mcp export` / `import`)
  now carries them inside the bank. A row keeps what chromadb held for a
  chunk: the same chunk id, the text, the vector and the metadata dict.
- `document_search` and the reference pool of `memory_search` serve the same
  shape as before (the same scores from `1 − cosine distance`, the same
  fields). Search is exact (`ORDER BY embedding <=> q`, no approximate
  index), and chunks at equal distance now come back in chunk-id order
  instead of whatever order chromadb's index held, so equal inputs always
  serve equal results.
- **Existing documents migrate on their own.** The first start on a v57 bank
  that finds a chromadb store copies every chunk in and marks the bank
  (`reference_chroma_import` in `meta`) so the store is not read again. The
  copy is idempotent and resumable: each page of chunks commits on its own,
  the marker is written only after every chunk read is checked to be in the
  bank, and a start cut short carries on where it stopped without embedding
  anything twice. It works whether chromadb has compacted its vectors into
  index files or not, reads a scratch copy so the store on disk stays
  byte-identical, and never deletes the store. Vectors of another dimension
  (documents from before the v25 embedding swap) are embedded again. A
  restored pre-v57 backup, whose documents exist only in its state archive's
  `chromadb/` directory, imports them at its first start. Nothing to run:
  install and `pseudolife-mcp update` reach it by starting the daemon.
- A document whose text, source or metadata contains a NUL character (some
  PDFs' extracted text does) is stored with U+FFFD in its place, because
  PostgreSQL refuses NUL; one such chunk no longer fails a whole ingest or
  import. A failed document query inside `memory_search` serves no
  reference hits instead of failing the search.
- The same bump adds `cms_counters`, one row for the counters
  `memory_state/weights.pt` held (see the counters entry); it stays out of
  logical transfers, like the file.
- `chromadb` stays a dependency for now, used only to read a pre-v57 store
  once; it can go after a release has carried the import.
