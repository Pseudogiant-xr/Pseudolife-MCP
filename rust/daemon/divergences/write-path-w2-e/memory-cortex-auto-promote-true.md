| Item | Python | Rust | Owner / why |
|---|---|---|---|
| `memory.cortex.auto_promote: true` | slot-shaped facts promoted into the cortex on every store | the client receives successful store admission with `cortex_promoted: 0`, but no cortex rows are written; the configuration is not refused | W2-E cortex PR (needs the cortex write path) |
