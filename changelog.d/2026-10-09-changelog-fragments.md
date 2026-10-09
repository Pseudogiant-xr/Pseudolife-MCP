### Changed (2026-10-09 — changelog fragments replace direct CHANGELOG.md edits)

- A change now records its CHANGELOG entry as a file in `changelog.d/`
  (`<YYYY-MM-DD>-<slug>.md`, one dated `###` subsection) instead of editing
  `CHANGELOG.md`, so parallel pull requests no longer conflict on the
  `[Unreleased]` section. `python ops/assemble_changelog.py` folds the
  fragments into `[Unreleased]`, newest first, at the release cut and
  deletes them; the release procedure runs it before the version cut.
- `tests/test_changelog_fragments.py` pins the fragment shape (file name,
  heading, date agreement, at least one bullet) and the assembler. The
  schema-version and evidence guards read pending fragments beside
  `CHANGELOG.md`, and the CI scope job treats a fragment like a CHANGELOG
  edit when deciding whether a Rust-only change can skip the Python suite.
