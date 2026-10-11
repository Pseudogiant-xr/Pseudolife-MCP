### Fixed (2026-10-11 — Audit command compatibility)
- The native board audit command accepts argparse's joined, abbreviated and repeated options, prints matching help and argument errors, and formats paths as pathlib does.
- Archive open, decoding and JSON failures, including lazy UTF-8 read ordering, and output-file creation failures now retain Python's diagnostics and exit codes.
- Historical audit payloads retain Python's non-finite numbers, large integers, nesting and surrogate strings during export, verification and redaction.
