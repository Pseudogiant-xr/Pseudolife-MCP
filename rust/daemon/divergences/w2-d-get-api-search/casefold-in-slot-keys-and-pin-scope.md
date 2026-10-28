| Item | Python | Rust | Why / owner |
|---|---|---|---|
| Casefold in slot keys and pin scope | `str.casefold()` (full Unicode folding) | `to_lowercase` plus `ß` to `ss` and final sigma | rare foldings (ligatures and similar) differ; no producer writes them in slot names. Closable with a casefold crate |
