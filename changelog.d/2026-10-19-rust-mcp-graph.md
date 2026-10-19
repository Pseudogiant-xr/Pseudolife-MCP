### Added (2026-10-19 — the Rust daemon serves memory_graph)

- The Rust daemon's MCP endpoint now answers `memory_graph` as the Python daemon does: an entity's neighborhood up to three hops, with derived transitive and inverse edges, each node's current facts (including facts written under an alias), community and provenance tags, an optional shortest path to a second entity, and a relation filter. An empty entity returns the whole graph with each node's sources.
