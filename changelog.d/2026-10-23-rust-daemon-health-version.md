### Fixed (2026-10-23 — the Rust daemon reports the release version, not 0.0.0)
- The Rust daemon reported its crate's placeholder version 0.0.0 on
  `/health`, so a shim connecting to it printed a version-mismatch notice
  telling the user to install release 0.0.0, which does not exist (the call
  still went through). The daemon crate now carries the release version, as
  the native shim's crate already did, and reports 0.19.0. Like a
  checkout-built Python daemon, a build from master between releases
  reports the last release's version. `tests/test_plugin_packaging.py` pins
  both native crates and their lock entries to `pyproject.toml`. A version
  cut moves `rust/daemon/Cargo.toml` with the rest (release-procedure
  skill), and the parity oracle's version-only release check accepts it.
