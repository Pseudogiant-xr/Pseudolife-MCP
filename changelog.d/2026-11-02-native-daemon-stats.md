### Added (2026-11-02 — Native daemon statistics for CLI clients)
- The Rust daemon answers authenticated `GET /api/stats` with the complete service snapshot used by `memory_stats`, allowing native clients to check bank readiness and statistics without a Python daemon.
