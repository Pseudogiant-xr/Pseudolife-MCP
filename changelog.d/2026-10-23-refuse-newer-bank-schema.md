### Fixed (2026-10-23 — an older daemon no longer stamps a newer bank down)
- The Python daemon's startup schema step (`ensure_schema`) wrote its own
  schema version over whatever the bank carried, so starting an older
  release against a bank a newer release had upgraded rewrote the stamp
  downward with no warning (a bank stamped 57 came back as 56 in the
  activation rehearsal). A rollback then crossed a schema boundary silently,
  and the bank claimed a version its tables no longer matched. The daemon
  now refuses to start on a bank stamped by a newer build, before any DDL
  runs, and leaves the stamp as it is; `/health` reports the refusal as
  `init_refusal`, and the shim prints it. The message names both ways out:
  run that release or a newer one (`pseudolife-mcp update`), or restore the
  backup taken before the upgrade (`ops/restore.ps1 -Apply -BackupFile
  <dump>` / `ops/restore.sh --apply --backup-file <dump>`). A stamp that is
  not a positive integer is refused the same way. The Rust daemon already
  refused both. It now gives the same text, and its startup plan runs the
  check as its own `version_guard` step.
- The newer-build check runs before the embedding-dimension check, so a
  bank a newer release upgraded is named as that, not as a dimension
  migration.
- `pseudolife-mcp update` adds a line to its printed rollback when the
  deploy moved the daemon's schema: an image-only rollback across that
  change is now refused, so the backup must be restored after the image
  rolls back (`--json`: `rollback.restore_required`). A deploy that never
  turns healthy says the same, conditionally, beside its rollback, and a
  `--no-backup` run says it took no backup.
- The Rust parity scenario `stamped` now requires both daemons to refuse
  the stamp with the same message and to leave the bank unchanged; the
  divergence it recorded is retired.
