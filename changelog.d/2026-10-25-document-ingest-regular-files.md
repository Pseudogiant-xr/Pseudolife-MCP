### Fixed (2026-10-25 — document_ingest no longer hangs on a FIFO or reads a file of any size)
- `document_ingest` checked only that its path existed, then read the whole
  file: a FIFO blocked the call, and every other call waiting on the
  daemon's lock, until something wrote to it; a device read without end;
  and a file of any size was read into memory at once. It now reads only a
  regular file of at most 64 MiB (symlinks to one are fine). A directory,
  FIFO, socket or device is refused as `not_a_regular_file`, naming what
  the path is, and a larger file as `document_too_large` with its size and
  the cap, before anything is read. The file is then opened without
  blocking, checked again on the open handle and read at most one byte past
  the cap, so a file that grows meanwhile, or a `/proc` entry that reports
  no size, is refused instead of read without end. 64 MiB is an estimate
  that covers a 1,000-page manual as a PDF with figures; a text file that
  size would take about 16 hours to embed on a CPU.
- The Python and Rust daemons refuse the same paths with the same errors on
  Linux (macOS takes the same calls, untested). On Windows a directory was an internal error before; it
  is now the same `not_a_regular_file` refusal. A Windows device name such
  as `NUL` is read by neither: Python refuses it as a character device, the
  Rust daemon answers an internal error (`rust/daemon/divergences.md`,
  DOC-WINDOWS-DEVICES). PDFs are read through the same bounded read.
