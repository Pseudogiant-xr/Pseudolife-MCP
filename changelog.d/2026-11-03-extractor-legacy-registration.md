### Fixed (2026-11-03 — Preserve extractor registrations during migration)

- Native extractor autostart recognizes both historical Windows Sonnet task
  templates, checks every uninstall target before removing any registration, and
  refuses unrelated executables with shim-like arguments. Failed Linux replacements
  restore the prior local enablement link without changing other service links.
