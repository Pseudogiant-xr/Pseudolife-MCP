### Fixed (2026-11-15 — the Codex setup step names every backup in a path you can use)
- On Windows, `pseudolife-mcp installer codex-hooks` reported the backup of
  an existing instruction file (AGENTS.md) in the `\\?\C:\...` verbatim
  spelling, and the backups of the Codex configuration and credential files
  could keep the ordinary spelling where it is too long to open. Every
  reported backup now uses the ordinary spelling, and the verbatim one only
  where Windows needs it (a path of 260 characters or more).
