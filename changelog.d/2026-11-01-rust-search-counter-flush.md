### Fixed (2026-11-01 — Preserve search access counts across daemon restarts)
- Rust autosave and clean-exit saves now synchronize resident entry access counts with PostgreSQL alongside the counters row. Search counter updates participate in write admission and shutdown draining, and saves cannot split a ranking update or overwrite a concurrent entry-access increment.
