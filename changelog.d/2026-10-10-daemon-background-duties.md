### Added (2026-10-10 — Rust daemon background duties)

- Add owned background scheduling, idle session reaping and deferred-empty
  tombstones, release knowledge, glibc heap trimming, and dream/retrieval
  journal pruning to the experimental Rust daemon.
- Keep writer persistence, canonical-store compaction integration, session
  write/resume routes, and automatic dream execution explicitly deferred.
- Fence background maintenance and session write-through with the shared
  writer recovery seam; retain autocommit boundaries for individual deletes
  and session updates.
