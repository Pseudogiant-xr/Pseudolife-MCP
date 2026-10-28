### Changed (2026-10-28 — Rust port pull requests stop colliding on the divergence ledger and the harness's declarations)
- The Rust daemon's declared-divergence ledger is now one file per entry
  under `rust/daemon/divergences/` (a directory per section), instead of
  one `divergences.md` every port pull request edited. Adding or closing a
  divergence adds or deletes its own file. `divergences.md` is a short
  pointer, and `python rust/daemon/split_divergences.py` converts a
  branch's single-file ledger when it merges master forward, applying only
  that branch's own changes; `--render` prints the whole ledger, `--check`
  verifies the layout.
- The differential harness (`rust/daemon/harness/run.py`) reads its
  declared route cases and Python-only `/health` keys from one file each
  under `harness/declared/`, and loads new scenario families, with any
  per-route response scrub, from one module each under
  `harness/run_scenarios/`. The scenarios, their cases, order and declared
  markers are unchanged.
