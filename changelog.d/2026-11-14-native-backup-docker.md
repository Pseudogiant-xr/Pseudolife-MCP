### Added (2026-11-14 — the native client backs up a Docker bank)

- `pseudolife-mcp backup` in the native (Rust) client now backs up a
  Docker bank, where it used to stop with "mode 'backup' is deferred in
  this candidate": it dumps the database through its PostgreSQL container
  and archives the daemon's `/data` through a read-only helper while the
  daemon keeps serving. It shares restore's verified backup: each file is
  read back before it is renamed, so the printed pair exists and is
  complete, and a failure or Ctrl-C leaves no file under a backup name.
- Rotation (`--keep-days`, default 7) removes only this command's exactly
  named files, keeps the newest three of each kind, and never touches a
  file a restore journal names (the state archive a failed restore would
  finish with included). Restore's safety pair is now named
  `pseudolife_safety_memory-…` and `pseudolife_safety_state-…`, which
  rotation never matches; a pair an earlier native restore wrote under the
  backup names is kept by its `pseudolife_restore-<timestamp>.txt` note,
  while one the Python restore wrote has no note and rotates like a backup,
  as it did under the Python backup. A restore journal that cannot be read
  stops rotation for that run, with a warning.
- A lite bank is refused with a line naming the Docker install; an `--out`
  inside the daemon's `/data` volume and a missing database are refused
  before anything is written. A `tar` exit 1 is reported as its likely
  cause (a file in `/data` changed while it was read, or Docker failed to
  run the helper). The file tier's refusals now name which cause they
  found: a backup directory inside the data directory, a path that cannot
  be resolved, or an entry the archive cannot write.
