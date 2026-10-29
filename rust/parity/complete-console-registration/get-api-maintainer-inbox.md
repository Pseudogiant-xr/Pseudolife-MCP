| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| GET | /api/maintainer/inbox | 3 | deferred | served | `read/maintainer.rs`; `daemon/harness/w2d_console.py live` (`console-maintainer` and its four refusal scenarios: unset and malformed origins, a principal not admitted, the board off) compares status and body and the full bank after each call, over SQL-seeded maintainer board state; recorded Linux dev-box run cited in the PR, until a published nightly run (B13, D11) covers it: replies newest first, a `reply_to`, the limit clamp |
