| Item | Python | Rust | Why / owner |
|---|---|---|---|
| PDFium in the daemon process (shared, not a divergence) | pypdfium2 loads PDFium into the daemon | pdfium-render loads the same build into the daemon | both daemons parse an untrusted PDF in process with one engine: a memory-safety bug a crafted file reaches would end either daemon. Subprocess isolation was ruled out by the maintainer (2026-10-26); the pin moves with PDFium releases (both pins together), and the 64 MiB read bound applies first |
