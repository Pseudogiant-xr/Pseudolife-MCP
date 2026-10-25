### Changed (2026-10-26 — PDF documents are read with PDFium by default)
- `document_ingest` read a PDF's text with pypdf unless the optional `pdf`
  extra was installed, so the same file could give different text (and
  chunks) from one install to another. pypdfium2 (Chromium's PDFium) is now
  a core dependency, locked to 5.14.0 in the daemon image, so every install
  reads PDFs with PDFium, as `document_parser` already preferred: its text
  keeps words whole where pypdf split them at kerning gaps, and it is about
  three times faster. pypdf stays the fallback when pypdfium2 cannot be
  imported.
- Only new ingests change: documents already in the bank keep their stored
  chunks. Ingesting the same PDF again behaves as any re-ingest of changed
  text does today (chunk ids hash the source, the position and the first
  100 characters): a chunk whose start reads the same is replaced, one whose
  start differs is stored beside the old chunk.
- `pip install pseudolife-mcp[pdf]` still works; the extra now adds nothing.
  Install and update need no extra step: the dependency arrives with the
  package and in the image.
- The lock's pypdfium2 bundles the PDFium build the Rust daemon will pin, so
  both daemons store byte-identical PDF text; the two pins move together.
