### Fixed (2026-10-26 — backups no longer copy installed runtimes or the lite password file)

- `pseudolife-mcp backup`'s state archive now leaves out the software the
  default data dir also holds, which reinstalls and is no bank state: the
  shim's `runtimes/` and `bin/`, and `native/` (pinned native runtimes such
  as the Rust lite tier's PostgreSQL). On a lite install with the default
  data dir, each state archive had carried the shim's runtimes, hundreds
  of MB.
- The Rust CLI's state archive also leaves out `embedded_pg.secret`, the
  bank's superuser password, as the Python CLI has since 2026-10-25.
