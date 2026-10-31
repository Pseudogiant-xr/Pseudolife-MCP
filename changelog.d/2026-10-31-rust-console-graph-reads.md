### Added (2026-10-31 — Console graph reads in the Rust daemon)
- Serve the Console's graph, shortest path, projects, digest, entity provenance, wiki, recall and dream status GET routes through the Rust services, with the Python HTTP defaults and response shapes.
- Cap whole-graph views with a heap-based degree peel after releasing the write lock, preserving Python's degree/name tie-breaking and output order.
