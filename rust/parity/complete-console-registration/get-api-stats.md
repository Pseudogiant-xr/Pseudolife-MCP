| Method | Path | Phase | Status | Rust | Evidence |
|---|---|---:|---|---|---|
| GET | /api/stats | 3 | deferred | 501 | 501 (`w2d-console-projections`, W2-D); declared case in `daemon/harness/run.py`; CLI, shim and eval callers only, no Console caller on master `2092e7b1`: CUTOVER.md proposal A (D9) moves it to the CLI batches |
