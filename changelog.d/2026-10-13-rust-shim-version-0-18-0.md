### Changed (2026-10-13 — native shim reports the 0.18.0 package version)

- The native Rust shim candidate's crate version and `PACKAGE_VERSION` move
  to 0.18.0, so its `version` output and startup version check match the
  released Python package again. The parity oracle compares the two; this
  follows the release, as `rust/PORTING.md` orders Python changes first.
- The Rust port's fixed Phase 1 oracle pin moves from the 0.17.0 commit
  `686b3f95` to the v0.18.0 release commit `1d444aa1`. The regenerated
  inventory counts 426 test files (the new `tests/test_changelog_fragments.py`
  is internal), and the startup contract's version-mismatch template carries
  0.18.0. Receipts captured at earlier oracles keep their identities.
