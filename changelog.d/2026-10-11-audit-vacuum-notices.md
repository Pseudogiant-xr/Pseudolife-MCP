### Fixed (2026-10-11 — Audit cleanup warnings)
- Audit redaction retains every PostgreSQL cleanup notice during its vacuum collection window, preserving order beyond 4096 warnings. Other sessions retain only the newest 4096 notices, and dropping an undrained collection restores that bound.
