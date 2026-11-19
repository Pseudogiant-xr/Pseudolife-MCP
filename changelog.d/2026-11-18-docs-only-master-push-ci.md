### Changed (2026-11-18 — Fast CI for documentation pushes)

- Documentation-only pull requests and direct master pushes run the doc guards and tests that name affected documents. Required checks report the scoped skips. Rust Scope reports producer census drift on a docs push, and the nightly runs the skipped suites.
