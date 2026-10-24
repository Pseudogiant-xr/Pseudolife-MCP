### Added (2026-10-24 — a hosted CI budget that a change must measure and stay inside)

- `rust/ci/ci_budget.json` records the measured minutes of every job a pull
  request can start in `rust.yml` and `ci.yml`, with limits on the job
  count, the summed runner minutes, the longest job and the critical path
  through `needs`, because every job shares the account's 20 hosted slots.
  `rust/ci/test_ci_budget.py` fails when the workflow and the budget
  disagree or a limit is exceeded, when a
  job waits on another it reads nothing from, when two jobs save one
  rust-cache key, and when a `rust.yml` install of the checkout or torch
  byte-compiles at install time. `python rust/ci/ci_budget.py measure
  --workflow <file> <run id>...` re-measures from hosted runs;
  `docs/dev/ci-budget.md` explains the rules and how to change a limit.
- The nightly source-mutant matrix, stress loops and the wait-mail capture
  now install with `pip install --no-compile` too, as the parity shards do.
- Only the `audit-0` source-mutant shard saves the shared mutant build's
  rust-cache; all 37 shards per OS used to race to save one key.
