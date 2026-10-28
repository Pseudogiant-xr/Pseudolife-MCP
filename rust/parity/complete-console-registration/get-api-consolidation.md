| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| GET | /api/consolidation | 3 | deferred | served | `read/console.rs`; `daemon/harness/w2d_console.py live` (`console-reads`, `console-reads-demote`, on `w2e.py`'s engine) compares status and body and the full bank after each call; recorded Linux dev-box run cited in the PR, until a published nightly run (B13, D11) covers it: no anchor, query and episode anchors, `top_k` 0 and negative |
