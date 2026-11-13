### Added (2026-11-14 — the native client restores a Docker bank)

- `pseudolife-mcp restore` in the native (Rust) client now replaces a
  Docker bank end to end: it checks the dump and state archive, asks at a
  terminal (or takes `--yes`), stops the daemon, takes and verifies a
  safety backup, stages and replays the dump, replaces the state volume
  and starts the daemon again. Before, every confirmed run stopped at the
  first change with "deferred in this candidate", so a native client could
  not restore a backup at all.
- Every failure or Ctrl-C stops at that step and names the bank's state and
  the command to run next. Once the database drop has begun the daemon is
  left stopped, the safety backup's recovery command is printed and also
  written beside the safety pair (`pseudolife_restore-<timestamp>.txt`),
  and a `restore-in-progress-<bank>.json` journal in the data directory
  keeps a later run naming the first safety backup rather than one of a
  half-restored bank.
  This covers the restore items of #1497 (an interrupt after the drop now
  prints the recovery command; a corrupt deflate stream is refused) and of
  #1507 (a wrong-typed `docker inspect` field is the `cannot inspect`
  refusal; a Ctrl-C at the confirmation prompt ends the run with nothing
  changed).
- The state archive check is never more permissive than the `tar` that
  extracts it: POSIX extended headers are now read, while a sparse member,
  a global extended header that sets a name or size, a damaged header
  anywhere and data after the end marker are refused with their reason.
- A lite bank is refused with a line naming the Docker install; restore
  runs `docker` only from absolute `PATH` entries or `PSEUDOLIFE_DOCKER`'s
  full path; the restarted daemon gets up to ten minutes to report ready.
