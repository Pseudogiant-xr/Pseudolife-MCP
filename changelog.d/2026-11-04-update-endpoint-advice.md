### Fixed (2026-11-04 — Docker update endpoint advice)
- Failed Docker update verification now names `--daemon-url` and suggests a published loopback URL when available. Version mismatches report the observed endpoint response without assuming the Compose image setting caused it. Healthy custom URLs and reverse proxies remain valid.
