### Added (2026-11-13 — Native conductor-watch help, stop and setup refusals)

- The Rust CLI answers `pseudolife-mcp conductor-watch` (and its deprecated
  name `delegate-watch`) up to the point where the watcher would start, byte
  for byte as the Python command does: `--help` and its argument errors,
  `--stop`, the refusals for a missing or unreadable owner, and a start that
  finds a watcher already running on its state, with or without
  `--detach`. Starting the watch itself is not native yet: the Rust CLI
  reports it as deferred, and `pseudolife-mcp conductor-watch` keeps running
  the Python command.
