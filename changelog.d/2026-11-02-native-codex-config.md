### Added (2026-11-02 — Native Codex connection configuration)

- The Rust CLI now includes a shared Codex configuration writer for native setup
  and client reconnection. It uses Codex's versioned configuration API, preserves
  unrelated settings, validates credential ownership before writing, and keeps
  private backups with guarded rollback. The Python installer remains the default.
- Linked Codex homes and configuration files retain their original resolution,
  explicit no-spawn requests persist in the user layer, and conflicting
  credential settings identify their configuration file and key without
  displaying credential values.

Explicit no-spawn requests now persist in the Codex user configuration even
when a project layer or installer environment already supplies a truthy value.
Setup diagnostics distinguish empty user credentials from project overrides.
