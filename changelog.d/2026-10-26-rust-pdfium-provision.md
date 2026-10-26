### Added (2026-10-26 — the Rust daemon can install the PDFium library it will read PDFs with)
- The Rust daemon refuses PDF ingest because it has no PDF reader. The
  maintainer chose PDFium (2026-10-26), the engine the Python daemon now
  uses on every install, so both daemons store the same text. This adds the
  pinned library and the command that installs it; the extraction itself
  follows in its own change.
- `rust/daemon/pdfium-pin.json` pins `bblanchon/pdfium-binaries`
  chromium/8076 for seven targets (Windows, Linux glibc and musl, macOS; x64
  and ARM): each archive's sha256 and the sha256 of the library inside it,
  every archive checked against its GitHub build attestation when pinned.
  `rust/daemon/pin_pdfium.py` writes it for a new release.
- `pseudolife-daemon provision pdfium` downloads this platform's archive,
  checks it against the pin, and installs only the library, `LICENSE` and
  `licenses/` into `<per-user data dir>/native/pdfium/<build>-<hash>/`
  (beside the lite tier's `native/postgresql/`), through the shared
  provisioner: all or nothing under an install lock, one line and no change
  on a rerun, and nothing installed when a hash differs. It needs no bank or
  configuration. `PSEUDOLIFE_DAEMON_PDFIUM_DIR` names another install root.
- The Rust image installs it at build time into `/opt/pdfium`. The
  installers and `pseudolife-mcp update` do not install the Rust daemon yet;
  when they do (the cutover's B11), they run the same command, tracked in
  `rust/CUTOVER.md`. Until then a host running the Rust daemon from a
  checkout runs `pseudolife-daemon provision pdfium` once.
- The PDFium build moves only together with the Python daemon's pypdfium2
  pin: tests on both sides fail when the two disagree.
