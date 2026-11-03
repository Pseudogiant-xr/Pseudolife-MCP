# Offline flat migration

The `migrate_flat` CLI harness row compares every table and sequence, config
bytes, migration hashes, stdout and the backup pair. Its named
`flat-migration-files` rule replaces the single generated UUID directory name
and verified backup timestamps, then uses the existing `backup-files` rule to
compare decompressed SQL and tar members. This does not normalize entry data,
capacity or migration-plan hashes.
`tests/test_migrate_flat.py` covers the new migration and startup fences;
these tests postdate the pinned Python oracle inventories.
The capture validates that absolute recovery paths name files in this arm's
`bank/backups` directory, then records that exact relative suffix under
`{HOME}/bank/backups`; no host path is saved in the golden metadata.
Record Linux goldens from a neutral test account: backup archives retain
owner and group metadata inside compressed payloads. Run the tracked-tree
identifier guard after recording; it inspects nested archives as well as text.

Physical restore is separately exercised by
`daemon/harness/test_flat_migration_restore.py` against an owned disposable
PostgreSQL owner connection. The restricted ordinary test login cannot rewrite
extension comments in a physical dump.

This command does not retire any preset or change normal startup. Only an
explicitly migrated bank with unfinished config or a mismatched loaded flat
layout is fenced before hydration. Automatic install/update integration and retirement follow
in a separate change.
