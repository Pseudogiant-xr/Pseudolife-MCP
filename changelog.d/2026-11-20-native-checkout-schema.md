### Fixed (2026-11-20 — Native checkout schema version)
- Checkout updates read the target schema version from the Rust daemon's schema plan, so they can validate a checkout after the frozen Python schema file is removed.
