### Fixed (2026-11-17 — Catch Rust harness drift before batching)
- Every pull request now runs both Rust workflow and batch harness agreement checks in the existing tree-wide guards step, including pull requests that also edit workflows.
