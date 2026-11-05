### Fixed (2026-11-04 — find the remote daemon from Codex shell commands)
- Codex and T3 Code Codex sessions running `pseudolife-mcp lease` or `doctor` discover the daemon and credential file saved by install/update/connect when the host omits the MCP server's environment. Explicit origins remain authoritative; an ambient credential must match the saved connection before its origin is selected.
