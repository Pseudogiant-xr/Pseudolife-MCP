### Fixed (2026-11-02 — Preserve owned Codex recovery state)

- Native client connection failures now return actual backup and compensation
  events, with a typed recovery handle only when the attempt still owns its
  writes. Unknown write acknowledgements never authorize rollback.
- Recovery preserves replacement files even when their bytes match, and checks
  the compensated configuration version before removing a retained token.
- Created-file cleanup rechecks identity after moving the file away from its
  public name. Recovery reports every retained owned file without contradictory
  restoration events or instructions referring to nonexistent backups.
