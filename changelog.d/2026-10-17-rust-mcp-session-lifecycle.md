### Fixed (2026-10-17 — the Rust daemon's MCP sessions follow mcp 2.2)

- A session whose opening request is refused or fails is discarded, so its id answers `Session not found`, as mcp 2.2 does.
- A session ended with DELETE is forgotten: a later request with its id answers `Session not found` without the id header, not `Session has been terminated`. The daemon keeps no history of ended sessions.
