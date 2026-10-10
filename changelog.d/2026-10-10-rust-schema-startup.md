### Changed (2026-10-10 — Rust schema startup follows the Python migration ladder)
- Generate the Rust daemon's DDL, ordered conditional migrations and version metadata from the complete Python schema source, with strict regeneration checks.
- Refuse banks stamped with a future or malformed schema version before DDL. Compare disposable historical PostgreSQL schemas and restart state against Python, including migration-required dimension refusals.
