### Fixed (2026-11-03 — Native live maintenance settings)

- Console saves now apply the native daemon's store surprise threshold,
  canonical compaction settings, retrieval and lesson-signal retention,
  and dream-run history limit without a restart.
- The running settings change together after the config file is saved;
  failed saves leave them unchanged, and concurrent patches retain both changes.
- Other settings continue to report that a restart is required until their
  native consumers support live updates.
