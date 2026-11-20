### Fixed (2026-11-20 — Upgrade legacy Docker banks after Python releases end)
- Release-mode `pseudolife-mcp update` uses the archived Python 0.23.1 daemon to import legacy banks before deploying the selected Rust release. It checks the import before moving clients and keeps the original bank and image available for recovery.
