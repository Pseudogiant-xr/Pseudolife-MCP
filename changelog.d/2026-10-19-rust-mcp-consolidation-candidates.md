### Added (2026-10-19 — the Rust daemon serves memory_consolidation_candidates)

- The Rust daemon's MCP endpoint now answers `memory_consolidation_candidates` as the Python daemon does: clusters of near-duplicate memories, anchored by a query (through the same retrieval as `memory_search`) or by an episode, each member a memory a correction can still address, ranked by cohesion and size.
