### Added (2026-11-02 — Opt-in Docker daemon engine selection)
- Checkout updates accept `--engine rust` and `--engine python`, retain the selected engine, back up the bank and state volume, tag the running image for rollback, and verify the replacement's version, schema and authenticated bank read before accepting it. Python remains the default and client installations are unchanged.
- Rust activation refuses pre-v57 banks and pending Python weights imports. Release-mode Rust selection names checkout mode until a Rust release image exists.
- `--engine rust --shadow` provides an isolated loopback companion with a SELECT-only database role and read-only bank state, gated on the checkout containing read-only daemon support.
