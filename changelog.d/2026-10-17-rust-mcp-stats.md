### Added (2026-10-17 — the Rust daemon serves memory_stats)

- The Rust daemon's MCP endpoint now answers `memory_stats` as the Python daemon does: band sizes, capacities and hit rates, the capacity warning, capacity drops, retrieval-log health, the read audit and graduation candidates. Swallowed telemetry failures are now counted for it instead of only logged.
