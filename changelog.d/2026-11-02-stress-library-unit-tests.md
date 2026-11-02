### Added (2026-11-02 — Stress library unit tests on hosted runners)
- Rust stress selections accept `cargo:<package>/lib[::<exact test path>]`, archive library test binaries once, and check each selected test's JUnit result and timing under nextest's package-only binary ID.
- `lib` is reserved for library unit tests; `test/lib` selects an integration test target literally named `lib`, while `bin/lib` is refused.
