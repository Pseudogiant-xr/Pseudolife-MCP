### Fixed (2026-11-01 — Idle Rust sessions keep meaningful titles)
- The Rust daemon now derives content titles when it closes idle, nonempty sessions that still have generic names, and persists the matching entry title stamps across restarts.
