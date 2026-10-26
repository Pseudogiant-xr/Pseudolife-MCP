### Added (2026-10-26 — the Rust daemon ingests PDFs, with the Python daemon's text)
- `document_ingest` on the Rust daemon refused every `.pdf`. It now reads a
  PDF's text with PDFium, the pinned build the Python daemon's pypdfium2
  bundles, through pypdfium2's own calls, so both daemons store the same
  text, chunks and chunk ids for a PDF. The text is compared byte for byte
  with the Python daemon's own output for every file of a committed corpus
  (synthetic, browser-printed in several scripts, and hostile files) and
  in the live differential.
- A PDF PDFium cannot open is refused as the Python daemon refuses it
  (`Failed to read PDF: Failed to load document (PDFium: Data format
  error).`, a wrong password, a page-tree cycle's `Failed to load page.`),
  after the same file-kind and size checks every document gets.
- When the PDFium library is missing or does not match its pin, PDF ingest
  is refused with `pdf_engine_unavailable`, naming the cause and the one
  fix; the Python daemon reads such a PDF with pypdf instead. Text,
  Markdown and HTML ingest never need the library.
- One declared difference: a page whose text starts or ends with about a
  thousand characters PDFium leaves out fails on the Python daemon
  (pypdfium2 trims them recursively and runs out of stack); the Rust
  daemon reads the page.
- CI provisions the pinned library for the tests, which fail without it.
