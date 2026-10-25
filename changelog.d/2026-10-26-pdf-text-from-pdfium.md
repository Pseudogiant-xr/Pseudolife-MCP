### Changed (2026-10-26 — PDF documents are read with PDFium on every install)
- `document_ingest` read a PDF's text with pypdf unless the optional `pdf`
  extra was installed, so the same file could give different text (and
  chunks) from one install to another. pypdfium2 (Chromium's PDFium) is now
  a core dependency, pinned to exactly 5.14.0, so every install reads PDFs
  with the same PDFium build, the path `document_parser` already preferred.
  pypdf stays the fallback when pypdfium2 cannot be imported or its bundled
  library cannot be loaded.
- A PDF PDFium cannot open (not a PDF, truncated, a wrong password) is
  refused with `Failed to read PDF: <PDFium's reason>`, the same
  `ValueError` an unreadable PDF gave on the pypdf path, instead of an
  unexpected `PdfiumError`.
- Only new ingests change: documents already in the bank keep their stored
  chunks. Re-ingesting a PDF first read with pypdf stores PDFium's text as
  well, and in practice that is a second copy of nearly the whole document:
  chunk ids hash the source, the position and the first 100 characters, and
  a small early difference shifts every later chunk's start. No tool or
  command removes a document's chunks, so re-ingest only when the second
  copy is acceptable.
- `pip install pseudolife-mcp[pdf]` still works; the extra now adds nothing.
  Install and update need no extra step: the dependency arrives with the
  package and in the image. The exact pin trades away pip's freedom to
  co-install another pypdfium2 beside the package.
- This pypdfium2 bundles the PDFium build the Rust daemon will pin, so once
  its PDF ingest lands both daemons store byte-identical PDF text; the two
  pins move together, and PDFium security fixes arrive through a
  Pseudolife release.
