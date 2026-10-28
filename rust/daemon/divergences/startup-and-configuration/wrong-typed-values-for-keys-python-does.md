| Item | Python | Rust | Why |
|---|---|---|---|
| Wrong-typed values for keys Python does not validate (`memory.top_k: abc`), and sections that are null or not mappings (`updates:` with every child commented out) | starts, fails later at use, or omits that `/health` key | refuses to start (exit 1) naming the key | fail early instead of serving a half-broken daemon |
