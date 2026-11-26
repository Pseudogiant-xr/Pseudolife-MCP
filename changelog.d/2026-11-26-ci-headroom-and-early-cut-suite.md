### Added (2026-11-26 — CI warns before a job outgrows its time limit)
- A daily `CI headroom` workflow (`rust/ci/ci_headroom.py`) compares every recent hosted job attempt with its `timeout-minutes` and the test job's pytest bound, and keeps each run at 80% or more of a bound in one self-closing issue with the run, runner CPU and share.
- A release cut's own pull request now runs the `test` job with the frozen test files, so the cut learns its suite time and failures before its batch does; other pull requests keep their fast lanes.
