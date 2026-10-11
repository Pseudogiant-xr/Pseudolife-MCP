### Fixed (2026-10-11 — Audit cleanup warnings)
- PostgreSQL sessions retain every server notice until drained, so audit redaction reports all cleanup warnings in their original order beyond the former 4096-entry cap.
