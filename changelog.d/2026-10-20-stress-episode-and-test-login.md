### Fixed (2026-10-20 — stress loops run the episode executable tests and Linux test_login container rows)

- A `mode=stress` loop over `evals/rust_port/test_episode_executable.py` now gets the stdio candidate in `PSEUDOLIFE_PORT_EPISODE_JSON`, as the eval shard does, so its native cases run instead of skipping and failing the iteration.
- On Linux, a stress selection naming `test_login` builds the `pseudolife-pg:18` image first, as the W1-C connection shard does, so the row's container cases can start.
