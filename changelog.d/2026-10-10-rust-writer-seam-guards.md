### Fixed (2026-10-10 — Rust writer failures no longer report commit success or deadlock on nesting)

- Check transaction health before committing so a swallowed statement error returns an error and rolls back instead of reporting success for PostgreSQL's implicit rollback.
- Reject nested writer adapters in the same task with a clear panic message before they wait on their own lock; the next writer operation recovers an abandoned transaction.
