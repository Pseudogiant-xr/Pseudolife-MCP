### Fixed (2026-10-30 — a stray file in the temp directory can no longer break `connect` or the install check)
- `pseudolife-mcp connect` verifies a new credential through a `python -c`
  child, and the client updater probes how the package is installed the
  same way. Both ran from the shared temp directory to keep a source
  checkout off `sys.path`, but `python -c` puts its working directory first
  there, so any file in the temp directory named like a module they import
  ran in its place: a stray `mcp.py` made every `connect` verification fail
  with "the handshake exited 1", and a stray `pseudolife_memory` folder could
  make the install check misreport an install. Each now runs from a fresh,
  empty directory of its own.
