### Fixed (2026-11-03 — Migrate detached provider shim tasks)

- Native extractor autostart recognizes historical detached Claude and Codex
  tasks, including their model, health-cache and optional CLI-path arguments.
  Native task ownership requires the selected executable path or the exact
  generated wrapper, rather than a matching executable filename.
