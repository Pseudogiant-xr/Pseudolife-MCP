| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `time_budget_seconds` (20 s) | wall clock (`time.monotonic`) | wall clock (`Instant`) | a walk near the budget can stop at a different step; the harness's walks finish in well under a second |
