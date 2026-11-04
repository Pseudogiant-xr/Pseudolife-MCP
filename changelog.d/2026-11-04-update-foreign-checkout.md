### Fixed (2026-11-04 — Refuse checkout updates on another compose project)

- Checkout updates inspect the daemon's compose project before writing an
  environment file, backing up, tagging or building. A foreign project is left
  unchanged, with the release-mode command that updates its own configuration.
  The check also covers engine and shadow updates and honours customized project
  names resolved by Compose.
