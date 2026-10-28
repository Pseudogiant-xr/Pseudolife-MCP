## Document store (B7): document_ingest and document_search (2026-10-25)

Both tools read and write the bank's `reference_chunks` table (schema v57)
as Python does (`documents.rs`): pathlib's parse of the path, the
missing-file refusal, the refusal of anything but a regular file of at
most 64 MiB (`document_file_problem`), `extract_text`'s branches and their error classes,
UTF-8 decoding with replacement and text-mode newlines, the chunk windows
and their md5 ids, the upsert that keeps a chunk's first `created_at`, and
the exact cosine search ordered by distance then chunk id, a stored 0.0
ingest time served as the clock at serving (`MemoryEntry.__post_init__`). The unit goldens
come from the Python functions (`harness/gen_document_goldens.py`); the
answers and the table are compared by `harness/w2e.py`'s `documents-*`
scenarios (`harness/w2e_documents.py`).

Since B7's second Rust PR the memory search (`/api/search`, `memory_search`
and the consolidation query anchor) also serves the reference pool as
Python's `retrieve` does (`cms.py:1440-1464`): up to `memory.ref_top_k`
chunks (the read takes `reference.max_results` rows when it is 0, and
`ref_pool[:0]` then serves none), read leniently (a failed read, a
negative LIMIT included, serves none), each skipped when its text was already seen or it
scores under the floor, appended after the memories, counted in the
reranker's budget, kept by the deferred cut, trailing in timeline order,
and anchoring contiguity neighbours as an episode-less hit of its source.
`memory_stats` counts the table's chunks and distinct sources, and adds the
chunk count to `total_memories`. No divergence is declared for these; the
`documents-search-*` scenarios compare them.
