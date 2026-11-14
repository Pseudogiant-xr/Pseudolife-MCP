### Added (2026-11-14 — the native client backs up a Docker bank)

- `pseudolife-mcp backup` in the native (Rust) client now backs up a
  Docker bank, where it used to stop with "mode 'backup' is deferred in
  this candidate": it dumps the database through its PostgreSQL container
  and archives the daemon's `/data` through a read-only helper while the
  daemon keeps serving. It shares restore's verified backup: each file is
  read back before it is renamed, so the printed pair exists and is
  complete, and a failure or Ctrl-C leaves no file under a backup name.
- Rotation (`--keep-days`, default 7) works by pair: a dump and the state
  archive with the same timestamp are kept or removed together, the newest
  three pairs always stay, and a file whose partner is missing is never
  removed but listed as `kept unpaired:`. It removes only this command's
  exactly named files and never a file a restore journal names (the state
  archive a failed restore would finish with included). Restore's safety
  pair is now named `pseudolife_safety_memory-…` and
  `pseudolife_safety_state-…`, which rotation never matches; a pair an
  earlier native restore wrote under the backup names is kept by its
  `pseudolife_restore-<timestamp>.txt` note, while one the Python restore
  wrote has no note and rotates like a backup, as it did under the Python
  backup. A data directory that cannot be listed or a restore journal that
  cannot be read skips rotation for that run with a warning saying why; the
  backup still succeeds. A `.part` file a killed backup left is removed by
  the next backup once it has been unchanged for 10 minutes.
- When `tar` reports a file in `/data` changing or vanishing while it was
  archived, backup takes the archive again (up to three attempts, 10
  seconds apart), keeping the verified dump, and says which attempt it
  kept; if every attempt meets a change it fails with tar's line and says to
  retry when the daemon is quieter or stop it for the backup. Other
  failures name their true cause (an unwritable directory, a timeout, a
  program that could not start, tar's or Docker's own last line), and a
  published file that could not be removed again is named rather than
  "No backup was written".
- If Docker is installed but cannot be asked whether the bank containers
  exist, backup without `--data-dir` refuses with that cause instead of
  archiving a data directory without its database and exiting 0.
- A held update lock now names every command that takes it: "another
  pseudolife-mcp update, restore or backup is running", in both backup and
  restore.
- A lite bank is refused with a line naming the Docker install; an `--out`
  inside the daemon's `/data` volume and a missing database are refused
  before anything is written. The file tier's refusals now name which cause
  they found: a backup directory inside the data directory, a path that
  cannot be resolved, or an entry the archive cannot write.
