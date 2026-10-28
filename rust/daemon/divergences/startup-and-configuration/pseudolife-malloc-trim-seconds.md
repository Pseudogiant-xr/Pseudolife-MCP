| Item | Python | Rust | Why |
|---|---|---|---|
| `PSEUDOLIFE_MALLOC_TRIM_SECONDS` | parsed before platform availability is checked; glibc trim runs only on Linux | parsed at start; glibc trim on Linux, no trim elsewhere | canonical finite seconds are implemented; Python-only numeric spellings remain deferred |
