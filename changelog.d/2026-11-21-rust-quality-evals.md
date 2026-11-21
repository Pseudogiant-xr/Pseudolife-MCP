### Changed (2026-11-21 — Rust daemon quality evals)
- LongMemEval drives the Rust daemon's public store, dream, search and history interfaces. The runner records its input and model identities and can resume extraction, answering and independent judging from saved artifacts.
- The retrieval gate measures public Rust search against a separate Rust baseline. The extractor ladder measures Rust dreams against a frozen raw-turn control.
