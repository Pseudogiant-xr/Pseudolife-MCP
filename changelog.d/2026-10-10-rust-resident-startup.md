### Added (2026-10-10 — Complete Rust bank startup inputs)

- The Rust daemon now loads episodes, canonical-store rows and identity metadata together with capacity-aware entry seating before serving. Late clock failures retain those stores and retry only the clock; required loader failures still abandon and back off. Health reports the generated schema version.
