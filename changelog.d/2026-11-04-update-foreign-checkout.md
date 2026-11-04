### Fixed (2026-11-04 — Refuse checkout updates on another compose project)

- Checkout updates inspect the daemon's compose project before writing an
  environment file, backing up, tagging or building. A foreign project is left
  unchanged, with instructions to stop that project while preserving its volumes
  or use release mode. The check also covers engine and shadow updates.
