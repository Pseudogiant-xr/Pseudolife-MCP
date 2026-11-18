| Mode | Phase | Status | Native | Evidence |
|---|---|---|---|---|
| delegate-watch | 2 | ported | yes | #1723 finished the native answers for both command names. The existing watch loop, detach and pid-identity checks run natively. Unsupported spellings, paths and pid records receive a refusal with a remedy before any change. `rust/shim/tests/cli_conductor_watch.rs`, the conductor-watch unit tests and both `conductor_watch` goldens pin the contract; `rust/cli_harness/rows/conductor_watch.md` lists the native answers. |
