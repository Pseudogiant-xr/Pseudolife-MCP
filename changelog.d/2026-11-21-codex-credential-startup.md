### Fixed (2026-11-21 — Codex setup on a slow daemon start)
- Native installers retry the authenticated Codex credential check while the daemon starts and show the reason if setup cannot finish. A rejected credential still fails immediately. Native `connect` uses the same startup budget for its authenticated check.
