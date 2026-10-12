### Added (2026-10-10 — Rust graph storage)
- Add native entity, alias, relation and edge storage for the Rust daemon port,
  including sticky removals, evidence transactions, orphan repair and entity
  merge/delete operations, checked against the Python store after every write.
  Graph service routes and review queue acceptance remain later port increments.
