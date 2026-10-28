| Item | Python | Rust | Why / owner |
|---|---|---|---|
| `memory_stats` `slot_index_shadow_divergences` and `weights_reset` | a sampled shadow check of the slot index; a flag for a pre-v57 `weights.pt` its one-time import found unreadable | `0` and `false`: Rust has no slot-index shadow and imports no `weights.pt` | the values Python serves on a healthy start |
