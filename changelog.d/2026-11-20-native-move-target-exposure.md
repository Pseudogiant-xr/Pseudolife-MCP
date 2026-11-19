### Added (2026-11-20 — Explicit tailnet exposure during a native move)
- Native `move --expose-target` can reuse or create the target's Tailscale forward, then verifies that the URL serves the moved bank before committing. Rollback uses the target's ownership receipt to identify a forward created by the move.
