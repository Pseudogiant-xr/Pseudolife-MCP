### Fixed (2026-11-19 — Verify the selected Docker target throughout a native move)
- Native move supplies the selected target container IDs to the remote helper and checks that Docker still returns those containers before operating. Recovery instructions identify the selected target and explain how to verify a replacement source PostgreSQL container before removing its fence.
