### Added (2026-10-17 — the Rust daemon serves memory_recent)

- The Rust daemon's MCP endpoint now answers `memory_recent` as the Python daemon does: the newest stored memories first, with the same filters, compact entries by default and full records with `verbose`.
