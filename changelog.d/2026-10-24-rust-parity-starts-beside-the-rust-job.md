### Changed (2026-10-24 — Rust parity shards start beside the Rust job, and the longest Windows shards are split)

- The Rust workflow's parity shards no longer wait for the whole `Rust` job
  (format, clippy, nextest, the override builds). They read nothing from it
  but the debug bind fixture, which a new `Fixtures / <os>` job now builds
  beside the candidate, reusing the `Rust` job's dependency cache read-only.
  The `Parity / <os>` gate still needs `Rust`, so a format, clippy or
  nextest failure still fails it; the shards now run even when `Rust`
  fails. On batch run 40047950017 every shard waited until minute 18 for
  `Rust / windows-latest`, though the Windows candidate was done at minute 9.
- The same job builds the daemon storage test executable once per OS
  (`rust/daemon/harness/schema_ci.py --build-only`), which each `cli-daemon`
  shard used to compile for itself.
- The three longest Windows shards (23-26 minutes) are split in two, so no
  Windows shard is planned past about 15 minutes: `cli` into `cli` and
  `cli-store` (dream cursor, static, graph store, background and principal
  steps); `cli-daemon` into `cli-daemon` (schema parity and the first 12
  resident startup cases) and `cli-daemon-2` (the other cases, the startup
  mutants, the clock retry and the writer guard;
  `rust/daemon/harness/startup_ci.py --part`); `w1c-connection` into
  `w1c-connection` (test_login, move, tunnel) and `w1c-connect` (connect,
  maintainer). Every step still runs once per OS, and both new suites join
  the pull-request floor, so every pull request runs what it ran before.
