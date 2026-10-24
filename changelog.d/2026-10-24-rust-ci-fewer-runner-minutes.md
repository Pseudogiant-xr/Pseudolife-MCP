### Changed (2026-10-24 — Rust CI spends fewer runner minutes and slots per run)

- Parity shards install their Python dependencies with `pip install
  --no-compile`: the same packages, without byte-compiling every installed
  file (Python compiles what a shard imports, on first import). The install
  step took about 2.3 minutes per hosted Windows shard.
- Only the `eval` and `cli` shards, the two that run cargo or rustc, install
  the Rust toolchain.
- The candidate's harness build (the daemon with its mutant and harness
  features, and the graph and dream contract binaries) runs as its own
  `Candidate harness / <os>` job beside the candidate, so parity shards no
  longer wait for both builds in a row. It reuses the candidate's cached
  dependencies read-only. Its binaries ship as `rust-shim-harness-<os>`.
- Hosted Linux runs `h5-audit`, `h5-backup` and `h5-background` one after
  another in a single `h5-light` job (`rust/ci/coverage_plan.py`
  `COMPOSITES`), two fewer short jobs against the account's 20-job cap.
  Windows keeps them as separate shards, any pull request reaching one of
  the three also runs `h5-light`, and each suite keeps its own receipts.
- The non-gating `Parity live (non-gating) / windows-latest / cli-live` job
  no longer runs on master pushes, only on the nightly and manual CI
  (maintainer decision). It gated nothing and held a Windows runner for
  about 26 minutes per push.
