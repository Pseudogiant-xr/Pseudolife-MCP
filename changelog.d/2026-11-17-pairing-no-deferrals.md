### Fixed (2026-11-17 — Native invite and pair always answer)
- `invite` and `pair` handle supported argument spellings natively and explain unsupported inputs without falling back to Python or exposing pairing secrets. Existing code validation, grants and owner-only token protection remain intact.
- When a refused pairing cannot remove its new token file, `pair` keeps the file and reports its path and the recovery step.
