### Fixed (2026-11-19 — Keep native move tied to its selected source daemon)
- Native `move` checks the originally selected Docker container is stopped before writing the moved-bank marker. Dry runs that can verify carried bearer settings report `"executable": false` when execution is unavailable.
