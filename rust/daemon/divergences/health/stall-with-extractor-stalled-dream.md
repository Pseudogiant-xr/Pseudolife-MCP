| Key | Python | Rust | Owner |
|---|---|---|---|
| `stall` (with `extractor: "stalled"`), `dream_tracking_error`, `capacity_warning` | subsystem state, none touching `status` | the same keys and values, read without the service lock, in Python's order (unit-tested in `health.rs`) | closed (B4): `harness/w2e.py` `health-stall`, `health-stall-fallback`, `health-capacity`, `health-tracking-error` compare the values live, with the `health-no-stall` and `health-no-capacity-warning` mutants |
