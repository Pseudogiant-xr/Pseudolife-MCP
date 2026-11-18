### Fixed (2026-11-19 — Retry transient Windows autostart task reads)

- Autostart setup and removal retry the Task Scheduler provider's temporary file-not-found error when reading an existing task's run-as account. A persistent error or an unverified task still stops the operation.
