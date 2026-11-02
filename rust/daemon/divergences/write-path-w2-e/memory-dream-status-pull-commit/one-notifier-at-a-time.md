| Item | Python | Rust | Owner / why | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| One notifier at a time | a non-blocking `threading.Lock` | a non-blocking async mutex | a second notifier skips in both | closed | At `54063c1788f2`: `rust/daemon/src/dream/ticks.rs` `stall_notify` holds `notify_lock.try_lock()` through send and marking; `pseudolife_memory/service_dream.py` `dream_stall_notify` holds `notify_lock.acquire(blocking=False)` through the same steps. Both skip the second notifier. B14b owner: none (this obligation is closed). |
