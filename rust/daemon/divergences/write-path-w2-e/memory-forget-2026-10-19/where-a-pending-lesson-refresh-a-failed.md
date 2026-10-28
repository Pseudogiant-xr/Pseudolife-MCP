| Item | Python | Rust | Owner / why |
|---|---|---|---|
| Where a pending lesson refresh (a failed mirror after a committed forget) is retried | in `_ensure_init`, before every service call, whose failure fails that call | before the next lesson read (`memory_lesson_search`, the history chain) or lesson save; other tools read no lessons | failure path only |
