| Item | Python | Rust | Owner / why |
|---|---|---|---|
| A lesson read racing a forget | waits on the service lock and the refresh barrier | reads its snapshot without the lock: between the cascade's commit and the mirror it can still serve a lesson retiring | the read slices' lock-free snapshots (as for the cortex) |
