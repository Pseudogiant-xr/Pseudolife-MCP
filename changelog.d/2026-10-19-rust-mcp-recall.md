### Added (2026-10-19 — the Rust daemon serves memory_recall)

- The Rust daemon's MCP endpoint now answers `memory_recall` as the Python daemon does: a seed search, the entities the question names (or its top hits name), a walk out through the graph hop by hop under the hub gate and the search ceilings, a re-query per newly reached entity, then the per-hop caps, pinned constraint facts and `re_verify` cautions. The `memory.recall` settings are read. The maintainer's 2026-10-19 decision returned graph traversal to the port; `rust/PARITY.md` records it.
