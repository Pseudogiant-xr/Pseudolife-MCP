### Added (2026-11-02 — Native Codex connection configuration)

- The Rust CLI now includes a shared Codex configuration writer for native setup
  and client reconnection. It uses Codex's versioned configuration API, preserves
  unrelated settings, validates credential ownership before writing, and keeps
  private backups with guarded rollback. The Python installer remains the default.
- Linked Codex homes and configuration files retain their original resolution,
  already-enabled project no-spawn choices are preserved, and conflicting
  credential settings identify their configuration file and key without
  displaying credential values.
