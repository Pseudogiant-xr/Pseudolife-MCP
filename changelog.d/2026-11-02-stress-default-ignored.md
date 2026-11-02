### Fixed (2026-11-02 — Stress reports exclude default-ignored Rust tests)
- Whole-binary Rust stress selections no longer fail solely because nextest excluded an `#[ignore]` test under its default policy. Runtime skips, unrecognized skip reasons and selections with no executed tests still fail.
