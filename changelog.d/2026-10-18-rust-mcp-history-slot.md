### Added (2026-10-18 — the Rust daemon serves memory_history slot reads)

- The Rust daemon's MCP endpoint now answers `memory_history` with an `attribute` (a fact slot's version chain, or a set slot's add and remove events) as the Python daemon does, including the `as_of` cutoff in epoch seconds or ISO-8601. Without an `attribute` (the entity's event chain) it still answers "not implemented": that mode needs the lesson store, which follows.
