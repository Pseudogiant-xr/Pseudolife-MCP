### Added (2026-10-27 — the Rust daemon serves the Console's config editor read)

- The Rust daemon now answers `GET /api/config`, which returned 501 `not_implemented`, with the Python daemon's knob table: every Console knob grouped as the Console shows it, each with the value the daemon runs with (as config.yaml set it at start, else the default) and, for a knob that needs a restart, the `saved` value config.yaml now holds when it differs. Saving settings (`POST /api/config`) still returns 501 on the Rust daemon.
