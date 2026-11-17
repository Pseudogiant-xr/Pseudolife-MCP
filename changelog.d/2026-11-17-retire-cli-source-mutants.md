### Removed (2026-11-17 — retire costly CLI source-mutant CI)
- Retired the weekly CLI source-mutant job, its Parity dependency and the source declarations and lists that CLI edits had to maintain. Rust unit and contract tests and committed golden replay remain; automatic proof that each planted source-line bug is detected is no longer run. Manual stress dispatches are unchanged.
