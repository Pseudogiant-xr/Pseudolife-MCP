| Item | Python | Rust | Owner | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| `application_name` fallback | derived from argv | `pseudolife-mcp pid=<pid> pseudolife-daemon serve` | free (not persisted) | presentation | `rust/daemon/src/storage/mod.rs::application_name` supplies the application name; `pseudolife_memory/storage/postgres.py::_application_name` uses argv; this is the diagnostic connection label, with no change to bank operations. B14b owner: B14b presentation disposition. |
