### Added (2026-10-09 — native move refusals)

- The experimental native candidate now answers `pseudolife-mcp move`'s
  refusals that come before any effect, matching the Python command's exit
  code and messages: an invalid `--target-url`, an empty `--to` or one
  holding whitespace or a control character, a `--target-checkout` holding
  a line break, and argparse's help and missing-`--to` usage at 80 columns.
  Every argument list that passes those checks, `--dry-run` included, still
  defers by name before the candidate reads the daemon, runs docker or ssh,
  or writes a file.
