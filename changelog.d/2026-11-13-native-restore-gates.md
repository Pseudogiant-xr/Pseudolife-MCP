### Added (2026-11-13 — native restore refusals before Docker)

- The native CLI now answers `pseudolife-mcp restore`'s checks that come
  before any Docker command, matching the Python command's exit code and
  messages: argparse's help and usage errors at 80 columns, an unreadable or
  incomplete dump, a state archive that is unreadable or holds unsafe paths
  or links, and the refusal without `--yes` when no terminal can answer.
  Every argument list that passes those checks, the interactive prompt, and
  archives with PAX, GNU long-name or sparse members still defer by name
  before the update lock, a Docker command or any write.
