| Key | Python | Rust | Owner | D7 bucket | Code verification / B14b owner |
|---|---|---|---|---|---|
| `memory` byte counts | live | live (same reader); compared by `source` only | values are process-specific | closed | `rust/daemon/src/health.rs::memory_headroom` and `pseudolife_memory/utils/memory_headroom.py::read_memory_headroom` use live process/cgroup metrics; values legitimately differ between processes, with the same fields/reader contract rather than an omitted counter. B14b owner: none (closed). |
