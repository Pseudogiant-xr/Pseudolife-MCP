### Added (2026-10-09 — CLI differential harness; native wait-mail closed)

- `rust/cli_harness` runs the Python CLI and the Rust CLI on the same case in
  the same disposable home and compares exit code, both streams, every file
  written, fixture-daemon requests and database rows, exiting non-zero on any
  difference. Comparison is exact apart from named, validated rules; a mutant
  control rebuilds the Rust leaf with deliberate breaks and requires each to
  be caught, and per-OS goldens replay without the oracle. The Rust workflow's
  Parity job runs it live on Linux and Windows.
- The native `wait-mail` matches the Python command on all canonical cases
  (40 on Windows, 42 on Linux). The harness found, and this fixes, digest
  paths in diagnostics spelled unlike pathlib: mixed separators on Windows,
  a forward-slash `PSEUDOLIFE_DIGEST_DIR`, a leading `./` and POSIX `//`. A
  test-only metadata seam proves the stat refusal on Windows. CLI-MAIL is
  `ported-with-substitution`.
