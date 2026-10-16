### Changed (2026-10-16 — Console-only pull requests skip the Python suite lanes)
- CI's `scope` job now recognises a Console-only pull request: one that
  touches `frontend/` and whose every other changed file is the committed
  build under `pseudolife_memory/web/static/`, `CHANGELOG.md` or a
  `changelog.d/` fragment. Like a Rust-only PR, it skips the four Python
  suite lanes (`test`, `test-lite-linux`, `test-lite-windows`,
  `test-lite-macos`, about 35 minutes). `guards` runs, the `frontend` job
  gates the change, and a new `console-guards` job runs the Python tests that
  read the Console source or its bundle against embedded PostgreSQL. A bundle
  change with no `frontend/` source change, or any other file, still runs the
  full suite, as do every master push and the nightly run.
  `tests/test_ci_scope.py` runs the job's own classification script on
  representative file lists and keeps `console-guards`' file list complete.
