### Changed (2026-10-23 — the memory counters live in the bank, schema v57)

- The counters that `memory_stats` reports across restarts (interaction and
  logical-turn counts, retrieval queries, per-band hits, surprise history,
  consolidation events) are saved in the bank's `cms_counters` row instead
  of the file `memory_state/weights.pt`, so a backup dump carries them and
  the Rust daemon can read them. Nothing to run: the first start on a bank
  without the row imports an existing `weights.pt` (falling back to its
  `.bak`) once, then renames the file `weights.pt.imported` (it is never
  deleted). A file neither copy of which can be read starts the counters
  fresh and reports `weights_reset`, as before. A failed read of the row
  fails the start and is retried, like any other failed load, so an autosave
  never overwrites real counters with fresh ones; a failed write is logged
  and the rest of the save goes on.
- Rolling back to an older release finds no `weights.pt` and starts its
  counters fresh. Upgrading again keeps the bank's counters and sets that
  release's file aside: a file newer than the row is imported only when it
  counts at least as many interactions, since counters only grow.
- An older daemon that refuses a newer bank is rolled back by restoring the
  backup taken before the upgrade. Restore its state archive with the dump
  (`ops/restore.ps1 -StateArchive` / `ops/restore.sh --state-archive`) and
  the older build finds its `weights.pt` again; restoring only the dump
  leaves the file set aside as `weights.pt.imported` (`weights.pt.bak.imported`
  when the backup copy was read, numbered `-2`, `-3` after later upgrades), so
  rename the newest back to `weights.pt` before starting the older build, or
  its counters start fresh.
