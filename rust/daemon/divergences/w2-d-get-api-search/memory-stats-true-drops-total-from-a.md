| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `memory_stats` `true_drops_total` from a malformed `capacity_true_drops` count | `int()` of any value Python accepts, including non-ASCII digit strings | integers, floats (truncated), booleans and ASCII integer strings; anything else serves both drop keys as null, as Python does for what `int()` rejects | the only writer stores a JSON integer |
