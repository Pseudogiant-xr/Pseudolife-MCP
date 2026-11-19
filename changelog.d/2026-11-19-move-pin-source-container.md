### Fixed (2026-11-19 — Keep native move tied to its selected source containers)
- Native `move` refuses a source whose daemon or PostgreSQL lacks an immutable Docker container ID or whose containers belong to different Compose projects.
- Stops, starts, restart policy changes, and moved-marker writes and removal use the selected daemon. PostgreSQL sessions, dumps and database fences use the selected PostgreSQL container.
- JSON dry-run plans include an `executable` flag. Plans that verify carried bearer settings report `false` while execution is unavailable.
