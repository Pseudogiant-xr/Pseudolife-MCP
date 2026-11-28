### Performance (2026-11-27 — world facts, corrections and auto-promoted slots embed before the write lock)
- `memory_world_set`, `memory_supersede` and `memory_consolidate`, and with the opt-in `memory.cortex.auto_promote` a store's promoted slots, now compute their embeddings before taking the daemon's write lock, as `memory_store` and `memory_fact_set` already do. Requests that need the lock no longer wait out these writes' forward passes.
- A correction still answers a missing or malformed selector at once, without embedding. A correction whose target is not found embeds its replacement before the lookup, so it now spends that forward pass.
- The restore of a lesson or world fact from a curation audit snapshot still embeds under the lock, because it reads its snapshot under it.
- Stored rows, vectors and answers are unchanged.
