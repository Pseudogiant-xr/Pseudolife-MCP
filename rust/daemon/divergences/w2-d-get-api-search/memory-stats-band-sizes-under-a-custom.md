| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `memory_stats` band sizes under a custom preset that repeats a band name | positional: a store lands in `bands[0]` even when a later band shares its name | an entry carries its band name, so every entry of that name counts in the last band bearing it (exact at startup, where seating picks that band) | no preset repeats a name; a custom config would have to |
