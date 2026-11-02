| Row | Python | Rust | Boundary and evidence |
|---|---|---|---|
| CLI-MIGRATE-FLAT | `migrate_flat_cli.py` | `shim/src/cli/migrate_flat.rs`, `daemon/src/migrate_flat_config.rs` | Opt-in `migrate-flat --apply --data-dir PATH`, explicit database URL, stopped writer, existing regular config file, physical backup, lossless band update and resumable config finalization. Native execution needs the matching daemon binary beside the CLI or on PATH. |

The `migrate_flat` CLI harness row compares every table and sequence, config
bytes, migration hashes, stdout and the backup pair. Its named
`flat-migration-files` rule replaces the single generated UUID directory name
and verified backup timestamps, then uses the existing `backup-files` rule to
compare decompressed SQL and tar members. This does not normalize entry data,
capacity or migration-plan hashes.

Physical restore is separately exercised by
`daemon/harness/test_flat_migration_restore.py` against an owned disposable
PostgreSQL owner connection. The restricted ordinary test login cannot rewrite
extension comments in a physical dump.

This command does not retire any preset or change normal startup. Only an
explicitly migrated bank with unfinished config or a mismatched loaded flat
layout is fenced before hydration. Automatic install/update integration and retirement follow
in a separate change.
