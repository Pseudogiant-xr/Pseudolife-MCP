| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `memory_outcome` `used_ids` tokens in non-ASCII digits | `int()` takes any Unicode decimal digit (`"١٢"` is 12) | ASCII digits only (with sign, leading zeros, single underscores and the 4,300-digit limit, as `int()`); other digits count as ignored | the same ASCII-only rule as `memory_stats`' drop count; agents type ASCII ids |
