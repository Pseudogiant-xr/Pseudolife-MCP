### Changed (2026-10-26 — `lease check` runs the gated command, so a held lease can't be ignored)
- `pseudolife-mcp lease check NAME -- COMMAND [ARGS...]` now runs COMMAND
  only when the lease is free, and exits with the command's own status. When
  the lease is held, the command does not run: the usual report prints, one
  stderr line names the next step (retry once free, or queue with `lease run
  NAME -- ...`), and the exit is `75`, so a refusal is never read as the
  command's own `1`. A failed check runs nothing and exits `70`. Nothing is
  held while the command runs. Before this change, `check` with a command was
  a usage error, and a bare check followed by `;` (`lease check full-suite;
  cargo build`) started heavy work while another session's full suite held
  the machine.
- `check` without a command is unchanged. `check --json -- COMMAND` and an
  empty command after `--` are usage errors (`2`) that name the right form.
  `lease check --help` documents the gated form, with a multi-step example
  (`-- sh -c 'cargo clippy && cargo nextest run && cargo build'`) and its exit
  codes. The Rust shim's native `lease check` behaves the same.
