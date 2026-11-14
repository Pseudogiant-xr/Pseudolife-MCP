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
  makes a later run of that bank name both safety backups with their
  dates: as before the interrupted restore, and as immediately before it
  (the incomplete bank, unless repaired since); a later run that fails
  before its own drop gives both courses, recover or (if repaired) start
  the daemon and delete the journal.
  This covers the restore items of #1497 (an interrupt after the drop now
  prints the recovery command; a corrupt deflate stream is refused) and of
  #1507 (a wrong-typed `docker inspect` field is the `cannot inspect`
  refusal; a Ctrl-C at the confirmation prompt ends the run with nothing
  changed).
- The state archive check is never more permissive than the `tar` that
  extracts it (probed against GNU tar 1.34 in the daemon image): POSIX
  extended headers are now read, numeric fields and the ustar prefix are
  read as tar reads them, and a member ending in `/` is a directory by its
  final name, while only the member types a state archive holds are read
  (plain files, links, directories); any other type, a link or directory
  with a non-zero size, a hard link to anything but a plain file before
  it, an extended header that repeats or empties a name or size, a GNU long
  name over 1 MiB, a sparse member, a global extended header that sets a
  name or size, a damaged header anywhere, a `move.json` that is not a
  plain file and data after the end marker are refused with their reason.
- The state is also judged by what `tar` actually extracts: before the
  database is dropped, the archive is extracted into a staging directory
  in `/data` and the extracted tree is checked (only plain files,
  directories and relative symlinks that stay inside it, no inode shared
  with anything outside it, no setuid or setgid file). A refusal there,
  or a failure or Ctrl-C while staging, leaves the bank unchanged; after
  the replay the staged tree is renamed into place. `/data` must have 8 GiB
  free beyond the archive's size plus what it unpacks to. A daemon with a
  volume or filesystem mounted inside `/data` (from `docker inspect` and
  from the mount table, which also shows one below a bind-mounted host
  folder) is refused before anything stops when the state is restored,
  since replacing `/data` would empty that volume.
- A lite bank is refused with a line naming the Docker install; restore
  runs `docker` only from absolute `PATH` entries or `PSEUDOLIFE_DOCKER`'s
  full path; the restarted daemon gets up to ten minutes to report ready.
