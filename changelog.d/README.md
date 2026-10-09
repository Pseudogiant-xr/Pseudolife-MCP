# Changelog fragments

Add your change's CHANGELOG entry here as one file, not by editing
`CHANGELOG.md`. Parallel pull requests then stop conflicting on the same lines.

- **Name:** `YYYY-MM-DD-short-slug.md`, using the date the change merges or
  was written, in lowercase with hyphens. For example,
  `2026-10-09-changelog-fragments.md`.
- **Content:** exactly the entry as it would sit under `## [Unreleased]`. The
  first line is `### <Added|Changed|Deprecated|Removed|Fixed|Security>
  (YYYY-MM-DD — title)`, with the same date as the file name. One or more
  `- ` bullets follow. Write one entry per file, with no `## ` release
  headers.
- **Release:** the version cut runs `python ops/assemble_changelog.py`. It
  folds every fragment into `## [Unreleased]` newest first and deletes the
  files (see the `release-procedure` skill).

`tests/test_changelog_fragments.py` checks every fragment's name and heading.
Tests that look up claims or the current schema version in the CHANGELOG
read `CHANGELOG.md` plus these fragments, so a claim is found before and
after the release folds it in.
