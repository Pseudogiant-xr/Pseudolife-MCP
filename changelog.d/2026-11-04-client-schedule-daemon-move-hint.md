### Fixed (2026-11-04 — Re-arm scheduled client updates after a daemon move)
- Scheduled client update errors and schedule confirmations now show `pseudolife-mcp update --schedule HH:MM --daemon-url <new-url>` when the daemon moves, using the chosen daily time when known. Python and native clients keep custom daemon URLs and leave unreachable-daemon runs unchanged.
