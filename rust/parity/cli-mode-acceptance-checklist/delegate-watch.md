| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| delegate-watch | 2 | ported | yes | Both command names answer every invocation natively; neither is in `DEFERRED_MODES`. The existing watch loop, detach and pid-identity checks are preserved. Unsupported spellings, paths and pid records receive a refusal with a remedy before effects. `rust/shim/tests/cli_conductor_watch.rs`, the conductor-watch unit tests and both `conductor_watch` goldens pin the contract; `rust/cli_harness/rows/conductor_watch.md` lists the native answers. |
