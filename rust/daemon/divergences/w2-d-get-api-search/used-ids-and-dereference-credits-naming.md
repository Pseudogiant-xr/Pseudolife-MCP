| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `used_ids` and dereference credits naming the entry the startup warmup search served | the warmup search logs a NULL-session event: a session-less credit may name it as the most recent serving event, and a session's `used_ids` counts it as served elsewhere | Rust's warmup logs no event (the declared warmup row in `harness/run.py`), so such a credit names the previous NULL-session event or none, and the id may read as unmatched | W2-D warmup telemetry; the harness's ops never name the entry the warmup serves |
