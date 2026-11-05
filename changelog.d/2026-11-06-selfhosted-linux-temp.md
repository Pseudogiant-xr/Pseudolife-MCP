### Fixed (2026-11-06 — Use the work disk for self-hosted Linux temporary files)
- Linux parity shards use the runner-managed job directory for temporary files, so a small RAM-backed `/tmp` no longer triggers the updater's free-disk refusal. The runner clears that directory between jobs.
