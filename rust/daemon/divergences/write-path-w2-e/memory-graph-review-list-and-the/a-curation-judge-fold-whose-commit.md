| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A curation judge fold whose commit outcome is unknown and whose reload cannot be classified | answers `reconciliation_required` and latches the recovery: every later call retries it and raises until the bank resolves | logs the reconciliation and applies nothing; later ticks are not gated (the next one sees the bank's state under its row locks and rehydrates the resident store) | the daemon runs no per-call recovery passes (the `_ensure_init` row above) |
