### Added (2026-10-19 — the Rust daemon serves memory_consolidate)

- The Rust daemon's MCP endpoint now answers `memory_consolidate` as the Python daemon does: a list of memories, chosen by row id or unique exact text, is retired and replaced by one canonical note in a single transaction, with every unresolved target reported and nothing changed unless all of them resolve.
