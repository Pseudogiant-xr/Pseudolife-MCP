### Changed (2026-10-13 — native shim reports the 0.18.0 package version)

- The native Rust shim candidate's crate version and `PACKAGE_VERSION` move
  to 0.18.0, so its `version` output and startup version check match the
  released Python package again. The parity oracle compares the two; this
  follows the release, as `rust/PORTING.md` orders Python changes first.
