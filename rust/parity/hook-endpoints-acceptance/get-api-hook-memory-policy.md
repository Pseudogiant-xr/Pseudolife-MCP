| Item | Phase | Status | Rust | Evidence |
|---|---|---|---|---|
| GET /api/hook/memory-policy | 4 | deferred | served | `routes/hook.rs`, `session_hook.rs`; `daemon/harness/w2e.py live --only hook-memory-policy hook-memory-policy-default` (`w2e_hooks.py`) compares status, content type, cache policy and body bytes over the variant, the A/B arms, resume and compact, and an unauthorized caller. `MEMORY_LOOP_BLOCK` is recorded byte for byte (`harness/record_routes.py --check`, `goldens/memory-loop-block.txt`) |
