### Fixed (2026-11-13 — restore and update no longer take NUL for a person at a terminal on Windows)

- On Windows, `pseudolife-mcp restore` without `--yes`, run with stdin and
  stdout redirected to `NUL`, now refuses with `rerun with --yes to
  confirm` (exit 2) instead of printing its question and reading an empty
  answer. `update`'s optional questions (the test suite's login, the
  maintainer passkeys) skip themselves the same way. Only a real console
  counts as a terminal now; the native restore makes the same check.
