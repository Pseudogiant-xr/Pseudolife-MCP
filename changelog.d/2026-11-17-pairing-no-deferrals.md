### Fixed (2026-11-17 — Native invite and pair always answer)
- `invite` and `pair` handle supported argument spellings natively and explain unsupported inputs without falling back to Python or exposing pairing secrets. Existing code validation, grants and owner-only token protection remain intact.
- When a refused pairing cannot remove its new token file, `pair` keeps the file and reports its path, the original refusal state and the recovery step.
- Database connection failures return exit 1. A lost invite COMMIT reply reports an unknown outcome with exit 1, warns that a code may exist, and names the check and revocation commands.
