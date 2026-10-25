### Added (2026-10-25 — the Rust daemon's memory search finds ingested documents)
- Memory search on the Rust daemon (`memory_search`, `GET /api/search`)
  served no document hits, and `memory_stats` counted no documents, because
  the reference pool was not ported. Both now read the bank's
  `reference_chunks` table as the Python daemon does: up to
  `memory.ref_top_k` chunks (3) are appended after the memories, a chunk
  whose text a memory already serves or that scores under the floor is
  skipped, a failed document read serves none instead of failing the search,
  and the reranker, the timeline order and contiguity treat them as Python
  does. `memory_stats` reports the chunk and document counts and adds the
  chunks to `total_memories`. The consolidation query anchor reads the pool
  too, as Python's does, then drops the chunks it cannot address.
- This closes the second half of the Rust cutover's reference-store blocker
  (the first was the document tools). Nothing installs the Rust daemon yet,
  so no user setup changes.
