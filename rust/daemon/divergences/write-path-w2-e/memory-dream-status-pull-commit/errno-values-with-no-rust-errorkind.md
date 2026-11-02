| Item | Python | Rust | Owner / why | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| Errno values with no Rust `ErrorKind` (ESHUTDOWN) | Python's class (`BrokenPipeError`) | `OSError` | transport edge case | presentation | At `54063c1788f2`: `rust/daemon/src/dream/extractor.rs` `io_failure` falls back to OSError; `pseudolife_memory/memory/dream.py` `classify_extractor_error` reports Python's errno subclass. Both select `extractor_unreachable` for this OSError case; only diagnostic class text differs. B14b owner: B14b presentation disposition. |
