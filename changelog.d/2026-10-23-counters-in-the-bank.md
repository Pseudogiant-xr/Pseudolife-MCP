### Changed (2026-10-23 — the memory counters live in the bank, schema v57)

- The counters that `memory_stats` reports across restarts (interaction and
  logical-turn counts, retrieval queries, per-band hits, surprise history,
  consolidation events) are saved in the bank's `cms_counters` row instead
  of the file `memory_state/weights.pt`, so a backup dump carries them and
  the Rust daemon can read them. Nothing to run: the first start on a bank
  without the row imports an existing `weights.pt` (falling back to its
  `.bak`) once and leaves the file in place; a file neither copy of which
  can be read starts the counters fresh and reports `weights_reset`, as
  before. A failed read of the row now fails the start and is retried,
  like any other failed load, so an autosave never overwrites real counters
  with fresh ones. Rolling back to an older release reads the old file,
  frozen at the upgrade, so its counters restart from that point.
