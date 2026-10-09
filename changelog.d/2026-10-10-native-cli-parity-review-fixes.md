### Fixed (2026-10-10 — native export, import, connect and test-login parity fixes)

- The experimental native `export`, `import` and `connect` now spell every
  float as CPython's `repr` does, including on an exact decimal tie at the
  shortest length, where Python keeps the even digit (`562949953421312.25` is
  `562949953421312.2`; Rust's shortest formatting gave `...3`). Export wrote
  such values into the archive, import re-encoded them into `jsonb`, and
  connect rewrote them in a client config. Both now pass the same CPython
  tables the pairing leaf does.
- The native `test-login create` stops at a report line that stdout refuses,
  as Python's flushed `print` does: before the login file or the role is
  touched it defers, and afterwards it exits 120 without running any later
  step. It used to ignore the error and go on to write the file and change
  the role.
- The native `connect` stops with exit 120 at a warning or failure line that
  stderr refuses, as Python's line-buffered stderr does, instead of going on
  to write client configs.
