| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A curated restore whose commit outcome is unknown and whose reload cannot be classified | raises, and every later call retries the recovery until it resolves | raises the same internal error; later calls are not gated | the daemon runs no per-call recovery passes (the `_ensure_init` row above) |
