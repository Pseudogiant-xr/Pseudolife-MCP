### Fixed (2026-10-27 — the Rust daemon prunes old fact versions as the Python daemon does)
- `memory.compaction` was read but never acted on by the Rust daemon, so
  superseded and retired versions of facts, world facts and lessons piled
  up in its bank indefinitely. Its background sweep now compacts them as
  the Python daemon does: per slot it keeps the newest `keep_per_slot`
  old versions and deletes the rest once they are older than
  `min_age_days`. Current values are never touched.
