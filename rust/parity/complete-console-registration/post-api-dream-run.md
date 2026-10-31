| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| POST | /api/dream/run | 4 | served | `routes/writes.rs` → `dream/run.rs::run_auto` | `harness/w2e.py` console-dream-run, console-dream-run-gates, console-dream-run-empty, console-dream-run-fallback, console-dream-bodies, console-dream-concurrent: HTTP status/body, bank state and extractor calls; Python 3.11 goldens |
