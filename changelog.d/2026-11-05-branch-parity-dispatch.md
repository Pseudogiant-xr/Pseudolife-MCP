### Added (2026-11-05 — Validate parity workflow changes on reviewed branches)
- Manual Rust CI can bind a reviewed branch checkout to one frozen master merge base shared by every oracle consumer. A full matching checkout SHA is required, source-pin checks remain in place, and failed CI binding prevents expensive builds while the Parity gate fails closed.
